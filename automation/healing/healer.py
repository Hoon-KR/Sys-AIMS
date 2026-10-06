"""Sys-AIMS healer — Zabbix Action(webhook)의 재기동 요청을 받아 컨테이너를 재기동한다.

Docker에는 socket-proxy를 통해서만 접근한다 (ADR-0001).
프록시가 이미 TARGET_CONTAINER 의 restart/inspect 만 허용하지만,
healer 코드에서도 같은 제한을 한 번 더 건다 (심층 방어).

여러 서버(계열사)의 컨테이너를 재기동한다 (ADR-0002)
  대상은 HEAL_TARGETS 로 주입한다. 각 대상은 자기 socket-proxy 주소를 가지며,
  라우팅 키는 "<company>/<container>" 다. 목록에 없는 조합은 403 으로 거부한다.
  **서킷 브레이커는 대상별로 분리한다** — 계열사 A 의 플래핑이 B 의 자동 복구를
  막아서는 안 된다 (단일 서버 시절에는 전역 하나였다).

엔드포인트
  POST /heal    {"company", "container", "event_id", "host", "trigger"}  (Bearer 토큰 필요)
                company 가 없으면 "internal"(감시 서버 자신)로 본다
  POST /reset   서킷 수동 해제. 본문 {"company","container"} 가 있으면 그 대상만,
                없으면 전체                                   (Bearer 토큰 필요)
  GET  /status  대상별 서킷/이력 조회
  GET  /health  헬스체크

모든 요청/결과/서킷 변화는 <EVENT_LOG_DIR>/healer.jsonl 에도 남는다 (common.eventlog).
컨테이너를 재생성해도 이력이 유지된다.

RCA 연동 (docs/rca.md)
  재기동 "전"에 컨테이너 상태와 직전 실행 구간 로그를 스냅샷으로 뜨고,
  재기동과 Zabbix 응답이 끝난 "뒤" 백그라운드로 rca 서비스에 넘긴다.
  - 복구가 우선: 스냅샷/전달이 실패해도 재기동은 그대로 진행한다.
  - 서킷 열림, 쿨다운, 허용되지 않은 컨테이너는 RCA도 하지 않는다.
  - OpenAI 키는 healer에 없다 (rca 서비스에만 있다).

응답 코드
  200 재기동 + healthy 확인 완료   403 허용되지 않은 컨테이너   401 인증 실패
  409 쿨다운 중(중복 요청)          429 서킷 열림(자동 복구 중단)
  502 재기동 실패 또는 제한 시간 내 healthy 미도달
"""

import datetime
import hmac
import http.client
import json
import os
import pathlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from common import slack
from common.eventlog import EventLog

TARGET_CONTAINER = os.environ["TARGET_CONTAINER"]
TOKEN = os.environ["HEALER_TOKEN"]
MAX_RESTARTS = int(os.environ.get("HEALER_MAX_RESTARTS", "3"))
WINDOW_SECONDS = int(os.environ.get("HEALER_WINDOW_SECONDS", "600"))
COOLDOWN_SECONDS = int(os.environ.get("HEALER_COOLDOWN_SECONDS", "60"))
VERIFY_TIMEOUT = int(os.environ.get("HEALER_VERIFY_TIMEOUT", "30"))
PROXY_HOST = os.environ.get("DOCKER_PROXY_HOST", "socket-proxy")
PROXY_PORT = int(os.environ.get("DOCKER_PROXY_PORT", "2375"))
# 감시 서버 자신의 컨테이너를 가리키는 계열사 키 (company 태그가 없을 때의 기본값)
DEFAULT_COMPANY = "internal"
# 로컬 Docker Desktop(API 1.40~1.56)과 EC2 Ubuntu 의 docker-ce 가 모두 지원하는 버전
DOCKER_API = "/v1.44"
STATE_FILE = pathlib.Path(os.environ.get("STATE_FILE", "/data/state.json"))
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
RCA_HOST = os.environ.get("RCA_HOST", "rca")
RCA_PORT = int(os.environ.get("RCA_PORT", "8081"))
RCA_TOKEN = os.environ.get("RCA_TOKEN", "")
SNAPSHOT_TIMEOUT = 3        # 스냅샷이 재기동을 늦추지 않도록 짧게
SNAPSHOT_TAIL = 2000        # 직전 실행 구간에서 가져올 최대 줄 수 (전송량 상한 ≈ 150KB)

