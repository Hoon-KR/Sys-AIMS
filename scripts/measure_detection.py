#!/usr/bin/env python3
"""장애 감지 / 자동 복구 시간 측정.

  python3 scripts/measure_detection.py [--mode detect|heal] [--trials 5] [--container pitwall_web]

  detect  docker stop → 감지 측정 → 스크립트가 docker start → 복구 확인 측정
  heal    docker stop 만 한다. 재기동은 Zabbix Action → healer 가 수행 (사람 개입 없음)

각 회차 공통:
  1) 정상 상태(최근 값 200, 열린 문제 없음) 확인
  2) 0~15초 무작위 대기 — 수집 주기(15s) 대비 장애 시점을 분산시켜 평균이 한쪽으로 치우치지 않게 함
  3) docker stop 시각을 기준(t=0)으로 각 시점을 측정
시간은 Zabbix 이벤트의 서버 시각(clock + ns)과 Docker의 StartedAt 으로 계산한다.

macOS에서는 스스로 caffeinate -i 아래에서 다시 실행된다 (절전으로 측정이 깨지는 것 방지,
docs/troubleshooting.md #3). 덮개를 닫으면 caffeinate 와 무관하게 잠드므로 열어 둘 것.
"""

import argparse
import datetime
import json
import os
import random
import statistics
import subprocess
import sys
import time
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "automation"))

from common.zabbix_api import ZabbixAPI  # noqa: E402

POLL = 0.5
TIMEOUT = 150
ALERT_STATUS = {"0": "not sent", "1": "sent", "2": "failed", "3": "new"}


def ensure_caffeinated():
    if sys.platform == "darwin" and not os.environ.get("SYS_AIMS_CAFFEINATED"):
        env = {**os.environ, "SYS_AIMS_CAFFEINATED": "1"}
        os.execvpe("caffeinate", ["caffeinate", "-i", sys.executable, *sys.argv], env)


def event_time(event, prefix=""):
    return int(event[f"{prefix}clock"]) + int(event[f"{prefix}ns"]) / 1e9


def open_problems(api, hostid):
    return api.call("problem.get", {"hostids": hostid, "tags": [{"tag": "healing", "value": "auto", "operator": 1}],
                                    "output": ["eventid", "clock", "ns", "name"]})


def wait_for(predicate, what):
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(POLL)
    raise SystemExit(f"timeout waiting for {what}")


def wait_healthy(api, hostid):
    def healthy():
        items = api.call("item.get", {"hostids": hostid, "filter": {"key_": "http.status.code"}, "output": ["lastvalue", "lastclock"]})
        fresh = items and items[0]["lastvalue"] == "200" and time.time() - int(items[0]["lastclock"]) < 20
        return fresh and not open_problems(api, hostid)
    wait_for(healthy, "healthy baseline")


def docker(*args):
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()


def container_started_at(name):
    raw = docker("inspect", "-f", "{{.State.StartedAt}}", name)  # 2026-09-18T09:53:41.123456789Z
    base, frac = raw.rstrip("Z").split(".")
    return datetime.datetime.fromisoformat(base).replace(tzinfo=datetime.timezone.utc).timestamp() + float("0." + frac)


def reset_healer_circuit():
    # 측정은 서킷 한도(10분 3회)를 넘으므로 회차마다 초기화한다. 토큰은 컨테이너 환경변수에서 읽는다.
    docker("exec", "healer", "python", "-c",
           "import os,urllib.request;urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8080/reset',"
           "method='POST',headers={'Authorization':'Bearer '+os.environ['HEALER_TOKEN']}),timeout=5)")


def recovery_event(api, problem):
    ev = api.call("event.get", {"eventids": problem["eventid"], "output": ["r_eventid"]})[0]
    if ev["r_eventid"] != "0":
        return api.call("event.get", {"eventids": ev["r_eventid"], "output": ["clock", "ns"]})[0]


