"""외부(OpenAI / Slack)로 나가는 본문의 민감정보 마스킹.

불변식: **경계를 넘는 모든 본문은 redact() 를 통과한다.**
  - rca:      compress() 가 전송 직전에 호출 (근거 대조도 마스킹된 본문 기준)
  - reporter: build_ai_input() 결과와 Slack 본문에 호출

설계 원칙 — 과잉 마스킹은 분석을 망가뜨린다
  가린다: 식별 가능한 값 (IP, 내부 호스트명, 이메일/전화/주민번호/카드번호, AWS 식별자)
  남긴다: 상태 코드, 포트, 바이트 수, 버전, **컨테이너·서비스명**
          (pitwall_web 같은 자체 서비스명은 공개 저장소에 이미 있고,
           가리면 AI가 "무엇이 죽었는지" 말할 수 없게 된다)

IP와 호스트명은 삭제가 아니라 **한 호출 안에서 일관된 가명**(<ip-1>, <host-1>)으로 바꾼다.
"같은 클라이언트가 반복 요청" 같은 추론은 살리고 값만 제거하기 위함이다.

적용 순서가 중요하다. EC2 내부 호스트명(ip-172-31-45-141.*.compute.internal)은
이름 안에 사설 IP를 담고 있어 IP 규칙보다 **먼저** 처리해야 한다.
"""

import re

# ---------------------------------------------------------------- 비밀값 (값 자체를 버린다)
_SECRETS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "sk-[REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)\b(\s*[=:]\s*)\S+"), r"\1\2[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA[REDACTED]"),
    (re.compile(r"https://hooks\.slack\.com/services/\S+"), "https://hooks.slack.com/services/[REDACTED]"),
    # AWS 식별자: 계정 구조가 드러난다
    (re.compile(r"\bi-[0-9a-f]{8,17}\b"), "i-[REDACTED]"),
    # ARN 안의 12자리 계정 ID까지 함께 사라진다. 맨숫자 12자리를 계정 ID로 추측하는
    # 규칙은 두지 않는다 — 로그의 바이트 수·epoch 밀리초를 오탐할 위험이 더 크다.
    (re.compile(r"\barn:aws:[a-z0-9-]+:[a-z0-9-]*:\d{12}:\S+"), "arn:aws:[REDACTED]"),
]

# ---------------------------------------------------------------- 개인정보
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
# 주민등록번호: 생년월일 6자리 + 성별 자리(1~4) + 6자리
_RRN = re.compile(r"\b\d{6}-[1-4]\d{6}\b")
# 휴대전화 (+82 / 01X). 유선번호는 오탐이 많아 제외한다 (docs/ai-governance.md)
_PHONE = re.compile(r"(?<![\d\-])(?:\+?82[ \-]?)?0?1[016789][ \-]?\d{3,4}[ \-]?\d{4}(?![\d\-])")
# 카드번호 후보: 13~19자리(구분자 허용). Luhn 을 통과한 것만 가린다
_CARD_CANDIDATE = re.compile(r"(?<![\d\-])(?:\d[ \-]?){12,18}\d(?![\d\-])")

# ---------------------------------------------------------------- 네트워크 식별자
# EC2 내부 호스트명 (이름에 사설 IP가 들어 있다) — IP 규칙보다 먼저
_EC2_HOST = re.compile(r"\bip-\d{1,3}-\d{1,3}-\d{1,3}-\d{1,3}(?:\.[a-z0-9.\-]+)?\b")
_IPV4 = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")


def _luhn_ok(digits):
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        n = int(ch)
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _card_sub(match):
    digits = re.sub(r"[ \-]", "", match.group(0))
    if 13 <= len(digits) <= 19 and _luhn_ok(digits):
        return "[REDACTED-CARD]"
    return match.group(0)      # 바이트 수·타임스탬프 등은 건드리지 않는다


def _is_keepable_ip(value):
    """loopback 과 0.0.0.0 은 남긴다 (식별 정보가 아니고 진단에 쓰인다)."""
    if value == "0.0.0.0":
        return True
    if value.startswith("127."):
        return True            # 127.0.0.1, Docker 내장 DNS 127.0.0.11
    return False


def _valid_ip(value):
    parts = value.split(".")
    return len(parts) == 4 and all(p.isdigit() and int(p) <= 255 for p in parts)


class _Pseudonyms:
    """같은 값 → 같은 가명 (한 번의 redact 호출 안에서만 유효)."""

    def __init__(self, prefix):
        self.prefix, self.seen = prefix, {}

    def of(self, value):
        if value not in self.seen:
            self.seen[value] = f"<{self.prefix}-{len(self.seen) + 1}>"
        return self.seen[value]


def redact(text):
    if not text:
        return text

    for pattern, repl in _SECRETS:
        text = pattern.sub(repl, text)

    text = _EMAIL.sub("[REDACTED-EMAIL]", text)
    text = _RRN.sub("[REDACTED-RRN]", text)
    text = _CARD_CANDIDATE.sub(_card_sub, text)
    text = _PHONE.sub("[REDACTED-PHONE]", text)

    hosts = _Pseudonyms("host")
    text = _EC2_HOST.sub(lambda m: hosts.of(m.group(0)), text)

    ips = _Pseudonyms("ip")

    def _ip_sub(match):
        value = match.group(0)
        if not _valid_ip(value) or _is_keepable_ip(value):
            return value
        return ips.of(value)

    return _IPV4.sub(_ip_sub, text)