_lock = threading.Lock()
EVENTS = EventLog("healer")


# ---------------------------------------------------------------- 대상 레지스트리
def load_targets():
    """HEAL_TARGETS(JSON) → {"<company>/<container>": {...}}.

    예: [{"company":"A","container":"pitwall_web","proxy":"172.31.9.142:2375"}]

    비어 있으면 단일 대상(감시 서버 자신)으로 동작한다. VM 분리 전 구성과
    로컬 개발에서 환경변수를 추가하지 않아도 그대로 돌아가게 하기 위함이다.
    형식이 잘못되면 기동 시점에 예외로 죽는다 — 잘못된 라우팅으로 엉뚱한
    서버를 재기동하는 것보다 안전하다.
    """
    raw = os.environ.get("HEAL_TARGETS", "").strip()
    if not raw:
        return {f"{DEFAULT_COMPANY}/{TARGET_CONTAINER}": {
            "company": DEFAULT_COMPANY, "container": TARGET_CONTAINER,
            "proxy_host": PROXY_HOST, "proxy_port": PROXY_PORT}}

    targets = {}
    for entry in json.loads(raw):
        company = (entry.get("company") or DEFAULT_COMPANY).strip()
        container = (entry.get("container") or "").strip()
        if not container:
            raise ValueError(f"HEAL_TARGETS entry has no container: {entry}")
        host, _, port = (entry.get("proxy") or f"{PROXY_HOST}:{PROXY_PORT}").strip().partition(":")
        key = f"{company}/{container}"
        if key in targets:
            raise ValueError(f"HEAL_TARGETS has duplicate target: {key}")
        targets[key] = {"company": company, "container": container,
                        "proxy_host": host, "proxy_port": int(port or PROXY_PORT)}
    if not targets:
        raise ValueError("HEAL_TARGETS is empty")
    return targets


TARGETS = load_targets()


def resolve_target(request):
    """요청의 (company, container) 로 대상을 찾는다. 목록에 없으면 None (→ 403).

    프록시가 이미 컨테이너를 제한하지만, 여기서도 조합을 확인한다 (심층 방어).

    company 가 없는 요청은 **대상이 하나뿐일 때만** 받는다. 대상이 여럿인데
    company 를 추측하면, 태그가 빠진 VM 호스트의 장애로 **감시 서버 자신의**
    컨테이너를 재기동할 수 있다. 엉뚱한 서버를 건드리는 것이 최악이므로
    모르면 거부한다 (fail closed).
    """
    company = (request.get("company") or "").strip()
    container = (request.get("container") or "").strip()
    if not company:
        if len(TARGETS) != 1:
            return None
        only = next(iter(TARGETS.values()))
        return only if only["container"] == container else None
    return TARGETS.get(f"{company}/{container}")


def target_key(target):
    return f"{target['company']}/{target['container']}"


def log(event, **fields):
    EVENTS.write(event, **fields)


# ---------------------------------------------------------------- state
# 서킷 상태는 파일에 저장한다. healer가 재시작되어도 서킷이 풀리지 않게 하기 위함.
# 형식: {"targets": {"<company>/<container>": {"attempts": [...], "circuit_open": bool, "opened_at": float}}}
def blank_target_state():
    return {"attempts": [], "circuit_open": False, "opened_at": None}


def load_state():
    try:
        raw = json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"targets": {}}
    if isinstance(raw.get("targets"), dict):
        return raw
    # 구버전(단일 대상, 전역 서킷) 형식 → internal 대상으로 이관한다.
    # 이관하지 않으면 열려 있던 서킷이 조용히 풀려 무한 재기동이 가능해진다.
    return {"targets": {f"{DEFAULT_COMPANY}/{TARGET_CONTAINER}": {
        "attempts": raw.get("attempts") or [],
        "circuit_open": bool(raw.get("circuit_open")),
        "opened_at": raw.get("opened_at"),
    }}}


def target_state(state, key):
    return state["targets"].setdefault(key, blank_target_state())


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(STATE_FILE)


