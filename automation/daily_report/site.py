"""보고서 HTML 변환 · 목록 페이지 · 보관 기간 정리.

Nginx 가 /reports/ 를 정적으로 서빙한다 (Basic Auth). 브라우저에서 .md 는 표가 렌더링되지 않으므로
생성 시점에 HTML 로 변환해 둔다. 원본 .md 와 AI 입력 .json 도 함께 둔다.
"""

import html
import json
import os
import pathlib
import re
import time

import markdown  # reporter 이미지에만 설치 (automation/requirements/reporter.txt, 해시 고정)

RETENTION_DAYS = int(os.environ.get("REPORT_RETENTION_DAYS", "90"))
ADHOC_RETENTION_DAYS = 7
_NAME = re.compile(r"^(daily-(\d{4}-\d{2}-\d{2})|adhoc-(\d{4}-\d{2}-\d{2})-\d{4}-[\d.]+h)$")

CSS = """
:root{--bg:#fff;--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--head:#f6f8fa;--accent:#0969da}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#9198a1;--line:#3d444d;--head:#151b23;--accent:#4493f8}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,"Apple SD Gothic Neo","Noto Sans KR","Malgun Gothic",sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:1.6em;border-bottom:1px solid var(--line);padding-bottom:.3em}
h2{font-size:1.25em;margin-top:2em;border-bottom:1px solid var(--line);padding-bottom:.2em}
table{border-collapse:collapse;display:block;overflow-x:auto;max-width:100%;margin:.5em 0}
th,td{border:1px solid var(--line);padding:6px 10px;white-space:nowrap}
th{background:var(--head)} blockquote{margin:0;padding:.2em 1em;color:var(--muted);border-left:4px solid var(--line)}
sub{color:var(--muted)} a{color:var(--accent)} code{font-size:.9em}
nav{margin-bottom:16px;font-size:.9em}
"""


def page(title, body, back=True):
    nav = '<nav><a href="./">← 보고서 목록</a></nav>' if back else ""
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">'
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>{nav}{body}</main></body></html>")


def render(directory, name, md_text):
    body = markdown.markdown(md_text, extensions=["tables", "sane_lists"], output_format="html")
    title = md_text.splitlines()[0].lstrip("# ").strip()
    (pathlib.Path(directory) / f"{name}.html").write_text(page(title, body))


def _date_of(name):
    m = _NAME.match(name)
    return time.mktime(time.strptime(m.group(2) or m.group(3), "%Y-%m-%d")) if m else None


def apply_retention(directory, now=None):
    """정기 보고서 RETENTION_DAYS 일, 수동 실행 보고서 7일. 파일명 날짜 기준으로 삭제한다."""
    now, deleted = now or time.time(), []
    for md in pathlib.Path(directory).glob("*.md"):
        name, stamp = md.stem, _date_of(md.stem)
        if stamp is None:
            continue
        keep = RETENTION_DAYS if name.startswith("daily-") else ADHOC_RETENTION_DAYS
        if now - stamp > keep * 86400:
            for ext in (".md", ".html", ".json"):
                (md.parent / f"{name}{ext}").unlink(missing_ok=True)
            deleted.append(name)
    return deleted


def render_index(directory):
    directory = pathlib.Path(directory)
    rows = []
    for md in sorted(directory.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        name = md.stem
        try:
            meta = json.loads((directory / f"{name}.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            meta = {}
        status = meta.get("overall_status") or "-"
        icon = {"정상": "🟢", "주의": "🟡", "위험": "🔴"}.get(status, "⚪")
        kind = "정기" if name.startswith("daily-") else "수동"
        rows.append(f"<tr><td><a href=\"{name}.html\">{html.escape(name)}</a></td><td>{kind}</td>"
                    f"<td>{icon} {html.escape(status)}</td><td>{html.escape(meta.get('period', ''))}</td>"
                    f"<td>{meta.get('coverage_pct', '-')}%</td>"
                    f"<td><a href=\"{name}.md\">md</a> · <a href=\"{name}.json\">json</a></td></tr>")
    body = ("<h1>Sys-AIMS 일일 점검 보고서</h1>"
            f"<p>정기 보고서 {RETENTION_DAYS}일 · 수동 실행 보고서 {ADHOC_RETENTION_DAYS}일 보관. "
            "json은 AI가 본 입력 데이터입니다.</p>"
            "<table><tr><th>보고서</th><th>종류</th><th>판정</th><th>기간</th><th>수집률</th><th>원본</th></tr>"
            + ("".join(rows) or "<tr><td colspan=6>아직 보고서가 없습니다.</td></tr>") + "</table>")
    (directory / "index.html").write_text(page("Sys-AIMS 일일 점검 보고서", body, back=False))
