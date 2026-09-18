#!/usr/bin/env python3
"""장애 감지/복구 시간 측정.

  python3 scripts/measure_detection.py [--trials 5] [--container pitwall_web]

각 회차:
  1) 정상 상태(최근 값 200, 열린 문제 없음) 확인
  2) 0~15초 무작위 대기 — 수집 주기(15s) 대비 장애 시점을 분산시켜 평균이 한쪽으로 치우치지 않게 함
  3) docker stop  → Zabbix 문제 이벤트 발생까지 = 감지 시간
  4) docker start → 문제 해소(복구 이벤트)까지 = 복구 확인 시간
시간은 Zabbix 이벤트의 서버 시각(clock + ns)으로 계산한다.
"""

import argparse
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
TIMEOUT = 120


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
    subprocess.run(["docker", *args], check=True, stdout=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--container", default="pitwall_web")
    args = parser.parse_args()

    rows = []
    with ZabbixAPI.from_env() as api:
        hostid = api.call("host.get", {"filter": {"host": [args.container]}, "output": ["hostid"]})[0]["hostid"]
        for n in range(1, args.trials + 1):
            wait_healthy(api, hostid)
            time.sleep(random.uniform(0, 15))

            docker("stop", args.container)
            t_down = time.time()
            problem = wait_for(lambda: open_problems(api, hostid), "problem")[0]
            detect = event_time(problem) - t_down

            docker("start", args.container)
            t_up = time.time()

            def resolved():
                ev = api.call("event.get", {"eventids": problem["eventid"], "output": ["r_eventid"]})[0]
                if ev["r_eventid"] != "0":
                    return api.call("event.get", {"eventids": ev["r_eventid"], "output": ["clock", "ns"]})[0]
            recovery_event = wait_for(resolved, "recovery")
            recover = event_time(recovery_event) - t_up

            rows.append((n, detect, recover))
            print(f"trial {n}: detect {detect:5.1f}s  recover {recover:5.1f}s  ({problem['name']})", flush=True)

    detects = [r[1] for r in rows]
    recovers = [r[2] for r in rows]
    print("\n| 회차 | 감지 (s) | 복구 확인 (s) |\n|---|---|---|")
    for n, d, r in rows:
        print(f"| {n} | {d:.1f} | {r:.1f} |")
    for label, values in (("감지", detects), ("복구 확인", recovers)):
        print(f"\n{label}: 평균 {statistics.mean(values):.1f}s / 중앙값 {statistics.median(values):.1f}s / "
              f"최소 {min(values):.1f}s / 최대 {max(values):.1f}s (n={len(values)})")


if __name__ == "__main__":
    main()