# ---------------------------------------------------------------- docker (via proxy)
def docker(target, method, path, timeout=None, raw=False):
    """대상의 socket-proxy 로만 요청한다. 대상마다 주소가 다르다 (VM-A / VM-B / 자기 자신)."""
    conn = http.client.HTTPConnection(target["proxy_host"], target["proxy_port"],
                                      timeout=timeout or VERIFY_TIMEOUT + 10)
    try:
        conn.request(method, DOCKER_API + path)
        response = conn.getresponse()
        body = response.read()
        return response.status, body if raw else body.decode(errors="replace")
    finally:
        conn.close()


def demux_logs(raw):
    """TTY가 아닌 컨테이너의 로그 스트림은 8바이트 헤더(stream, size)로 다중화되어 있다."""
    out, i = bytearray(), 0
    while i + 8 <= len(raw):
        size = int.from_bytes(raw[i + 4:i + 8], "big")
        out += raw[i + 8:i + 8 + size]
        i += 8 + size
    return out.decode(errors="replace").splitlines()


def container_state(target):
    status, body = docker(target, "GET", f"/containers/{target['container']}/json")
    if status != 200:
        return None
    state = json.loads(body)["State"]
    return {"status": state["Status"], "health": (state.get("Health") or {}).get("Status")}


def restart_and_verify(target):
    status, body = docker(target, "POST", f"/containers/{target['container']}/restart?t=5")
    if status != 204:
        return False, f"restart API returned {status}: {body.strip()[:200]}"
    deadline = time.time() + VERIFY_TIMEOUT
    last = None
    while time.time() < deadline:
        last = container_state(target)
        # healthcheck가 없는 컨테이너는 running이면 성공으로 본다
        if last and last["status"] == "running" and last["health"] in (None, "healthy"):
            return True, f"running/{last['health'] or 'no-healthcheck'}"
        time.sleep(1)
    return False, f"not healthy within {VERIFY_TIMEOUT}s (last={last})"


# ---------------------------------------------------------------- RCA snapshot / handoff
def _epoch(docker_ts):
    """'2026-09-18T10:43:52.649744417Z' → epoch 초 (소수점 이하 버림)."""
    return int(datetime.datetime.fromisoformat(docker_ts[:19]).replace(tzinfo=datetime.timezone.utc).timestamp())


def _ts_key(docker_ts):
    """RFC3339Nano 를 나노초까지 비교 가능한 문자열로 (소수부 길이가 제각각이라 9자리로 맞춘다)."""
    base, _, frac = docker_ts.rstrip("Z").partition(".")
    return f"{base}.{frac.ljust(9, '0')[:9]}"


def logs_since_start(raw_lines, started_at):
    """timestamps=1 로 받은 줄에서 started_at 이후 줄만 남기고 타임스탬프를 뗀다.

    Docker API 의 since 는 초 단위라서, 재기동이 같은 초 안에 일어나면(docker restart 에서 흔함)
    이전 실행의 종료 로그(SIGQUIT 등)가 섞인다. 실제로 이 때문에 RCA가 원인을 '외부 종료'로 잘못 분류했다.
    """
    start, kept = _ts_key(started_at), []
    for line in raw_lines:
        stamp, _, text = line.partition(" ")
        if _ts_key(stamp) >= start:
            kept.append(text)
    return kept


def snapshot(target):
    """재기동 직전 상태 + 직전 실행 구간 로그. 어떤 실패도 예외로 올리지 않는다."""
    started = time.time()
    name = target["container"]
    try:
        status, body = docker(target, "GET", f"/containers/{name}/json", timeout=SNAPSHOT_TIMEOUT)
        if status != 200:
            return {"error": f"inspect HTTP {status}"}, [], round(time.time() - started, 2)
        s = json.loads(body)["State"]
        state = {"status": s["Status"], "exit_code": s["ExitCode"], "oom_killed": s["OOMKilled"],
                 "error_message": s.get("Error") or None, "started_at": s["StartedAt"], "finished_at": s["FinishedAt"]}
        since = _epoch(s["StartedAt"])
        status, raw = docker(target, "GET",
                             f"/containers/{name}/logs?stdout=1&stderr=1&timestamps=1&since={since}&tail={SNAPSHOT_TAIL}",
                             timeout=SNAPSHOT_TIMEOUT, raw=True)
        logs = logs_since_start(demux_logs(raw), s["StartedAt"]) if status == 200 else []
        if status != 200:
            state["logs_error"] = f"logs HTTP {status}"
        return state, logs, round(time.time() - started, 2)
    except Exception as e:  # 복구가 우선: 스냅샷 실패는 기록만 한다
        return {"error": f"{type(e).__name__}: {e}"[:200]}, [], round(time.time() - started, 2)


