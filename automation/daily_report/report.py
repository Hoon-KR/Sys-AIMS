"""일일 점검 보고서 생성.

원칙: "숫자는 코드, 해석은 AI" (docs/ai-trust.md)
  - 표와 숫자는 collect.py 가 계산한 값을 코드가 그대로 렌더링한다.
  - 종합 판정(정상/주의/위험)도 코드 규칙으로 정한다 (verdict). 같은 데이터에는 항상 같은 판정.
  - AI 는 JSON 스키마로 해석 문장만 돌려준다 (판정 사유 설명, 전일 대비, 주의사항, 조치).
  - AI 문장 속 숫자는 입력 데이터와 대조하고, 입력에 없는 숫자는 ⚠️ 로 표시한다.
  - AI 가 본 입력(JSON)을 보고서 옆에 그대로 저장한다 (사후 검증용).

실패 시에도 보고서는 항상 만든다.
  Zabbix 실패 → 해당 섹션에 "수집 실패(사유)"
  OpenAI 실패 / 일일 한도 초과 → 코드가 만든 표는 그대로, AI 섹션은 "AI 분석 불가(사유)"
"""

import json
import os
import pathlib
import re
import string
import time

from common import llm, quota, slack
from common.eventlog import EventLog
from daily_report import collect, site

PROMPTS = pathlib.Path(os.environ.get("REPORT_PROMPTS_DIR", pathlib.Path(__file__).resolve().parents[1] / "prompts"))
REPORTS_DIR = pathlib.Path(os.environ.get("REPORTS_DIR", "/reports"))
EVENTS_DIR = os.environ.get("EVENTS_DIR", "/data/events")
ZABBIX_URL = os.environ.get("REPORT_ZABBIX_URL", "http://zabbix-web:8080/api_jsonrpc.php")
ZABBIX_USER = os.environ.get("ZABBIX_REPORT_USER", "")
ZABBIX_PASSWORD = os.environ.get("ZABBIX_REPORT_PASSWORD", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "")
OPENAI_TIMEOUT = int(os.environ.get("REPORT_OPENAI_TIMEOUT", "40"))
MAX_PER_DAY = int(os.environ.get("REPORT_MAX_PER_DAY", "5"))
MAX_OUTPUT_TOKENS = int(os.environ.get("REPORT_MAX_OUTPUT_TOKENS", "2000"))
MAX_INPUT_CHARS = 16000
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
BASE_URL = os.environ.get("REPORT_BASE_URL", "").rstrip("/")
WEEKDAYS = "월화수목금토일"
STATUS_ICON = {"정상": "🟢", "주의": "🟡", "위험": "🔴"}
CATEGORY_KO = {"external_stop": "외부 종료", "oom": "메모리 부족", "crash": "앱 오류", "config_error": "설정 오류",
               "dependency": "의존 서비스", "resource": "자원 고갈", "unknown": "판단 불가"}

SYSTEM_PROMPT = (PROMPTS / "report_system.md").read_text()
USER_TEMPLATE = string.Template((PROMPTS / "report_user.md").read_text())
SCHEMA = json.loads((PROMPTS / "report_schema.json").read_text())
MD_TEMPLATE = string.Template((PROMPTS / "report_template.md").read_text())


def events():
    return EventLog("report", directory=REPORTS_DIR / ".events")


# ---------------------------------------------------------------- formatting
def fmt_time(epoch, with_date=True):
    return time.strftime("%m-%d %H:%M" if with_date else "%H:%M", time.localtime(epoch))