def summarize(label, values):
    print(f"{label}: 평균 {statistics.mean(values):.1f}s / 중앙값 {statistics.median(values):.1f}s / "
          f"최소 {min(values):.1f}s / 최대 {max(values):.1f}s (n={len(values)})")


def run_detect(api, hostid, container, n):
    docker("stop", container)
    t_down = time.time()
    problem = wait_for(lambda: open_problems(api, hostid), "problem")[0]
    docker("start", container)
    t_up = time.time()
    rec = wait_for(lambda: recovery_event(api, problem), "recovery")
    row = {"detect": event_time(problem) - t_down, "recover": event_time(rec) - t_up}
    print(f"trial {n}: detect {row['detect']:5.1f}s  recover {row['recover']:5.1f}s", flush=True)
    return row


def run_heal(api, hostid, container, n):
    reset_healer_circuit()
    docker("stop", container)
    t_down = time.time()
    problem = wait_for(lambda: open_problems(api, hostid), "problem")[0]
    # 여기서 스크립트는 아무것도 하지 않는다. 재기동은 Zabbix Action → healer 의 몫이다.
    rec = wait_for(lambda: recovery_event(api, problem), "recovery (self-healing)")
    alerts = api.call("alert.get", {"eventids": problem["eventid"], "output": ["status", "error", "esc_step"],
                                    "selectMediatypes": ["name"]})
    row = {
        "detect": event_time(problem) - t_down,
        "restarted": container_started_at(container) - t_down,  # 실제 서비스 복구 시점
        "recovered": event_time(rec) - t_down,                  # Zabbix가 복구를 확인한 시점
        "event_id": problem["eventid"],
        "alerts": [f"{a['mediatypes'][0]['name'] if a['mediatypes'] else '?'}:{ALERT_STATUS[a['status']]}" for a in alerts],
    }
    print(f"trial {n}: detect {row['detect']:5.1f}s  restarted {row['restarted']:5.1f}s  "
          f"zabbix-recovered {row['recovered']:5.1f}s  event {row['event_id']} actions {row['alerts']}", flush=True)
    return row


def main():
    ensure_caffeinated()
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["detect", "heal"], default="detect")
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--container", default="pitwall_web")
    args = parser.parse_args()
    print(f"mode={args.mode} trials={args.trials} caffeinated={bool(os.environ.get('SYS_AIMS_CAFFEINATED'))}", flush=True)

    rows = []
    with ZabbixAPI.from_env() as api:
        hostid = api.call("host.get", {"filter": {"host": [args.container]}, "output": ["hostid"]})[0]["hostid"]
        for n in range(1, args.trials + 1):
            wait_healthy(api, hostid)
            time.sleep(random.uniform(0, 15))
            rows.append((run_heal if args.mode == "heal" else run_detect)(api, hostid, args.container, n))

    print()
    if args.mode == "detect":
        print("| 회차 | 감지 (s) | 복구 확인 (s) |\n|---|---|---|")
        for n, r in enumerate(rows, 1):
            print(f"| {n} | {r['detect']:.1f} | {r['recover']:.1f} |")
        print(); summarize("감지", [r["detect"] for r in rows]); summarize("복구 확인", [r["recover"] for r in rows])
    else:
        print("| 회차 | 감지 (s) | 재기동 완료 (s) | Zabbix 복구 확인 (s) | Event ID | Action |\n|---|---|---|---|---|---|")
        for n, r in enumerate(rows, 1):
            print(f"| {n} | {r['detect']:.1f} | {r['restarted']:.1f} | {r['recovered']:.1f} | {r['event_id']} | {', '.join(r['alerts'])} |")
        print()
        summarize("감지 (stop → 문제 이벤트)", [r["detect"] for r in rows])
        summarize("서비스 다운타임 (stop → 재기동 완료)", [r["restarted"] for r in rows])
        summarize("Zabbix 기준 다운타임 (stop → 복구 이벤트)", [r["recovered"] for r in rows])
    print("\nraw:", json.dumps(rows, ensure_ascii=False))


if __name__ == "__main__":
    main()