def handoff_to_rca(job):
    """재기동이 끝난 뒤 백그라운드 스레드에서 실행된다. 실패해도 healer 동작에 영향 없음."""
    ctx = {"event_id": job.get("event_id"), "container": job.get("container"), "company": job.get("company")}
    if not RCA_TOKEN:
        return log("rca.handoff_skipped", reason="RCA_TOKEN not set", **ctx)
    try:
        conn = http.client.HTTPConnection(RCA_HOST, RCA_PORT, timeout=3)
        conn.request("POST", "/analyze", json.dumps(job, ensure_ascii=False).encode(),
                     {"Content-Type": "application/json", "Authorization": f"Bearer {RCA_TOKEN}"})
        status = conn.getresponse().status
        conn.close()
        log("rca.handoff", status=status, log_lines=len(job.get("logs") or []), **ctx)
    except Exception as e:
        log("rca.handoff_failed", error=f"{type(e).__name__}: {e}"[:200], **ctx)


# ---------------------------------------------------------------- heal
def heal(request):
    target = resolve_target(request)
    ctx = {"company": (request.get("company") or "").strip() or None,
           "container": request.get("container", ""),
           "event_id": request.get("event_id"), "host": request.get("host")}

    if target is None:
        log("heal.rejected", reason="target not allowed", **ctx)
        return 403, {"result": "rejected",
                     "reason": f"target '{ctx['company'] or '(no company tag)'}/{ctx['container']}' is not allowed"}

    key = target_key(target)
    ctx["company"] = target["company"]
    ctx["proxy"] = f"{target['proxy_host']}:{target['proxy_port']}"

    with _lock:
        state = load_state()
        ts = target_state(state, key)
        now = time.time()
        # 서킷은 대상별이다. A 가 막혀도 B 의 자동 복구는 계속되어야 한다.
        if ts["circuit_open"]:
            log("heal.blocked", reason="circuit open", **ctx)
            return 429, {"result": "blocked",
                         "reason": f"circuit open for {key} — manual reset required (POST /reset)"}

        recent = [a for a in ts["attempts"] if now - a["at"] < WINDOW_SECONDS]
        if recent and now - recent[-1]["at"] < COOLDOWN_SECONDS:
            log("heal.skipped", reason="cooldown", since=round(now - recent[-1]["at"], 1), **ctx)
            return 409, {"result": "skipped",
                         "reason": f"restarted {now - recent[-1]['at']:.0f}s ago (cooldown {COOLDOWN_SECONDS}s)"}

        if len(recent) >= MAX_RESTARTS:
            ts["circuit_open"] = True
            ts["opened_at"] = now
            ts["attempts"] = recent
            save_state(state)
            notified = slack.post(SLACK_WEBHOOK_URL,
                                  f":rotating_light: *[Sys-AIMS] 자동 복구 중단 — 사람 개입 필요*\n"
                                  f"계열사 `{target['company']}` 의 `{target['container']}` 가 "
                                  f"{WINDOW_SECONDS // 60}분 안에 {len(recent)}회 재기동되었지만 계속 실패합니다.\n"
                                  f"원인 확인 후 해당 대상의 서킷을 수동 해제하세요 "
                                  f"(POST /reset {{\"company\":\"{target['company']}\","
                                  f"\"container\":\"{target['container']}\"}}). event_id={ctx['event_id']}")
            log("circuit.opened", restarts_in_window=len(recent), window_seconds=WINDOW_SECONDS,
                slack_notified=notified, **ctx)
            return 429, {"result": "blocked",
                         "reason": f"{len(recent)} restarts within {WINDOW_SECONDS}s — circuit opened for {key}"}

        # 재기동 "전" 스냅샷: 재기동 후에는 새 프로세스의 기동 로그가 섞인다
        container_state, logs, snapshot_s = snapshot(target)
        log("rca.snapshot", snapshot_s=snapshot_s, log_lines=len(logs),
            state_error=container_state.get("error"), **ctx)

        started = time.time()
        ok, detail = restart_and_verify(target)
        elapsed = round(time.time() - started, 1)
        ts["attempts"] = recent + [{"company": target["company"], "container": target["container"],
                                    "at": started, "ok": ok, "event_id": ctx["event_id"]}]
        save_state(state)
        attempt = len(ts["attempts"])

    heal_result = {"result": "restarted" if ok else "failed", "detail": detail, "elapsed_s": elapsed}
    # 재기동 성공/실패와 무관하게 RCA 수행. Zabbix 응답을 늦추지 않도록 백그라운드로 넘긴다.
    threading.Thread(target=handoff_to_rca, daemon=True, args=({
        **ctx, "trigger": request.get("trigger"), "container_state": container_state,
        "logs": logs, "heal": heal_result,
    },)).start()

    if ok:
        log("heal.succeeded", elapsed_s=elapsed, detail=detail, attempt=attempt, **ctx)
        return 200, {"result": "restarted", "elapsed_s": elapsed, "detail": detail}
    notified = slack.post(SLACK_WEBHOOK_URL,
                          f":x: *[Sys-AIMS] 자동 복구 실패*\n계열사 `{target['company']}` 의 "
                          f"`{target['container']}` 재기동 후 정상화 확인 실패: {detail}\nevent_id={ctx['event_id']}")
    log("heal.failed", elapsed_s=elapsed, detail=detail, slack_notified=notified, **ctx)
    return 502, {"result": "failed", "elapsed_s": elapsed, "detail": detail}


