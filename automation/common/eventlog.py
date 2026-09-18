"""구조화 이벤트 로그 (JSON Lines) — <EVENT_LOG_DIR>/<source>.jsonl

컨테이너를 재생성하면 docker logs 는 사라진다. 장애/복구 이력은 볼륨에 남겨야
발표 증거와 사후 분석에 쓸 수 있다.

- 한 줄 = 한 이벤트. 공통 필드: v(스키마 버전), ts, epoch, source, event + 이벤트별 필드
- 쓰는 주체(source)마다 파일을 나눈다 (healer.jsonl, rca.jsonl ...).
  여러 프로세스가 한 파일을 로테이트하면 경합이 생기기 때문이다.
- 크기 기반 로테이션: <source>.jsonl → .1 → ... → .N (기본 5MB × 5개 = 최대 약 30MB/source)
- event_id 로 healer 기록과 RCA 기록을 연결해 집계한다.
- 파일 기록이 실패해도 예외를 던지지 않는다 (로깅 실패가 복구 작업을 막으면 안 된다).
  stdout(docker logs)에는 항상 같은 줄이 남는다.
"""

import json
import logging
import logging.handlers
import os
import pathlib
import time

SCHEMA_VERSION = 1
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUPS = 5


class EventLog:
    def __init__(self, source, directory=None, max_bytes=None, backups=None):
        self.source = source
        directory = pathlib.Path(directory or os.environ.get("EVENT_LOG_DIR", "/data/events"))
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{source}.jsonl"

        handler = logging.handlers.RotatingFileHandler(
            self.path,
            maxBytes=int(max_bytes or os.environ.get("EVENT_LOG_MAX_BYTES", DEFAULT_MAX_BYTES)),
            backupCount=int(backups or os.environ.get("EVENT_LOG_BACKUPS", DEFAULT_BACKUPS)),
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger = logging.getLogger(f"sys_aims.events.{source}")
        self._logger.handlers[:] = [handler]
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False

    def write(self, event, **fields):
        now = time.time()
        record = {
            "v": SCHEMA_VERSION,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
            "epoch": round(now, 3),
            "source": self.source,
            "event": event,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False)
        print(line, flush=True)  # docker logs
        self._logger.info(line)  # 볼륨 파일 (실패 시 logging이 stderr에 보고만 하고 계속 진행)
        return record