def fmt_dur(seconds):
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}초"
    if seconds < 3600:
        return f"{seconds // 60}분" + (f" {seconds % 60}초" if seconds % 60 else "")
    return f"{seconds // 3600}시간" + (f" {seconds % 3600 // 60}분" if seconds % 3600 // 60 else "")


def fmt_num(value, unit):
    if value is None:
        return "-"
    if unit == "B":
        return f"{value / 1024 ** 3:.2f} GiB"
    if unit == "bps":
        return f"{value / 1e6:.2f} Mbps" if value >= 1e6 else f"{value / 1e3:.1f} kbps"
    if unit == "%":
        return f"{value:.1f}%"
    return f"{value:.2f}"


def fmt_pct(value):
    return "-" if value is None else f"{value:.1f}%"


def fmt_delta(cur, prev, unit="", reliable=True):
    if cur is None or prev is None:
        return "비교 데이터 없음"
    diff = cur - prev
    mark = "" if reliable else " (수집률 낮음)"
    if unit == "%":
        return f"{diff:+.1f}%p{mark}"
    if unit in ("B", "bps"):
        return f"{fmt_num(abs(diff), unit)} {'↑' if diff > 0 else '↓' if diff < 0 else ''}{mark}".strip()
    return f"{diff:+.2f}{mark}"


# ---------------------------------------------------------------- AI input
def compact(facts):
    """AI 입력용 요약 (소수 첫째 자리 반올림, 긴 목록은 상한). 표와 같은 원본 값에서 만든다."""
    r1 = lambda v: round(v, 1) if isinstance(v, float) else v
    out = {"window_hours": facts["window"]["hours"], "errors": facts["errors"]}
    if "services" in facts:
        out["services"] = {h: {k: r1(v) for k, v in s.items() if k not in ("first_clock", "monitoring_since_clock")}
                           for h, s in facts["services"].items()}
    if "incidents" in facts:
        i = facts["incidents"]
        out["incidents"] = {k: i[k] for k in ("count", "resolved", "open", "mttr_s", "per_host", "list_truncated")}
        out["incidents"]["list"] = [{k: e[k] for k in ("time", "host", "name", "duration_s", "resolved", "healing", "rca_category")}
                                    for e in i["list"]]
    if "resources" in facts:
        out["resources"] = {rid: ({k: r1(v) for k, v in s.items() if k in ("avg", "p95", "max", "coverage_pct", "unit", "unavailable")})
                            for rid, s in facts["resources"].items() if rid not in ("mem_avail", "mem_total")}
    if "automation" in facts:
        out["automation"] = facts["automation"]
    return out


def build_ai_input(cur, prev, v):
    # verdict 는 코드가 정한 판정. AI 는 이를 바꾸지 않고 사유를 설명만 한다.
    payload = {"verdict": {k: v[k] for k in ("status", "danger_reasons", "caution_reasons")},
               "current": compact(cur), "previous": compact(prev)}
    payload["previous"].get("incidents", {}).pop("list", None)
    # 직전 기간에 존재하지 않던 서비스는 비교 대상에서 뺀다 (두 기간의 "감시 시작" 표시를 AI가 섞어 해석한 사례가 있었음)
    prev_services = payload["previous"].get("services", {})
    for host in [h for h, sv in prev_services.items() if not sv.get("monitored_during_period")]:
        del prev_services[host]
    for sv in prev_services.values():
        sv.pop("monitoring_since", None)
        sv.pop("monitored_during_period", None)
    payload["previous"].get("automation", {}).get("rca", {}).pop("summaries", None)
    text = json.dumps(payload, ensure_ascii=False)
    # 상한 초과 시: 장애 목록 → RCA 요약 순으로 오래된 것부터 줄인다
    while len(text) > MAX_INPUT_CHARS:
        inc = payload["current"].get("incidents", {}).get("list", [])
        summaries = payload["current"].get("automation", {}).get("rca", {}).get("summaries", [])
        if inc:
            inc.pop(0)
            payload["current"]["incidents"]["list_truncated"] = payload["current"]["incidents"].get("list_truncated", 0) + 1
        elif summaries:
            summaries.pop(0)
        else:
            break
        text = json.dumps(payload, ensure_ascii=False)
    return payload, text


_NUM = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*(?:\.\d+)?")


def _numbers(text):
    return {n.replace(",", "") for n in _NUM.findall(text)}


def number_check(ai, ai_input_text, extra_text):
    """AI 문장 속 숫자가 입력 데이터(또는 기간 표기)에 있는지 대조한다. 반올림(정수/소수 1자리)은 허용."""
    known = set()
    # 시스템 프롬프트의 정책 기준값(예: 수집률 50%/80%)도 AI가 인용할 수 있는 "알려진 숫자"다
    for n in _numbers(ai_input_text + " " + extra_text + " " + SYSTEM_PROMPT):
        known.add(n)
        try:
            v = float(n)
            known.update({str(round(v)), f"{v:.1f}", f"{v:.0f}"})
        except ValueError:
            pass
    fields = [ai["overall_summary"], ai["incident_commentary"], ai["data_quality_note"], *ai["comparison_notes"]]
    fields += [f"{c['title']} {c['evidence']} {c['action']}" for c in ai["cautions"]]
    found = sorted(set().union(*(_numbers(f) for f in fields)), key=lambda x: float(x))
    unknown = [n for n in found if n not in known and f"{float(n):.1f}" not in known]
    return {"checked": len(found), "unknown": unknown}


# ---------------------------------------------------------------- verdict (code rule)
COVERAGE_WARN = 80
SECTION_KO = {"services": "서비스 가용성", "incidents": "장애 이벤트", "resources": "리소스", "automation": "자동화 기록"}
VERDICT_RULE = ("위험 = 미해소 장애 · 서킷 열림/차단 · 자동 복구 실패 / "
                f"주의 = 장애 발생 · 데이터 수집률 {COVERAGE_WARN}% 미만 · 수집 실패 / 정상 = 그 외")


def verdict(facts, coverage):
    """종합 판정은 코드 규칙으로 정한다. AI는 판정을 바꾸지 않고 사유만 설명한다.

    같은 데이터에 AI가 "위험"과 "주의"를 번갈아 낸 사례, Zabbix 다운(모니터링 실패)을
    "위험"(서비스 장애)으로 본 사례가 있었다 (docs/ai-trust.md 원칙 6).
    수집 실패는 서비스 장애가 아니라 모니터링 문제이므로 "주의"로만 올린다.
    """
    danger, caution = [], []
    inc = facts.get("incidents")
    heal = facts.get("automation", {}).get("healer")
    if inc and inc["open"]:
        hosts = sorted({e["host"] for e in inc["list"] if not e["resolved"]})
        danger.append(f"미해소 장애 {inc['open']}건" + (f" ({', '.join(hosts)})" if hosts else ""))
    if heal:
        if heal["circuit_opened"]:
            danger.append(f"서킷 열림 {heal['circuit_opened']}건 (반복 장애로 자동 복구 중단)")
        if heal["blocked"]:
            danger.append(f"서킷 차단 {heal['blocked']}건 (자동 복구 요청 거부)")
        if heal["failed"]:
            danger.append(f"자동 복구 실패 {heal['failed']}건")
    if inc and inc["count"]:
        caution.append(f"장애 {inc['count']}건 발생" + (" (모두 해소)" if not inc["open"] else ""))
    if coverage < COVERAGE_WARN:
        caution.append(f"데이터 수집률 {coverage:.1f}% (기준 {COVERAGE_WARN}% 미만)")
    if facts["errors"]:
        names = ", ".join(SECTION_KO.get(k, k) for k in facts["errors"])
        caution.append(f"수집 실패: {names} — 모니터링 문제이며, 해당 영역의 서비스 장애 여부는 확인할 수 없음")
    status = "위험" if danger else "주의" if caution else "정상"
    return {"status": status, "danger_reasons": danger, "caution_reasons": caution, "rule": VERDICT_RULE}


def verdict_block(v):
    lines = [f"### {STATUS_ICON[v['status']]} {v['status']}", "", "**판정 사유** (코드 규칙)"]
    lines += [f"- 🔴 {r}" for r in v["danger_reasons"]]
    lines += [f"- 🟡 {r}" for r in v["caution_reasons"]]
    if v["status"] == "정상":
        lines.append("- 🟢 위험·주의 조건에 해당하는 항목 없음")
    lines += ["", f"<sub>판정 규칙: {v['rule']}</sub>"]
    return "\n".join(lines)


# ---------------------------------------------------------------- markdown sections
def overall_coverage(facts):
    values = [s["coverage_pct"] for s in facts.get("services", {}).values() if s.get("coverage_pct") is not None]
    values += [s["coverage_pct"] for rid, s in facts.get("resources", {}).items()
               if s.get("coverage_pct") is not None and rid in ("cpu", "load", "mem_avail", "net_in", "net_out")]
    return round(sum(values) / len(values), 1) if values else 0.0


def section_error(facts, section):
    return f"> ⚠️ **수집 실패** — {facts['errors'][section]}" if section in facts["errors"] else None


def services_table(cur, prev):
    if err := section_error(cur, "services"):
        return err
    if not cur["services"]:
        return "감시 중인 서비스가 없습니다."
    rows = ["| 서비스 | 가용률 | 비정상 응답 | 최장 연속 실패 | 장애 | 누적 다운타임 | 수집률 | 직전 대비 가용률 |",
            "|---|---:|---:|---:|---:|---:|---:|---|"]
    prev_s, per_host = prev.get("services", {}), cur.get("incidents", {}).get("per_host", {})
    for host in sorted(cur["services"]):
        s, p, inc = cur["services"][host], prev_s.get(host, {}), per_host.get(host, {"count": 0, "downtime_s": 0})
        reliable = (s.get("coverage_pct") or 0) >= 50 and (p.get("coverage_pct") or 0) >= 50
        rows.append(f"| {host} | **{fmt_pct(s['availability_pct'])}** | {s['non_200_points']} / {s['points']} | "
                    f"{fmt_dur(s['longest_failure_s'])} | {inc['count']} | {fmt_dur(inc['downtime_s'])} | {fmt_pct(s['coverage_pct'])} | "
                    f"{fmt_delta(s['availability_pct'], p.get('availability_pct'), '%', reliable)} |")
    rows.append("\n<sub>가용률 = 수집된 HTTP 체크 중 200 응답 비율. 비정상 응답 = 200이 아닌 체크 수 / 전체 체크 수.</sub>")
    return "\n".join(rows)


def incidents_sections(cur):
    if err := section_error(cur, "incidents"):
        return err, ""
    i = cur["incidents"]
    if not i["count"]:
        return "기간 중 장애가 없습니다.", ""
    summary = (f"**장애 {i['count']}건** (해소 {i['resolved']} · 미해소 {i['open']}) · "
               f"평균 복구 시간(MTTR) **{fmt_dur(i['mttr_s'])}**")
    rows = ["| 시각 | 서비스 | 장애 | 지속 | 상태 | 자동 복구 | AI 분류 |", "|---|---|---|---:|---|---|---|"]
    for e in i["list"]:
        rows.append(f"| {e['time']} | {e['host']} | {e['name'].split(': ', 1)[-1]} | {fmt_dur(e['duration_s'])} | "
                    f"{'해소' if e['resolved'] else '**미해소**'} | {'대상' if e['healing'] == 'auto' else '제외'} | "
                    f"{CATEGORY_KO.get(e['rca_category'], '-') if e['rca_category'] else '-'} |")
    if i["list_truncated"]:
        rows.append(f"\n<sub>오래된 장애 {i['list_truncated']}건은 표에서 생략했습니다.</sub>")
    return summary, "\n".join(rows)


def resources_table(cur, prev):
    if err := section_error(cur, "resources"):
        return err
    r, p = cur["resources"], prev.get("resources", {})
    rows = ["| 지표 | 평균 | p95 | 최대 | 수집률 | 직전 평균 대비 |", "|---|---:|---:|---:|---:|---|"]
    for rid in ("cpu", "load", "mem_used_pct", "net_in", "net_out", "disk"):
        s = r.get(rid)
        if not s:
            continue
        if s.get("unavailable"):
            rows.append(f"| {s['name']} | 미수집 | | | | {s['unavailable']} |")
            continue
        if not s.get("points"):
            rows.append(f"| {s['name']} | 데이터 없음 | | | 0% | |")
            continue
        ps = p.get(rid, {})
        reliable = (s.get("coverage_pct") or 0) >= 50 and (ps.get("coverage_pct") or 0) >= 50
        rows.append(f"| {s['name']} | {fmt_num(s['avg'], s['unit'])} | {fmt_num(s.get('p95'), s['unit'])} | "
                    f"{fmt_num(s['max'], s['unit'])} | {fmt_pct(s.get('coverage_pct'))} | "
                    f"{fmt_delta(s['avg'], ps.get('avg'), s['unit'], reliable)} |")
    mem = r.get("mem_used_pct")
    if mem:
        rows.append(f"\n<sub>최소 가용 메모리 {fmt_num(mem['min_available_bytes'], 'B')}. "
                    "맥 로컬에서는 Docker Desktop VM의 지표입니다.</sub>")
    return "\n".join(rows)


def automation_section(cur):
    if err := section_error(cur, "automation"):
        return err
    h, r = cur["automation"]["healer"], cur["automation"]["rca"]
    rate = f" (성공률 {100 * h['succeeded'] / (h['succeeded'] + h['failed']):.0f}%)" if h["succeeded"] + h["failed"] else ""
    cats = ", ".join(f"{CATEGORY_KO.get(k, k)} {v}" for k, v in sorted(r["categories"].items(), key=lambda x: -x[1])) or "-"
    cost = f"약 ${r['cost_usd']:.4f}" if r["cost_usd"] is not None else "단가 미등록 모델"
    return "\n".join([
        "| 항목 | 값 |", "|---|---|",
        f"| 자동 복구 요청 | {h['requested']}건 |",
        f"| 재기동 성공 / 실패 | {h['succeeded']} / {h['failed']}{rate} |",
        f"| 평균 재기동 시간 | {fmt_dur(h['avg_restart_s'])} |" if h["avg_restart_s"] is not None else "| 평균 재기동 시간 | - |",
        f"| 서킷 차단 / 열림 / 수동 해제 | {h['blocked']} / {h['circuit_opened']} / {h['circuit_reset']} |",
        f"| AI 장애 분석 (완료 / 실패 / 한도 생략) | {r['completed']} / {r['failed']} / {r['skipped']} |",
        f"| AI 분석 원인 분류 | {cats} |",
        f"| AI 분석 토큰 (입력 / 출력) · 비용 | {r['tokens_in']:,} / {r['tokens_out']:,} · {cost} |",
    ])


def data_quality_section(cur, check):
    start = cur["window"]["start"]
    end = cur["window"]["end"]

    def first(s):
        if s.get("monitoring_since_clock") and s["monitoring_since_clock"] >= end:
            return f"기간 이후 감시 시작 ({s['monitoring_since']})"
        if not s.get("first_clock"):
            return "-"
        late = s["first_clock"] - start > 3 * (s.get("interval_s") or 60)
        return fmt_time(s["first_clock"]) + (" (기간 중 시작)" if late else "")
    rows = ["| 데이터 | 수집 점 수 | 수집 간격 | 수집률 | 첫 수집 |", "|---|---:|---:|---:|---|"]
    for host, s in sorted(cur.get("services", {}).items()):
        rows.append(f"| HTTP 체크 · {host} | {s['points']} | {fmt_dur(s.get('interval_s'))} | {fmt_pct(s['coverage_pct'])} | {first(s)} |")
    for rid, s in cur.get("resources", {}).items():
        if s.get("unavailable"):
            rows.append(f"| {s['name']} | - | | 미수집 | |")
        elif "interval_s" in s or s.get("points") == 0:
            rows.append(f"| {s['name']} | {s['points']} | {fmt_dur(s.get('interval_s'))} | {fmt_pct(s.get('coverage_pct'))} | {first(s)} |")
    for section, reason in cur["errors"].items():
        rows.append(f"| ⚠️ {section} 수집 실패 | | | | {reason} |")
    rows.append("\n<sub>수집률 = 수집된 점 수 ÷ (기간 ÷ 실제 수집 간격). 맥 절전·컨테이너 중지 구간은 수집되지 않습니다. "
                "'기간 중 시작'은 해당 기간 시작 이후에 첫 데이터가 들어온 경우입니다 (감시 대상이 새로 추가되었거나 수집이 중단되었다가 재개).</sub>")
    if check is not None:
        mark = "✅" if not check["unknown"] else "⚠️"
        line = f"{mark} **AI 문장 숫자 대조**: {check['checked']}개 중 {check['checked'] - len(check['unknown'])}개가 입력 데이터와 일치"
        if check["unknown"]:
            line += f" — 입력에 없는 숫자: {', '.join(check['unknown'])}"
        rows.append("\n" + line)
    return "\n".join(rows)


def ai_sections(ai, ai_error, v):
    if ai is None:
        note = f"> 🤖 **AI 분석 불가** — {ai_error.rstrip('.')}. 판정과 표는 코드가 계산한 값이므로 그대로 유효합니다."
        return {"overall": verdict_block(v) + "\n\n" + note, "incident_commentary": "",
                "cautions": note, "comparison": ""}
    cautions = "\n".join(f"{n}. **{c['title']}**\n   - 근거: {c['evidence']}\n   - 조치: {c['action']}"
                         for n, c in enumerate(ai["cautions"], 1)) or "특별히 주의할 점이 없습니다."
    extra = f"\n\n> 데이터 품질: {ai['data_quality_note']}" if ai["data_quality_note"] else ""
    return {
        "overall": f"{verdict_block(v)}\n\n🤖 {ai['overall_summary']}{extra}",
        "incident_commentary": f"🤖 {ai['incident_commentary']}",
        "cautions": "🤖\n\n" + cautions,
        "comparison": "**🤖 직전 기간 대비**\n" + "\n".join(f"- {n}" for n in ai["comparison_notes"]),
    }


# ---------------------------------------------------------------- generate
def report_name(kind, end, hours):
    if kind == "daily":
        return f"daily-{time.strftime('%Y-%m-%d', time.localtime(end))}"
    return f"adhoc-{time.strftime('%Y-%m-%d-%H%M', time.localtime(end))}-{hours:g}h"


def generate(*, kind, end, hours, use_ai=True, notify=True, zabbix_url=None):
    log = events()
    started = time.time()
    start, prev_start = end - hours * 3600, end - 2 * hours * 3600
    name = report_name(kind, end, hours)
    log.write("report.started", name=name, kind=kind, hours=hours, start=start, end=end)
    kw = dict(zabbix_url=zabbix_url or ZABBIX_URL, zabbix_user=ZABBIX_USER, zabbix_password=ZABBIX_PASSWORD, events_dir=EVENTS_DIR)
    cur = collect.collect(start=start, end=end, **kw)
    prev = collect.collect(start=prev_start, end=start, **kw)
    coverage = overall_coverage(cur)
    v = verdict(cur, coverage)
    ai_input, ai_input_text = build_ai_input(cur, prev, v)
    period = f"{fmt_time(start)} ~ {fmt_time(end)} KST ({hours:g}시간)"
    prev_period = f"{fmt_time(prev_start)} ~ {fmt_time(start)} KST"

    ai, ai_error, meta, check = None, None, {}, None
    if not use_ai:
        ai_error = "AI 사용 안 함(--no-ai)"
    else:
        allowed, used = quota.take(REPORTS_DIR / ".state" / "quota.json", MAX_PER_DAY)
        if not allowed:
            ai_error = f"일일 한도({MAX_PER_DAY}회) 초과"
        else:
            try:
                user_input = USER_TEMPLATE.safe_substitute(period_current=period, period_previous=prev_period, facts=ai_input_text)
                ai, meta = llm.analyze(api_key=OPENAI_API_KEY, model=OPENAI_MODEL, instructions=SYSTEM_PROMPT, user_input=user_input,
                                       schema=SCHEMA, max_output_tokens=MAX_OUTPUT_TOKENS, timeout=OPENAI_TIMEOUT)
                check = number_check(ai, ai_input_text, period + " " + prev_period)
            except llm.LLMError as e:
                ai_error = f"OpenAI {e.kind} — {e}"

    sections = ai_sections(ai, ai_error, v)
    inc_summary, inc_table = incidents_sections(cur)
    usage = meta.get("usage", {})
    ai_label = (f"{meta.get('model')} (토큰 {usage.get('input_tokens', 0):,}/{usage.get('output_tokens', 0):,})" if ai
                else f"사용 안 함 — {ai_error}")
    title_date = time.strftime("%Y-%m-%d", time.localtime(end)) + f" ({WEEKDAYS[time.localtime(end).tm_wday]})"
    if kind != "daily":
        title_date += f" · 수동 실행 {hours:g}시간"
    markdown = MD_TEMPLATE.safe_substitute(
        title_date=title_date, period=period, generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        coverage=f"{coverage:.1f}%" + (" ⚠️ 낮음" if coverage < 80 else ""), ai_label=ai_label,
        overall=sections["overall"], services_table=services_table(cur, prev),
        incidents_summary=inc_summary, incidents_table=inc_table, incident_commentary=sections["incident_commentary"],
        resources_table=resources_table(cur, prev), cautions=sections["cautions"], comparison=sections["comparison"],
        automation=automation_section(cur), data_quality=data_quality_section(cur, check),
        footer=f"Sys-AIMS reporter · {name} · 원본 데이터: {name}.json · 비교 기간 {prev_period}",
    )

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (REPORTS_DIR / f"{name}.md").write_text(markdown)
    (REPORTS_DIR / f"{name}.json").write_text(json.dumps({
        "name": name, "kind": kind, "period": period, "coverage_pct": coverage,
        "overall_status": v["status"], "verdict": v, "ai_error": ai_error,
        "ai_input": ai_input, "ai_output": ai, "number_check": check, "llm_meta": meta,
    }, ensure_ascii=False, indent=2))
    site.render(REPORTS_DIR, name, markdown)
    deleted = site.apply_retention(REPORTS_DIR)
    site.render_index(REPORTS_DIR)

    notified = None
    if notify:
        notified = slack.post(SLACK_WEBHOOK_URL, *slack_message(name, title_date, period, cur, ai, ai_error, coverage, v))
    record = log.write("report.completed", name=name, kind=kind, coverage_pct=coverage, overall_status=v["status"],
                       verdict_reasons=v["danger_reasons"] + v["caution_reasons"],
                       ai_error=ai_error, errors=cur["errors"], number_check=check, usage=usage, latency_s=meta.get("latency_s"),
                       elapsed_s=round(time.time() - started, 1), deleted=deleted, slack_notified=notified)
    return name, record


def slack_message(name, title_date, period, cur, ai, ai_error, coverage, v):
    status = f"{STATUS_ICON[v['status']]} {v['status']}"
    reasons = v["danger_reasons"] or v["caution_reasons"]
    svc = cur.get("services", {})
    worst = min(svc.items(), key=lambda kv: kv[1]["availability_pct"] if kv[1]["availability_pct"] is not None else 101) if svc else None
    inc = cur.get("incidents", {})
    heal = cur.get("automation", {}).get("healer", {})
    link = f"<{BASE_URL}/{name}.html|보고서 열기>" if BASE_URL else f"`{name}.md`"
    fields = [
        {"type": "mrkdwn", "text": f"*종합 판정*\n{status}"},
        {"type": "mrkdwn", "text": f"*최저 가용률*\n{worst[0]} {fmt_pct(worst[1]['availability_pct'])}" if worst else "*최저 가용률*\n-"},
        {"type": "mrkdwn", "text": f"*장애*\n{inc.get('count', '-')}건 (미해소 {inc.get('open', '-')})"},
        {"type": "mrkdwn", "text": f"*자동 복구*\n성공 {heal.get('succeeded', '-')} · 실패 {heal.get('failed', '-')}"},
    ]
    summary = ai["overall_summary"] if ai else f"AI 분석 불가 — {ai_error}"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📋 일일 점검 보고서 — {title_date}"[:150], "emoji": True}},
        {"type": "section", "fields": fields},
        {"type": "section", "text": {"type": "mrkdwn", "text": (f"*판정 사유*\n" + "\n".join(f"• {r}" for r in reasons[:4]) + "\n\n"
                                                                if reasons else "") + f"*요약*\n{summary}"[:2900]}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{period} · 수집률 {coverage:.1f}% · {link}"}]},
    ]
    return f"📋 일일 점검 보고서 — {title_date}: {status}", blocks
