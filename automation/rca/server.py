"""Sys-AIMS rca — 장애 직전 로그를 OpenAI로 분석해 Slack으로 보낸다.

권한 분리 (ADR-0001과 같은 원칙)
  healer: Docker 접근 O, OpenAI 키 X  →  재기동 전에 로그 스냅샷을 떠서 이 서비스로 넘긴다
  rca   : OpenAI 키 O, Docker 접근 X  →  받은 스냅샷만 분석한다

엔드포인트
  POST /analyze  (Bearer RCA_TOKEN)  → 202 대기열 등록 / 503 대기열 가득 참
  GET  /health

비용 상한 (근거: docs/rca.md)
  로그: 노이즈 압축 후 RCA_MAX_LOG_CHARS 자 (최신 줄 우선)
  출력: RCA_MAX_OUTPUT_TOKENS (추론 토큰 포함)
  횟수: 하루 RCA_MAX_PER_DAY 회 (OpenAI 호출 시도 기준, 볼륨에 저장)
  동시성: 작업자 1개, 대기열 3개

결과는 /data/events/rca.jsonl 에 event_id 와 함께 남는다 (healer.jsonl 과 event_id 로 연결).
"""

import hmac
import json
import os
import pathlib
import queue
import string
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from common import slack
from common.eventlog import EventLog
from rca import llm
from rca.compress import compress, redact

TOKEN = os.environ["RCA_TOKEN"]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "")
OPENAI_TIMEOUT = int(os.environ.get("RCA_OPENAI_TIMEOUT", "20"))
MAX_PER_DAY = int(os.environ.get("RCA_MAX_PER_DAY", "20"))
MAX_LOG_CHARS = int(os.environ.get("RCA_MAX_LOG_CHARS", "12000"))
MAX_OUTPUT_TOKENS = int(os.environ.get("RCA_MAX_OUTPUT_TOKENS", "1200"))
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
STATE_FILE = pathlib.Path(os.environ.get("RCA_STATE_FILE", "/data/rca_state.json"))
PROMPTS = pathlib.Path(os.environ.get("RCA_PROMPTS_DIR", pathlib.Path(__file__).resolve().parents[1] / "prompts"))
FALLBACK_LINES = 5

SYSTEM_PROMPT = (PROMPTS / "rca_system.md").read_text()
USER_TEMPLATE = string.Template((PROMPTS / "rca_user.md").read_text())
SCHEMA = json.loads((PROMPTS / "rca_schema.json").read_text())

EVENTS = EventLog("rca")
JOBS = queue.Queue(maxsize=3)
CATEGORY_KO = {"external_stop": "외부 종료", "oom": "메모리 부족(OOM)", "crash": "애플리케이션 오류",
               "config_error": "설정 오류", "dependency": "의존 서비스 장애", "resource": "자원 고갈", "unknown": "판단 불가"}


# ---------------------------------------------------------------- daily cap
def take_daily_quota():
    """오늘 호출 가능하면 카운트를 올리고 (True, 사용량) 을, 아니면 (False, 사용량) 을 돌려준다."""
    today = time.strftime("%Y-%m-%d")
    try:
        state = json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    if state.get("date") != today:
        state = {"date": today, "count": 0}
    if state["count"] >= MAX_PER_DAY:
        return False, state["count"]
    state["count"] += 1
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_FILE)
    return True, state["count"]


# ---------------------------------------------------------------- formatting
def describe_state(state):
    if not state:
        return "스냅샷 없음"
    if state.get("error"):
        return f"스냅샷 실패 ({state['error']})"
    return (f"status={state.get('status')}, exit_code={state.get('exit_code')}, oom_killed={state.get('oom_killed')}, "
            f"error={state.get('error_message') or '-'}, started_at={state.get('started_at')}, finished_at={state.get('finished_at')}")


def describe_heal(heal):
    if not heal:
        return "정보 없음"
    return f"{heal.get('result')} ({heal.get('detail', '')}, {heal.get('elapsed_s', '?')}초)"


def verify_evidence(evidence, sent_text):
    """모델이 인용한 줄이 실제로 보낸 로그에 있는지 대조한다 (환각 방지)."""
    return [{**e, "verified": bool(e.get("line", "").strip()) and e["line"].strip() in sent_text} for e in evidence]


def slack_header(job):
    heal = job.get("heal") or {}
    icon = "✅" if heal.get("result") == "restarted" else "❌"
    state = job.get("container_state") or {}
    exit_info = f"exit {state.get('exit_code')}, OOM {'예' if state.get('oom_killed') else '아니오'}" if "exit_code" in state else "상태 스냅샷 없음"
    return (f":mag: *[Sys-AIMS] AI 장애 분석 — `{job['container']}`* (event {job.get('event_id')})\n"
            f"복구: {icon} {heal.get('result', '?')} ({heal.get('elapsed_s', '?')}초)   종료: {exit_info}")


