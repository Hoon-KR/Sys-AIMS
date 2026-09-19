"""일일 보고서 데이터 수집 — 숫자는 전부 여기(코드)에서 계산한다. AI는 숫자를 만들지 않는다.

수집 대상
  - 서비스 가용성: http.status.code 아이템이 있는 모든 호스트 (200 비율)
  - 장애: Zabbix 문제 이벤트 (지속 시간, 누적 다운타임, MTTR)
  - 리소스: zabbix-agent 의 CPU / load / 메모리 / eth0 트래픽 (디스크는 수집되면 포함)
  - 자동화: healer.jsonl / rca.jsonl (Zabbix에 없는 Sys-AIMS 고유 데이터)
  - 데이터 품질: 아이템별 수집률 = 받은 점 수 / (기간 ÷ 실제 수집 간격)

각 섹션은 독립적으로 실패할 수 있다. 실패한 섹션은 {"error": ...} 로 남고 나머지는 계속 수집한다.
"""

import json
import math
import pathlib
import statistics
import time
import urllib.error

from common.zabbix_api import ZabbixAPI, ZabbixAPIError

AGENT_HOST = "zabbix-agent"
HTTP_KEY = "http.status.code"
RESOURCE_ITEMS = [  # (id, key, 이름, 단위)
    ("cpu", "system.cpu.util", "CPU 사용률", "%"),
    ("load", "system.cpu.load[all,avg5]", "Load average (5분)", ""),
    ("mem_avail", "vm.memory.size[available]", "가용 메모리", "B"),
    ("mem_total", "vm.memory.size[total]", "전체 메모리", "B"),
    ("net_in", 'net.if.in["eth0"]', "eth0 수신", "bps"),
    ("net_out", 'net.if.out["eth0"]', "eth0 송신", "bps"),
    ("disk", "vfs.fs.dependent.size[/,pused]", "디스크 사용률 (/)", "%"),
]
DISK_UNAVAILABLE_REASON = ("컨테이너 agent 한계: 파일시스템 탐색에서 호스트 '/'가 발견되지 않음 "
                           "(EC2 이전 후 호스트 루트 읽기 전용 마운트로 수집 예정, docs/aws-migration.md)")
MAX_INCIDENTS = 20
MAX_RCA_SUMMARIES = 10
RETRY_DELAYS = (2, 5)
# 비용 추정용 단가 (USD / 1M tokens, OpenAI 공식 요금 페이지 short context, 2026-09-19 조회)
PRICING = {"gpt-5.6-luna": {"input": 0.20, "cache_write": 0.25, "cached": 0.02, "output": 1.20}}


def _retry(fn):
    """네트워크 오류는 2회 재시도(2초, 5초). API 오류(권한 등)는 재시도하지 않는다."""
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            return fn()
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == len(RETRY_DELAYS):
                raise
            time.sleep(RETRY_DELAYS[attempt])


def _pct(part, whole):
    return round(100 * part / whole, 1) if whole else None


def _series_stats(points, window_s):
    """points: [(clock, value)] → 통계 + 수집률. 수집 간격은 연속된 점 간격의 중앙값으로 추정한다."""
    if not points:
        return {"points": 0, "coverage_pct": 0.0}
    values = sorted(v for _, v in points)
    clocks = [c for c, _ in points]
    gaps = [b - a for a, b in zip(clocks, clocks[1:]) if b > a]
    interval = statistics.median(gaps) if gaps else None
    expected = window_s / interval if interval else None
    p95 = values[min(len(values) - 1, math.ceil(0.95 * len(values)) - 1)]
    return {
        "points": len(points),
        "interval_s": round(interval, 1) if interval else None,
        "coverage_pct": round(min(100.0, 100 * len(points) / expected), 1) if expected else None,
        "avg": statistics.fmean(values), "p95": p95, "max": values[-1], "min": values[0],
        "first_clock": clocks[0], "last_clock": clocks[-1],
    }


def _history(api, item, start, end):
    rows = _retry(lambda: api.call("history.get", {
        "itemids": item["itemid"], "history": int(item["value_type"]), "time_from": int(start), "time_till": int(end) - 1,
        "sortfield": "clock", "sortorder": "ASC", "output": ["clock", "value"], "limit": 100000}))
    return [(int(r["clock"]), float(r["value"])) for r in rows]