def reset(request=None):
    """본문에 (company, container) 가 있으면 그 대상만, 없으면 전체를 해제한다.

    전체 해제를 남겨두는 이유: 측정 스크립트(scripts/measure_detection.py)가
    회차마다 본문 없이 /reset 을 호출한다.
    """
    request = request or {}
    scoped = request.get("container")
    with _lock:
        state = load_state()
        if scoped:
            target = resolve_target(request)
            if target is None:
                return 404, {"result": "unknown target",
                             "target": f"{request.get('company') or DEFAULT_COMPANY}/{scoped}"}
            key = target_key(target)
            was_open = bool(state["targets"].get(key, {}).get("circuit_open"))
            state["targets"][key] = blank_target_state()
            save_state(state)
            log("circuit.reset", company=target["company"], container=target["container"], was_open=was_open)
            return 200, {"result": "reset", "target": key, "was_open": was_open}

        was_open = sorted(k for k, v in state["targets"].items() if v.get("circuit_open"))
        save_state({"targets": {}})
    log("circuit.reset", scope="all", was_open=was_open)
    return 200, {"result": "reset", "scope": "all", "was_open": was_open}


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
            return self._send(200, {
                "targets": {key: {**t, "state": state["targets"].get(key, blank_target_state())}
                            for key, t in sorted(TARGETS.items())},
                "max_restarts": MAX_RESTARTS, "window_seconds": WINDOW_SECONDS,
                "cooldown_seconds": COOLDOWN_SECONDS})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/heal", "/reset"):
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            log("auth.failed", path=self.path, client=self.client_address[0])
            return self._send(401, {"error": "unauthorized"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "invalid json"})
        if self.path == "/reset":
            return self._send(*reset(request))
        log("heal.requested", company=request.get("company"), container=request.get("container"),
            event_id=request.get("event_id"), host=request.get("host"))
        self._send(*heal(request))

    def log_message(self, *args):  # 기본 access log 대신 구조화 로그만 남긴다
        pass


def main():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state = load_state()
    log("healer.started", event_log=str(EVENTS.path),
        targets={key: f"{t['proxy_host']}:{t['proxy_port']}" for key, t in sorted(TARGETS.items())},
        max_restarts=MAX_RESTARTS, window_seconds=WINDOW_SECONDS, cooldown_seconds=COOLDOWN_SECONDS,
        circuits_open=sorted(k for k, v in state["targets"].items() if v.get("circuit_open")))
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()
