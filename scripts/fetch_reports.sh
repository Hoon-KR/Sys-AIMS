#!/usr/bin/env sh
# reporter 볼륨(reports_data)의 보고서를 로컬 ./reports/ 로 복사한다 (gitignore 대상).
# 사용: sh scripts/fetch_reports.sh  →  open reports/daily-YYYY-MM-DD.html
set -eu
cd "$(dirname "$0")/.."
docker cp reporter:/reports/. ./reports/
rm -rf ./reports/.state ./reports/.events
ls -1 reports | grep -vE '^\.gitkeep$'
