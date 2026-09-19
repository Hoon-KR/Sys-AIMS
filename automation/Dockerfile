# Sys-AIMS automation 이미지
#   base     : healer / rca (표준 라이브러리만 사용, 외부 패키지 없음)
#   reporter : base + markdown (해시 고정) + daily_report
FROM python:3.14.7-alpine3.24 AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 비root 실행. /data 는 named volume으로 마운트되며 이 소유권을 이어받는다.
RUN adduser -D -H -u 10001 app && mkdir /data && chown app:app /data

WORKDIR /app
COPY common/ common/
COPY healing/ healing/
COPY rca/ rca/
COPY prompts/ prompts/

USER app
EXPOSE 8080
CMD ["python", "-m", "healing.healer"]


FROM base AS reporter
USER root
COPY requirements/reporter.txt /tmp/reporter.txt
RUN pip install --no-cache-dir --require-hashes --no-deps -r /tmp/reporter.txt && rm /tmp/reporter.txt \
    && mkdir /reports && chown app:app /reports
COPY daily_report/ daily_report/
USER app
CMD ["python", "-m", "daily_report.scheduler"]
