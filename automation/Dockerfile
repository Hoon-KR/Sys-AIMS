# Sys-AIMS automation 이미지 — healer / rca 가 같은 이미지를 쓰고 실행 명령만 다르다
# 표준 라이브러리만 사용하므로 pip 설치 단계가 없다.
FROM python:3.14.7-alpine3.24

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
