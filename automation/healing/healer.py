"""Sys-AIMS healer — Zabbix Action(webhook)의 재기동 요청을 받아 컨테이너를 재기동한다.

Docker에는 socket-proxy를 통해서만 접근한다 (ADR-0001).
프록시가 이미 TARGET_CONTAINER 의 restart/inspect 만 허용하지만,
healer 코드에서도 같은 제한을 한 번 더 건다 (심층 방어).

엔드포인트
  POST /heal    {"container", "event_id", "host", "trigger"}  (Bearer 토큰 필요)
  POST /reset   서킷 수동 해제                                  (Bearer 토큰 필요)
  GET  /status  서킷/이력 조회
  GET  /health  헬스체크

응답 코드
  200 재기동 + healthy 확인 완료   403 허용되지 않은 컨테이너   401 인증 실패
  409 쿨다운 중(중복 요청)          429 서킷 열림(자동 복구 중단)
  502 재기동 실패 또는 제한 시간 내 healthy 미도달
"""

import hmac
import http.client
import json
import os
import pathlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from common import slack

TARGET_CONTAINER = os.environ["TARGET_CONTAINER"]
TOKEN = os.environ["HEALER_TOKEN"]
MAX_RESTARTS = int(os.environ.get("HEALER_MAX_RESTARTS", "3"))
WINDOW_SECONDS = int(os.environ.get("HEALER_WINDOW_SECONDS", "600"))
COOLDOWN_SECONDS = int(os.environ.get("HEALER_COOLDOWN_SECONDS", "60"))
VERIFY_TIMEOUT = int(os.environ.get("HEALER_VERIFY_TIMEOUT", "30"))
PROXY_HOST = os.environ.get("DOCKER_PROXY_HOST", "socket-proxy")
PROXY_PORT = int(os.environ.get("DOCKER_PROXY_PORT", "2375"))
# 로컬 Docker(API 1.40~1.56)와 Amazon Linux 2023 기본 Docker 25(API 1.44)가 모두 지원하는 버전
DOCKER_API = "/v1.44"
STATE_FILE = pathlib.Path(os.environ.get("STATE_FILE", "/data/state.json"))
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

_lock = threading.Lock()


def log(event, **fields):
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields}, ensure_ascii=False), flush=True)


# ---------------------------------------------------------------- state
# 서킷 상태는 파일에 저장한다. healer가 재시작되어도 서킷이 풀리지 않게 하기 위함.
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"attempts": [], "circuit_open": False, "opened_at": None}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(STATE_FILE)


# ---------------------------------------------------------------- docker (via proxy)
def docker(method, path):
    conn = http.client.HTTPConnection(PROXY_HOST, PROXY_PORT, timeout=VERIFY_TIMEOUT + 10)
    try:
        conn.request(method, DOCKER_API + path)
        response = conn.getresponse()
        return response.status, response.read().decode(errors="replace")
    finally:
        conn.close()


def container_state(name):
    status, body = docker("GET", f"/containers/{name}/json")
    if status != 200:
        return None
    state = json.loads(body)["State"]
    return {"status": state["Status"], "health": (state.get("Health") or {}).get("Status")}


def restart_and_verify(name):
    status, body = docker("POST", f"/containers/{name}/restart?t=5")
    if status != 204:
        return False, f"restart API returned {status}: {body.strip()[:200]}"
    deadline = time.time() + VERIFY_TIMEOUT
    last = None
    while time.time() < deadline:
        last = container_state(name)
        # healthcheck가 없는 컨테이너는 running이면 성공으로 본다
        if last and last["status"] == "running" and last["health"] in (None, "healthy"):
            return True, f"running/{last['health'] or 'no-healthcheck'}"
        time.sleep(1)
    return False, f"not healthy within {VERIFY_TIMEOUT}s (last={last})"


