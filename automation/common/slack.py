"""Slack Incoming Webhook 전송 (표준 라이브러리만 사용)."""

import json
import time
import urllib.error
import urllib.request

ATTEMPTS = 3
RETRY_DELAYS = (2, 5)  # 1→2회차, 2→3회차 대기 (초)


def post(webhook_url, text, blocks=None, timeout=10):
    """성공하면 True. 실패해도 예외를 던지지 않는다 (알림 실패가 본 작업을 막지 않도록).

    blocks 를 주면 Block Kit 으로 렌더링되고, text 는 알림 미리보기/대체 텍스트로 쓰인다.

    네트워크 오류(DNS 일시 실패 등), 429, 5xx 는 최대 3회까지 시도한다.
    실측: Docker Desktop에서 외부 DNS 조회가 순간적으로 실패([Errno -3] Try again)해
    1회만 시도하던 알림이 유실된 적이 있다 (docs/troubleshooting.md #6).
    4xx(형식 오류 등)는 재시도해도 같으므로 바로 포기한다.
    """
    if not webhook_url:
        return False
    payload = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    body = json.dumps(payload).encode()
    for attempt in range(ATTEMPTS):
        if attempt:
            time.sleep(RETRY_DELAYS[attempt - 1])
        request = urllib.request.Request(webhook_url, body, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status == 200
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                return False
        except OSError:  # URLError, 타임아웃, DNS 실패
            pass
    return False