# ---------------------------------------------------------------- Zabbix sections
def resources(api, start, end):
    keys = [k for _, k, _, _ in RESOURCE_ITEMS]
    items = _retry(lambda: api.call("item.get", {"host": AGENT_HOST, "filter": {"key_": keys},
                                                 "output": ["itemid", "key_", "value_type", "units"]}))
    by_key = {i["key_"]: i for i in items}
    out = {}
    for rid, key, name, unit in RESOURCE_ITEMS:
        if key not in by_key:
            out[rid] = {"name": name, "unit": unit, "unavailable": DISK_UNAVAILABLE_REASON if rid == "disk" else "아이템 없음"}
            continue
        out[rid] = {"name": name, "unit": unit, **_series_stats(_history(api, by_key[key], start, end), end - start)}
    # 메모리 사용률 = 1 - 가용/전체 (전체 메모리는 거의 일정하므로 평균값 사용)
    avail, total = out.get("mem_avail", {}), out.get("mem_total", {})
    if avail.get("points") and total.get("points"):
        out["mem_used_pct"] = {"name": "메모리 사용률", "unit": "%", "points": avail["points"], "coverage_pct": avail["coverage_pct"],
                               "avg": 100 * (1 - avail["avg"] / total["avg"]), "max": 100 * (1 - avail["min"] / total["avg"]),
                               "min_available_bytes": avail["min"]}
    return out


def services(api, start, end):
    items = _retry(lambda: api.call("item.get", {"filter": {"key_": HTTP_KEY}, "output": ["itemid", "value_type"],
                                                 "selectHosts": ["host"]}))
    out = {}
    for item in items:
        host = item["hosts"][0]["host"]
        points = _history(api, item, start, end)
        stats = _series_stats(points, end - start)
        # 감시 시작 시각 = 이 아이템의 가장 오래된 데이터 (기간과 무관). 기간이 끝난 뒤 추가된 서비스를
        # "수집 실패"로 오해하지 않도록 AI와 표에 함께 준다 (실측: 신규 서비스 수집률 0%를 수집 경로 문제로 해석).
        first_ever = _retry(lambda: api.call("history.get", {"itemids": item["itemid"], "history": int(item["value_type"]),
                                                             "sortfield": "clock", "sortorder": "ASC", "limit": 1, "output": ["clock"]}))
        since = int(first_ever[0]["clock"]) if first_ever else None
        ok = sum(1 for _, v in points if v == 200)
        # 가장 긴 연속 실패 구간 (수집된 점 기준)
        longest, run_start = 0, None
        for clock, v in points:
            if v != 200:
                run_start = clock if run_start is None else run_start
                longest = max(longest, clock - run_start + (stats.get("interval_s") or 0))
            else:
                run_start = None
        out[host] = {"points": len(points), "coverage_pct": stats["coverage_pct"], "interval_s": stats.get("interval_s"),
                     "first_clock": stats.get("first_clock"), "monitoring_since_clock": since,
                     "monitoring_since": time.strftime("%m-%d %H:%M", time.localtime(since)) if since else None,
                     "monitored_during_period": since is not None and since < end,
                     "availability_pct": _pct(ok, len(points)), "non_200_points": len(points) - ok,
                     "longest_failure_s": round(longest)}
    return out


def incidents(api, start, end, rca_by_event):
    events = _retry(lambda: api.call("event.get", {
        "source": 0, "object": 0, "value": 1, "time_from": int(start), "time_till": int(end) - 1,
        "output": ["eventid", "clock", "name", "severity", "r_eventid"], "selectHosts": ["host"],
        "selectTags": "extend", "sortfield": ["clock"], "sortorder": "ASC"}))
    recovery_ids = [e["r_eventid"] for e in events if e["r_eventid"] != "0"]
    recovered = {r["eventid"]: int(r["clock"]) for r in _retry(lambda: api.call(
        "event.get", {"eventids": recovery_ids, "output": ["eventid", "clock"]}))} if recovery_ids else {}
    rows, per_host, durations = [], {}, []
    for e in events:
        host = e["hosts"][0]["host"] if e["hosts"] else "?"
        clock = int(e["clock"])
        r_clock = recovered.get(e["r_eventid"])
        duration = (r_clock or int(end)) - clock
        tags = {t["tag"]: t["value"] for t in e["tags"]}
        rca = rca_by_event.get(e["eventid"], {})
        rows.append({"event_id": e["eventid"], "time": time.strftime("%m-%d %H:%M", time.localtime(clock)), "host": host,
                     "name": e["name"], "duration_s": duration, "resolved": bool(r_clock), "healing": tags.get("healing", "-"),
                     "rca_category": rca.get("category"), "rca_summary": rca.get("summary")})
        per_host.setdefault(host, {"count": 0, "downtime_s": 0})
        per_host[host]["count"] += 1
        per_host[host]["downtime_s"] += duration
        if r_clock:
            durations.append(duration)
    return {"count": len(rows), "resolved": len(durations), "open": len(rows) - len(durations),
            "mttr_s": round(statistics.fmean(durations)) if durations else None,
            "per_host": per_host, "list": rows[-MAX_INCIDENTS:], "list_truncated": max(0, len(rows) - MAX_INCIDENTS)}