def slack_success(job, result, evidence, meta):
    usage = meta.get("usage", {})
    lines = [slack_header(job), "", f"*요약*: {result['summary']}"]
    if evidence:
        lines.append("*근거 로그*:")
        for e in evidence:
            mark = "" if e["verified"] else "  ⚠️ _원문에서 확인 안 됨_"
            lines.append(f"> `{e['line'].strip()[:300]}`{mark}\n>   ↳ {e['reason']}")
    else:
        lines.append("*근거 로그*: 없음 (로그에서 근거를 찾지 못함)")
    lines.append(f"_분류: {CATEGORY_KO.get(result['category'], result['category'])} · 신뢰도: {result['confidence']} · "
                 f"{meta.get('model')} · 토큰 입력 {usage.get('input_tokens', '?')}/출력 {usage.get('output_tokens', '?')} · "
                 f"{meta.get('latency_s')}초_")
    return "\n".join(lines)


def slack_fallback(job, reason, sent_text):
    tail = [l for l in sent_text.splitlines() if l.strip()][-FALLBACK_LINES:]
    body = "\n".join(f"> `{l[:300]}`" for l in tail) or "> (로그 없음)"
    return f"{slack_header(job)}\n\n:warning: *AI 분석 불가*: {reason}\n*장애 직전 로그 (최근 {len(tail)}줄)*:\n{body}"


# ---------------------------------------------------------------- worker
def process(job):
    ctx = {"event_id": job.get("event_id"), "container": job.get("container")}
    logs = job.get("logs") or []
    sent_text, stats = compress(logs, MAX_LOG_CHARS)
    EVENTS.write("rca.started", log_stats=stats, **ctx)

    allowed, used = take_daily_quota()
    if not allowed:
        notified = slack.post(SLACK_WEBHOOK_URL, slack_fallback(job, f"일일 한도({MAX_PER_DAY}회) 초과로 생략", sent_text))
        EVENTS.write("rca.skipped", reason="daily_cap", used_today=used, slack_notified=notified, **ctx)
        return

    note = (f"(원본 {stats['raw_lines']}줄 → 반복 패턴 {stats['pattern_dropped_lines']}줄 생략, "
            f"길이 한도로 오래된 {stats['truncated_lines']}줄 생략 → {stats['sent_lines']}줄 전달)")
    user_input = USER_TEMPLATE.safe_substitute(
        container=job.get("container"), trigger=job.get("trigger") or "-", event_id=job.get("event_id"),
        container_state=describe_state(job.get("container_state")), heal_result=describe_heal(job.get("heal")),
        compression_note=note, logs=sent_text or "(로그 없음)",
    )
    try:
        result, meta = llm.analyze(api_key=OPENAI_API_KEY, model=OPENAI_MODEL, instructions=SYSTEM_PROMPT,
                                   user_input=user_input, schema=SCHEMA, max_output_tokens=MAX_OUTPUT_TOKENS,
                                   timeout=OPENAI_TIMEOUT)
    except llm.LLMError as e:
        reason = f"OpenAI {e.kind} — {e}"
        notified = slack.post(SLACK_WEBHOOK_URL, slack_fallback(job, reason, sent_text))
        EVENTS.write("rca.failed", error_kind=e.kind, error=str(e), attempts=getattr(e, "attempts", None),
                     latency_s=getattr(e, "latency_s", None), usage=getattr(e, "usage", None),
                     model=OPENAI_MODEL, used_today=used, slack_notified=notified, **ctx)
        return

    evidence = verify_evidence(result.get("evidence", []), sent_text)
    message = redact(slack_success(job, result, evidence, meta))
    notified = slack.post(SLACK_WEBHOOK_URL, message)
    EVENTS.write("rca.completed", category=result["category"], confidence=result["confidence"], summary=result["summary"],
                 evidence=evidence, evidence_verified=sum(e["verified"] for e in evidence),
                 model=meta["model"], usage=meta["usage"], latency_s=meta["latency_s"], attempts=meta["attempts"],
                 used_today=used, slack_notified=notified, **ctx)


def worker():
    while True:
        job = JOBS.get()
        try:
            process(job)
        except Exception as e:  # 한 건의 실패가 작업자를 죽이면 안 된다
            EVENTS.write("rca.error", error=redact(repr(e))[:300], event_id=job.get("event_id"))
        finally:
            JOBS.task_done()


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = "sys-aims-rca"

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True, "queued": JOBS.qsize()})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/analyze":
            return self._send(404, {"error": "not found"})
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(supplied.encode(), TOKEN.encode()):
            EVENTS.write("auth.failed", path=self.path, client=self.client_address[0])
            return self._send(401, {"error": "unauthorized"})
        try:
            job = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "invalid json"})
        try:
            JOBS.put_nowait(job)
        except queue.Full:
            EVENTS.write("rca.dropped", reason="queue full", event_id=job.get("event_id"))
            return self._send(503, {"error": "queue full"})
        EVENTS.write("rca.queued", event_id=job.get("event_id"), container=job.get("container"), log_lines=len(job.get("logs") or []))
        self._send(202, {"queued": True})

    def log_message(self, *args):
        pass


def main():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    EVENTS.write("rca.service_started", model=OPENAI_MODEL, api_key_set=bool(OPENAI_API_KEY), max_per_day=MAX_PER_DAY,
                 max_log_chars=MAX_LOG_CHARS, max_output_tokens=MAX_OUTPUT_TOKENS, openai_timeout_s=OPENAI_TIMEOUT)
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8081), Handler).serve_forever()


if __name__ == "__main__":
    main()
