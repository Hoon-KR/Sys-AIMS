"""로그 노이즈 압축 + 길이 상한 + 민감정보 마스킹.

근거 (docs/rca.md): pitwall_web 실측 시 로그의 약 96%가 헬스체크(Docker healthcheck,
Zabbix HTTP 체크) 200 응답 줄이었다. 원문 그대로 보내면 토큰 대부분이 노이즈에 쓰인다.

처리 순서
  1) 줄 길이 상한 (한 줄이 한도를 독점하지 않게)
  2) 패턴 압축: 타임스탬프/IP/PID/숫자를 정규화해 같은 패턴은 **최신 N건만** 남기고
     생략한 건수를 표시한다 (원인 분석엔 장애 직전 줄이 중요하므로 뒤에서부터 센다)
  3) 글자 수 상한: 최신 줄부터 채우고 넘치는 오래된 줄은 버린다
  4) 민감정보 마스킹 (OpenAI/Slack으로 나가기 전)
"""

import re
from collections import Counter

_NORMALIZERS = [
    (re.compile(r"\[\d\d/\w{3}/\d{4}:\d\d:\d\d:\d\d [+-]\d{4}\]"), "[ts]"),     # nginx access
    (re.compile(r"\d{4}[/-]\d\d[/-]\d\d[ T]\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:?\d\d)?"), "<ts>"),
    (re.compile(r"\b\d{1,3}(\.\d{1,3}){3}(:\d+)?\b"), "<ip>"),
    (re.compile(r"\b\d+#\d+:"), "<pid>:"),
    (re.compile(r"\b[0-9a-f]{12,64}\b"), "<hex>"),
    (re.compile(r"\b\d+\b"), "<n>"),
]

_SECRETS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "sk-[REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)\b(\s*[=:]\s*)\S+"), r"\1\2[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA[REDACTED]"),
    (re.compile(r"https://hooks\.slack\.com/services/\S+"), "https://hooks.slack.com/services/[REDACTED]"),
]


def normalize(line):
    for pattern, repl in _NORMALIZERS:
        line = pattern.sub(repl, line)
    return line.strip()


def redact(text):
    for pattern, repl in _SECRETS:
        text = pattern.sub(repl, text)
    return text


def compress(lines, max_chars, max_line_chars=500, keep_per_pattern=3):
    """lines(오래된 → 최신) 를 압축해 (본문 텍스트, 통계) 를 돌려준다."""
    lines = [l.rstrip() for l in lines if l.strip()]
    lines = [l if len(l) <= max_line_chars else l[:max_line_chars] + " …(잘림)" for l in lines]

    # 뒤(최신)에서부터 패턴별로 keep_per_pattern 건만 남긴다
    seen, kept, dropped = Counter(), [], Counter()
    for line in reversed(lines):
        key = normalize(line)
        seen[key] += 1
        if seen[key] <= keep_per_pattern:
            kept.append((key, line))
        else:
            dropped[key] += 1
    kept.reverse()

    # 생략 건수는 해당 패턴의 가장 오래된(=처음 나오는) 유지 줄에 붙인다
    annotated, marked = [], set()
    for key, line in kept:
        if dropped[key] and key not in marked:
            line = f"{line}  [같은 패턴 {dropped[key]}줄 생략]"
            marked.add(key)
        annotated.append(line)

    # 최신 줄부터 글자 수 상한까지 채운다
    body, used = [], 0
    for line in reversed(annotated):
        if used + len(line) + 1 > max_chars:
            break
        body.append(line)
        used += len(line) + 1
    body.reverse()
    truncated = len(annotated) - len(body)

    text = redact("\n".join(body))
    stats = {
        "raw_lines": len(lines),
        "raw_chars": sum(len(l) + 1 for l in lines),
        "pattern_dropped_lines": sum(dropped.values()),
        "truncated_lines": truncated,
        "sent_lines": len(body),
        "sent_chars": len(text),
    }
    return text, stats
