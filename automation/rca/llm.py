"""OpenAI Responses API 호출 (표준 라이브러리만 사용).

- 타임아웃/429/5xx 는 1회 재시도, 4xx(키/모델/요청 오류)는 재시도하지 않는다.
- 응답은 JSON 스키마(strict)로 받는다.
- 오류 메시지에서 API 키를 반드시 제거한다. 요청 헤더는 어디에도 기록하지 않는다.
"""

import json
import re
import socket
import time
import urllib.error
import urllib.request

API_URL = "https://api.openai.com/v1/responses"
RETRY_DELAY = 3
_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")


class LLMError(Exception):
    def __init__(self, kind, message, retryable=False):
        super().__init__(message)
        self.kind = kind  # timeout | http_<code> | incomplete | invalid_output | network
        self.retryable = retryable


def _scrub(text, api_key):
    text = text.replace(api_key, "[REDACTED]") if api_key else text
    return _KEY_PATTERN.sub("sk-[REDACTED]", text)


def _post(body, api_key, timeout):
    request = urllib.request.Request(
        API_URL, json.dumps(body).encode(), {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("error", {}).get("message", detail)
        except (json.JSONDecodeError, AttributeError):
            pass
        raise LLMError(f"http_{e.code}", _scrub(str(detail)[:300], api_key), retryable=e.code == 429 or e.code >= 500)
    except (TimeoutError, socket.timeout):
        raise LLMError("timeout", f"no response within {timeout}s", retryable=True)
    except urllib.error.URLError as e:
        if isinstance(e.reason, (TimeoutError, socket.timeout)):
            raise LLMError("timeout", f"no response within {timeout}s", retryable=True)
        raise LLMError("network", _scrub(str(e.reason)[:300], api_key), retryable=True)


def analyze(*, api_key, model, instructions, user_input, schema, max_output_tokens, timeout, reasoning_effort="low"):
    body = {
        "model": model,
        "instructions": instructions,
        "input": user_input,
        "max_output_tokens": max_output_tokens,
        "reasoning": {"effort": reasoning_effort},
        "text": {"format": {"type": "json_schema", "name": "rca", "strict": True, "schema": schema}},
    }
    attempts, started = 0, time.time()
    while True:
        attempts += 1
        try:
            response = _post(body, api_key, timeout)
            break
        except LLMError as e:
            if e.retryable and attempts < 2:
                time.sleep(RETRY_DELAY)
                continue
            e.attempts, e.latency_s = attempts, round(time.time() - started, 1)
            raise

    meta = {"attempts": attempts, "latency_s": round(time.time() - started, 1), "usage": response.get("usage", {}),
            "response_id": response.get("id"), "model": response.get("model", model)}
    if response.get("status") != "completed":
        error = LLMError("incomplete", f"status={response.get('status')} {response.get('incomplete_details')}")
        error.attempts, error.latency_s, error.usage = attempts, meta["latency_s"], meta["usage"]
        raise error

    texts = [c.get("text", "") for o in response.get("output", []) if o.get("type") == "message"
             for c in o.get("content", []) if c.get("type") == "output_text"]
    try:
        result = json.loads("".join(texts))
    except json.JSONDecodeError:
        error = LLMError("invalid_output", "response is not valid JSON")
        error.attempts, error.latency_s, error.usage = attempts, meta["latency_s"], meta["usage"]
        raise error
    return result, meta
