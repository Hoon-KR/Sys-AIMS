"""Slack Incoming Webhook 전송 (표준 라이브러리만 사용)."""

import json
import urllib.request


def post(webhook_url, text, timeout=10):
    """성공하면 True. 실패해도 예외를 던지지 않는다 (알림 실패가 본 작업을 막지 않도록)."""
    if not webhook_url:
        return False
    request = urllib.request.Request(
        webhook_url, json.dumps({"text": text}).encode(), {"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except OSError:
        return False
