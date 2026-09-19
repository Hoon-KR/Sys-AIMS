#!/usr/bin/env python3
"""Sys-AIMS 장애 시나리오 (발표/검증용). 자세한 내용: docs/chaos-scenarios.md

  python3 scripts/chaos.py dependency [--watch]   ① 의존 서비스(pitwall_api) 중지 → pitwall_web /healthz 502
  python3 scripts/chaos.py config     [--watch]   ② 잘못된 nginx 설정 배포 → pitwall_web 기동 실패
  python3 scripts/chaos.py watch                   진행 중인 장애의 타임라인만 출력
  python3 scripts/chaos.py restore                 원상 복구 (주입 파일 제거, pitwall_api 기동, 서킷 해제, pitwall_web 기동)

두 시나리오 모두 "재기동으로 해결되지 않는 장애"다.
healer는 재기동 후 healthy 확인에 실패해 502를 돌려주고(Action Failed), rca가 원인을 분석하며,
5분 뒤 에스컬레이션으로 사람을 부른다.

--watch 는 healer/rca 이벤트와 Zabbix 문제/Action 기록을 실시간으로 보여준다.
macOS에서는 스스로 caffeinate -i 아래에서 다시 실행된다.
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "automation"))

from common.zabbix_api import ZabbixAPI  # noqa: E402

CONF_DIR = REPO_ROOT / "services" / "pitwall_web" / "conf.d"
CHAOS_CONF = CONF_DIR / "zz-chaos-broken.conf"
# 설정 배포 실수를 흉내 낸다: proxy_pass 오타 → nginx [emerg] unknown directive → 기동 실패
BROKEN_CONF = """# [chaos] 잘못 배포된 설정 — scripts/chaos.py restore 로 제거
server {
    listen 8088;
    location /v2/ {
        proxy_pas http://pitwall_api:80;
    }
}
"""
ALERT_STATUS = {"0": "not sent", "1": "sent", "2": "failed", "3": "new"}


def ensure_caffeinated():
    if sys.platform == "darwin" and not os.environ.get("SYS_AIMS_CAFFEINATED"):
        os.execvpe("caffeinate", ["caffeinate", "-i", sys.executable, *sys.argv], {**os.environ, "SYS_AIMS_CAFFEINATED": "1"})


def sh(*args, check=True):
    return subprocess.run(args, capture_output=True, text=True, check=check).stdout.strip()


def ts(epoch=None):
    return time.strftime("%H:%M:%S", time.localtime(epoch or time.time()))


def container_state(name):
    return sh("docker", "inspect", "-f", "{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{end}}", name, check=False)


def wait_until(predicate, what, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(2)
    raise SystemExit(f"timeout waiting for {what}")


def healer_post(path):
    return sh("docker", "exec", "healer", "python", "-c",
              "import os,urllib.request;urllib.request.urlopen(urllib.request.Request("
              f"'http://127.0.0.1:8080{path}',method='POST',headers={{'Authorization':'Bearer '+os.environ['HEALER_TOKEN']}}),timeout=5)")


def read_events(container, name, since):
    raw = sh("docker", "exec", container, "sh", "-c", f"cat /data/events/{name}.jsonl 2>/dev/null || true", check=False)
    return [r for r in (json.loads(l) for l in raw.splitlines() if l.strip()) if r.get("epoch", 0) >= since]


# ---------------------------------------------------------------- scenarios
def scenario_dependency():
    print(f"[{ts()}] ① 의존 서비스 장애 주입: docker stop pitwall_api")
    sh("docker", "stop", "pitwall_api")


def scenario_config():
    print(f"[{ts()}] ② 설정 오류 주입: {CHAOS_CONF.relative_to(REPO_ROOT)} 작성 후 pitwall_web 재기동 (배포 흉내)")
    CHAOS_CONF.write_text(BROKEN_CONF)
    sh("docker", "restart", "pitwall_web", check=False)


def restore():
    print(f"[{ts()}] 원상 복구")
    if CHAOS_CONF.exists():
        CHAOS_CONF.unlink()
        print("  - 주입 설정 제거")
    sh("docker", "start", "pitwall_api")
    wait_until(lambda: container_state("pitwall_api") == "running/healthy", "pitwall_api healthy")
    print("  - pitwall_api healthy")
    healer_post("/reset")
    print("  - healer 서킷 해제")
    sh("docker", "restart", "pitwall_web")
    wait_until(lambda: container_state("pitwall_web") == "running/healthy", "pitwall_web healthy")
    print("  - pitwall_web healthy")
    with ZabbixAPI.from_env() as api:
        wait_until(lambda: not api.call("problem.get", {"tags": [{"tag": "healing", "value": "auto", "operator": 1}], "output": ["eventid"]}),
                   "Zabbix problems cleared", timeout=240)
    print(f"[{ts()}] 복구 완료 — 열린 문제 없음")


# ---------------------------------------------------------------- timeline
def watch(since, until_escalation, timeout):
    print(f"[{ts()}] 타임라인 관찰 시작 (에스컬레이션까지 대기: {until_escalation})")
    seen, done_rca, escalated = set(), False, False
    deadline = time.time() + timeout
    with ZabbixAPI.from_env() as api:
        while time.time() < deadline:
            rows = []
            for p in api.call("event.get", {"source": 0, "object": 0, "time_from": int(since), "value": 1,
                                            "output": ["eventid", "clock", "name", "severity"], "selectTags": "extend"}):
                rows.append((int(p["clock"]), f"zabbix:{p['eventid']}", f"Zabbix 문제 발생 — {p['name']} (event {p['eventid']})"))
                for a in api.call("alert.get", {"eventids": p["eventid"], "output": ["alertid", "clock", "status", "error", "esc_step"],
                                                "selectMediatypes": ["name"]}):
                    if a["status"] in ("1", "2"):
                        media = a["mediatypes"][0]["name"] if a["mediatypes"] else "?"
                        err = f" — {a['error'][:90]}" if a["error"] else ""
                        rows.append((int(a["clock"]), f"alert:{a['alertid']}:{a['status']}",
                                     f"Zabbix Action step{a['esc_step']} {media}: {ALERT_STATUS[a['status']]}{err}"))
                        escalated |= a["esc_step"] == "2"
            for r in read_events("healer", "healer", since):
                detail = {k: r[k] for k in ("elapsed_s", "detail", "log_lines", "status", "reason", "error") if k in r}
                rows.append((r["epoch"], f"healer:{r['epoch']}:{r['event']}", f"healer {r['event']} {detail if detail else ''}"))
            for r in read_events("rca", "rca", since):
                if r["event"] == "rca.completed":
                    done_rca = True
                    u = r.get("usage", {})
                    text = (f"rca 분석 완료 — 분류 {r['category']} / 신뢰도 {r['confidence']} / 근거 {r['evidence_verified']}/{len(r['evidence'])} 원문 일치 "
                            f"/ 토큰 {u.get('input_tokens')}/{u.get('output_tokens')} / {r['latency_s']}초\n           요약: {r['summary']}")
                elif r["event"] in ("rca.failed", "rca.skipped"):
                    done_rca = True
                    text = f"rca {r['event']} {r.get('error_kind') or r.get('reason')}"
                elif r["event"] == "rca.started":
                    s = r["log_stats"]
                    text = f"rca 분석 시작 — 로그 {s['raw_lines']}줄/{s['raw_chars']}자 → {s['sent_lines']}줄/{s['sent_chars']}자"
                else:
                    continue
                rows.append((r["epoch"], f"rca:{r['epoch']}:{r['event']}", text))
            for epoch, key, text in sorted(rows):
                if key not in seen:
                    seen.add(key)
                    print(f"[{ts(epoch)}] +{epoch - since:5.1f}s  {text}", flush=True)
            if done_rca and (escalated or not until_escalation):
                break
            time.sleep(2)
    print(f"[{ts()}] 관찰 종료. pitwall_web={container_state('pitwall_web')} pitwall_api={container_state('pitwall_api')}")


def main():
    ensure_caffeinated()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["dependency", "config", "watch", "restore"])
    parser.add_argument("--watch", action="store_true", help="주입 후 타임라인 관찰")
    parser.add_argument("--until-escalation", action="store_true", help="5분 뒤 에스컬레이션(Slack)까지 관찰")
    parser.add_argument("--since", type=float, help="watch 기준 시각 (epoch)")
    args = parser.parse_args()

    started = time.time()
    if args.command == "restore":
        return restore()
    if args.command == "dependency":
        scenario_dependency()
    elif args.command == "config":
        scenario_config()
    if args.command == "watch" or args.watch:
        watch(args.since or started - 1, args.until_escalation, timeout=480 if args.until_escalation else 180)


if __name__ == "__main__":
    main()