# ---------------------------------------------------------------- local (Sys-AIMS) sections
def _read_events(events_dir, source, start, end):
    rows = []
    for path in sorted(pathlib.Path(events_dir).glob(f"{source}.jsonl*")):
        for line in path.read_text(errors="replace").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if start <= r.get("epoch", 0) < end:
                rows.append(r)
    return sorted(rows, key=lambda r: r["epoch"])


def automation(events_dir, start, end):
    heal = _read_events(events_dir, "healer", start, end)
    rca = _read_events(events_dir, "rca", start, end)
    count = lambda rows, name: sum(1 for r in rows if r["event"] == name)
    succeeded = [r for r in heal if r["event"] == "heal.succeeded"]
    completed = [r for r in rca if r["event"] == "rca.completed"]
    tokens_in = sum(r.get("usage", {}).get("input_tokens", 0) for r in completed)
    tokens_out = sum(r.get("usage", {}).get("output_tokens", 0) for r in completed)
    cost = 0.0
    for r in completed:
        price, u = PRICING.get(r.get("model")), r.get("usage", {})
        if not price:
            cost = None
            break
        d = u.get("input_tokens_details", {})
        cw, ca = d.get("cache_write_tokens", 0), d.get("cached_tokens", 0)
        cost += ((u.get("input_tokens", 0) - cw - ca) * price["input"] + cw * price["cache_write"]
                 + ca * price["cached"] + u.get("output_tokens", 0) * price["output"]) / 1e6
    categories = {}
    for r in completed:
        categories[r["category"]] = categories.get(r["category"], 0) + 1
    return {
        "healer": {"requested": count(heal, "heal.requested"), "succeeded": len(succeeded), "failed": count(heal, "heal.failed"),
                   "blocked": count(heal, "heal.blocked"), "skipped": count(heal, "heal.skipped"),
                   "circuit_opened": count(heal, "circuit.opened"), "circuit_reset": count(heal, "circuit.reset"),
                   "avg_restart_s": round(statistics.fmean(r["elapsed_s"] for r in succeeded), 1) if succeeded else None},
        "rca": {"completed": len(completed), "failed": count(rca, "rca.failed"), "skipped": count(rca, "rca.skipped"),
                "categories": categories, "tokens_in": tokens_in, "tokens_out": tokens_out,
                "cost_usd": round(cost, 4) if cost is not None else None,
                "summaries": [{"event_id": r["event_id"], "category": r["category"], "confidence": r["confidence"],
                               "summary": r["summary"][:200]} for r in completed[-MAX_RCA_SUMMARIES:]]},
    }, {r["event_id"]: r for r in completed}


# ---------------------------------------------------------------- orchestration
def collect(*, zabbix_url, zabbix_user, zabbix_password, events_dir, start, end):
    """한 기간의 facts. 섹션별 실패는 error 로 남긴다."""
    facts = {"window": {"start": start, "end": end, "hours": round((end - start) / 3600, 1)}, "errors": {}}
    try:
        facts["automation"], rca_by_event = automation(events_dir, start, end)
    except Exception as e:  # 로컬 파일 문제도 보고서 전체를 막지 않는다
        facts["errors"]["automation"] = f"{type(e).__name__}: {e}"[:200]
        rca_by_event = {}
    try:
        api = _retry(lambda: ZabbixAPI(zabbix_url, zabbix_user, zabbix_password))
    except Exception as e:
        reason = f"Zabbix API 접속 실패 — {type(e).__name__}: {e}"[:200]
        for section in ("services", "incidents", "resources"):
            facts["errors"][section] = reason
        return facts
    try:
        for section, fn in (("services", lambda: services(api, start, end)),
                            ("incidents", lambda: incidents(api, start, end, rca_by_event)),
                            ("resources", lambda: resources(api, start, end))):
            try:
                facts[section] = fn()
            except (ZabbixAPIError, urllib.error.URLError, TimeoutError, ConnectionError, KeyError, ValueError) as e:
                facts["errors"][section] = f"{type(e).__name__}: {e}"[:200]
    finally:
        try:
            api.logout()
        except Exception:
            pass
    return facts
