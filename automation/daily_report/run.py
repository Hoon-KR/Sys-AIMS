"""보고서 즉시 생성 (개발·시연용). 정기 실행은 daily_report.scheduler 가 한다.

  docker exec reporter python -m daily_report.run                 # 직전 24시간, 수동 실행 보고서
  docker exec reporter python -m daily_report.run --hours 6       # 직전 6시간
  docker exec reporter python -m daily_report.run --daily         # 오늘 정기 보고서를 지금 생성 (REPORT_TIME 기준 24시간)
  docker exec reporter python -m daily_report.run --no-ai --no-slack

수동 실행 보고서는 adhoc-*.md 로 저장되어 정기 보고서(daily-*.md)를 덮어쓰지 않는다.
"""

import argparse
import json
import time

from daily_report import report, scheduler


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", type=float, default=24, help="기간 (기본 24시간, 끝 = 지금)")
    parser.add_argument("--daily", action="store_true", help="정기 보고서 규칙(REPORT_TIME 기준 24시간)으로 생성")
    parser.add_argument("--no-ai", action="store_true", help="AI 호출 없이 표만 생성 (비용 0)")
    parser.add_argument("--no-slack", action="store_true", help="Slack 알림 생략")
    parser.add_argument("--zabbix-url", help="Zabbix API URL 재정의 (검증용)")
    args = parser.parse_args()

    if args.daily:
        kind, end, hours = "daily", scheduler.last_scheduled_time(), 24
    else:
        kind, end, hours = "adhoc", int(time.time()), args.hours
    name, record = report.generate(kind=kind, end=end, hours=hours, use_ai=not args.no_ai,
                                   notify=not args.no_slack, zabbix_url=args.zabbix_url)
    print(json.dumps({k: record[k] for k in ("name", "coverage_pct", "overall_status", "ai_error", "errors",
                                            "number_check", "usage", "elapsed_s", "slack_notified")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
