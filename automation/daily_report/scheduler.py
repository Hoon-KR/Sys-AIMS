"""매일 REPORT_TIME(기본 08:00, TZ 기준)에 정기 보고서를 생성하는 스케줄러 (컨테이너 상주 프로세스).

cron 대신 쓰는 이유 (docs/daily-report.md)
  - 컨테이너 안에 있어 EC2에서 `compose up` 만으로 재현된다 (호스트 crontab 불필요)
  - 비root / read-only 파일시스템 유지 (busybox crond 는 root 필요)
  - 놓친 실행 따라잡기: 기동 시 오늘 보고서가 없고 예정 시각이 지났으면 즉시 생성
    (맥 절전·컨테이너 재시작으로 08:00 을 놓쳐도 보고서가 빠지지 않는다, troubleshooting #3)

정기 보고서의 기간은 항상 "전날 REPORT_TIME ~ 오늘 REPORT_TIME" 으로 고정한다 (따라잡기 실행도 동일).
"""

import os
import time
import traceback

from daily_report import report

REPORT_TIME = os.environ.get("REPORT_TIME", "08:00")
CHECK_INTERVAL = 60  # 벽시계 기준으로 매분 확인 (절전/시계 변경에도 어긋나지 않게)


def _today_at(hhmm, now=None):
    t = time.localtime(now or time.time())
    h, m = map(int, hhmm.split(":"))
    return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, h, m, 0, 0, 0, -1)))


def last_scheduled_time(now=None):
    """가장 최근에 지난 예정 시각 (오늘 것이 아직이면 어제 것)."""
    now = now or time.time()
    today = _today_at(REPORT_TIME, now)
    return today if now >= today else today - 86400


def main():
    log = report.events()
    log.write("scheduler.started", report_time=REPORT_TIME, tz=os.environ.get("TZ", "UTC"))
    while True:
        due = last_scheduled_time()
        name = report.report_name("daily", due, 24)
        if not (report.REPORTS_DIR / f"{name}.md").exists():
            late = int(time.time() - due)
            log.write("scheduler.run", name=name, late_s=late, catch_up=late > 2 * CHECK_INTERVAL)
            try:
                report.generate(kind="daily", end=due, hours=24)
            except Exception as e:  # 한 번의 실패가 스케줄러를 죽이면 안 된다 (다음 확인 때 재시도)
                log.write("scheduler.error", name=name, error=f"{type(e).__name__}: {e}"[:300],
                          trace=traceback.format_exc()[-800:])
                time.sleep(300)  # 실패 시 5분 뒤 재시도 (일일 한도가 반복 호출을 추가로 막는다)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