# ---------------------------------------------------------------- heal
def heal(request):
    container = request.get("container", "")
    ctx = {"container": container, "event_id": request.get("event_id"), "host": request.get("host")}

    if container != TARGET_CONTAINER:
        log("heal.rejected", reason="container not allowed", **ctx)
        return 403, {"result": "rejected", "reason": f"container '{container}' is not allowed"}

    with _lock:
        state = load_state()
        now = time.time()
        if state["circuit_open"]:
            log("heal.blocked", reason="circuit open", **ctx)
            return 429, {"result": "blocked", "reason": "circuit open — manual reset required (POST /reset)"}

        recent = [a for a in state["attempts"] if a["container"] == container and now - a["at"] < WINDOW_SECONDS]
        if recent and now - recent[-1]["at"] < COOLDOWN_SECONDS:
            log("heal.skipped", reason="cooldown", since=round(now - recent[-1]["at"], 1), **ctx)
            return 409, {"result": "skipped", "reason": f"restarted {now - recent[-1]['at']:.0f}s ago (cooldown {COOLDOWN_SECONDS}s)"}

        if len(recent) >= MAX_RESTARTS:
            state["circuit_open"] = True
            state["opened_at"] = now
            save_state(state)
            notified = slack.post(SLACK_WEBHOOK_URL,
                                  f":rotating_light: *[Sys-AIMS] 자동 복구 중단 — 사람 개입 필요*\n"
                                  f"`{container}` 가 {WINDOW_SECONDS // 60}분 안에 {len(recent)}회 재기동되었지만 계속 실패합니다.\n"
                                  f"원인 확인 후 healer 서킷을 수동 해제하세요 (POST /reset). event_id={ctx['event_id']}")
            log("circuit.opened", restarts_in_window=len(recent), window_seconds=WINDOW_SECONDS, slack_notified=notified, **ctx)
            return 429, {"result": "blocked", "reason": f"{len(recent)} restarts within {WINDOW_SECONDS}s — circuit opened"}

        started = time.time()
        ok, detail = restart_and_verify(container)
        elapsed = round(time.time() - started, 1)
        state["attempts"] = recent + [{"container": container, "at": started, "ok": ok, "event_id": ctx["event_id"]}]
        save_state(state)

    if ok:
        log("heal.succeeded", elapsed_s=elapsed, detail=detail, attempt=len(state["attempts"]), **ctx)
        return 200, {"result": "restarted", "elapsed_s": elapsed, "detail": detail}
    notified = slack.post(SLACK_WEBHOOK_URL,
                          f":x: *[Sys-AIMS] 자동 복구 실패*\n`{container}` 재기동 후 정상화 확인 실패: {detail}\nevent_id={ctx['event_id']}")
    log("heal.failed", elapsed_s=elapsed, detail=detail, slack_notified=notified, **ctx)
    return 502, {"result": "failed", "elapsed_s": elapsed, "detail": detail}


def reset():
    with _lock:
        state = load_state()
        was_open = state["circuit_open"]
        save_state({"attempts": [], "circuit_open": False, "opened_at": None})
    log("circuit.reset", was_open=was_open)
    return 200, {"result": "reset", "was_open": was_open}


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = "sys-aims-healer"

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
        return hmac.compare_digest(supplied.encode(), TOKEN.encode())

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True})
        if self.path == "/status":
            state = load_state()
            return self._send(200, {**state, "target": TARGET_CONTAINER, "max_restarts": MAX_RESTARTS,
                                    "window_seconds": WINDOW_SECONDS, "cooldown_seconds": COOLDOWN_SECONDS})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/heal", "/reset"):
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            log("auth.failed", path=self.path, client=self.client_address[0])
            return self._send(401, {"error": "unauthorized"})
        if self.path == "/reset":
            return self._send(*reset())
        try:
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "invalid json"})
        log("heal.requested", container=request.get("container"), event_id=request.get("event_id"), host=request.get("host"))
        self._send(*heal(request))

    def log_message(self, *args):  # 기본 access log 대신 구조화 로그만 남긴다
        pass


def main():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    log("healer.started", target=TARGET_CONTAINER, max_restarts=MAX_RESTARTS,
        window_seconds=WINDOW_SECONDS, cooldown_seconds=COOLDOWN_SECONDS, circuit_open=load_state()["circuit_open"])
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()
