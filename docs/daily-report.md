# AI 일일 점검 보고서

매일 **08:00**(`REPORT_TIME`, Asia/Seoul)에 직전 24시간 지표를 모아 마크다운 보고서를 만듭니다.
- 수집원: Zabbix API, Sys-AIMS 이벤트 기록(`healer.jsonl`, `rca.jsonl`)
- 결과물: 마크다운 원본, HTML, AI 입력 JSON
- 외부 열람: Nginx `/reports/` (Basic Auth)

> **원칙: 숫자는 코드, 해석은 AI.** 표와 숫자, **종합 판정(정상/주의/위험)과 그 사유**는 전부 코드가 계산합니다. AI는 JSON 스키마로 해석 문장만 씁니다.
> AI 문장 속 숫자는 입력과 다시 대조합니다. 근거와 실제 사례는 [ai-trust.md](ai-trust.md)에 있습니다.

## 사용법

```bash
# 정기 실행: reporter 컨테이너가 기동되어 있으면 자동 (별도 cron 불필요)
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d reporter

# 즉시 생성 (개발·시연용). 정기 보고서를 덮어쓰지 않도록 adhoc-*.md 로 저장
docker exec reporter python -m daily_report.run                   # 직전 24시간
docker exec reporter python -m daily_report.run --hours 6         # 직전 6시간
docker exec reporter python -m daily_report.run --daily           # 오늘 정기 보고서 재생성 (전날 08:00 ~ 오늘 08:00)
docker exec reporter python -m daily_report.run --no-ai --no-slack   # AI·Slack 없이 표만 (비용 0)

# 로컬에서 보기 (./reports/ 는 gitignore)
sh scripts/fetch_reports.sh && open reports/index.html
```

## 구성

```
reporter (automation 이미지의 reporter 스테이지, 비root, read-only FS)
  daily_report.scheduler   매분 확인 → 오늘 정기 보고서가 없고 08:00이 지났으면 생성 (놓친 실행 따라잡기)
  daily_report.collect     Zabbix(읽기 전용 계정) + healer/rca 기록 → facts (숫자는 전부 여기서 계산)
  daily_report.report      facts → AI(해석) → 숫자 대조 → 마크다운 → 저장 → Slack
  daily_report.site        HTML 변환 · 목록(index.html) · 보관 기간 정리
볼륨
  automation_data → /data:ro   healer.jsonl / rca.jsonl (읽기 전용: 장애 기록 변조 방지)
  reports_data    → /reports   보고서. prod 에서는 nginx 가 읽기 전용으로 서빙
네트워크
  zbx_mon (zabbix-web API), egress (OpenAI, Slack)
```

### 스케줄링: 컨테이너 내부 Python 스케줄러
| | 호스트 crontab | **컨테이너 내부 스케줄러 (채택)** |
|---|---|---|
| EC2 재현 | 저장소 밖에서 별도 등록 필요 | `compose up`만 하면 됨 |
| 놓친 실행 | 08:00에 호스트가 꺼져 있거나 절전이면 누락 | **기동할 때 따라잡기** |
| 보안 | — | 비root, read-only 유지. busybox crond는 root가 필요하고, supercronic은 외부 바이너리를 받아야 함 |

- 정기 보고서의 기간은 따라잡기로 실행해도 항상 **"전날 08:00 ~ 오늘 08:00"**입니다.
- **검증**: reporter를 18:50에 처음 기동하자 `scheduler.run late_s=39012 catch_up=true`가 기록되고, 오늘치 `daily-2026-09-19`가 즉시 생성되었습니다.

## 수집 지표

| 영역 | 지표 | 집계 | 근거 |
|---|---|---|---|
| **서비스 가용성** | `http.status.code` (모든 HTTP 감시 호스트) | 가용률(200 비율), 비정상 응답 수, 최장 연속 실패 | "서비스가 살아 있었나"를 가장 직접적으로 보여줌 |
| 장애 | Zabbix 문제 이벤트 | 건수, 해소·미해소, 서비스별 누적 다운타임, MTTR, 최근 20건 목록(+AI 분류) | 장애 영향 규모 |
| **자동화** | `healer.jsonl`, `rca.jsonl` | 복구 요청·성공·실패·차단, 서킷, AI 분석 건수·분류·토큰·비용 | **Zabbix에 없는 Sys-AIMS 고유 데이터** |
| CPU | `system.cpu.util`, `system.cpu.load[all,avg5]` | 평균, **p95**, 최대 | 평균만으로는 순간 급증이 가려짐 |
| 메모리 | `vm.memory.size[available/total]` | 사용률 평균·최대, 최소 가용량 | OOM 선행 지표 |
| 네트워크 | `net.if.in/out["eth0"]` | 평균·p95·최대 | 나머지 11개 인터페이스는 가상 터널이라 제외 |
| 디스크 | — | **"미수집(사유)"으로 표시** | 컨테이너 agent 한계. EC2 이전 후 해결 예정 ([aws-migration.md](aws-migration.md)) |
| **데이터 품질** | 아이템별 점 수 | **수집률** = 점 수 ÷ (기간 ÷ 실제 수집 간격), 첫 수집·감시 시작 시각 | 보고서를 얼마나 믿을 수 있는지 |

**직전 기간 비교**
- 같은 길이의 직전 기간을 한 번 더 집계해 차이를 보여줍니다.
- 어느 한쪽 수집률이 50% 미만이면 "(수집률 낮음)"으로 표시합니다.
- 직전 기간에 존재하지 않던 서비스는 비교에서 제외합니다.

**수집률이 낮은 이유 (로컬)**: 맥 절전 구간은 수집되지 않습니다. 2026-09-19 기준 24시간 수집률은 7~14%였습니다. EC2에서는 이 제약이 없습니다.

## 보고서 구조 (`automation/prompts/report_template.md`)

| 섹션 | 작성 |
|---|---|
| 머리말: 기간·생성 시각·**수집률**·AI 모델/토큰 | 코드 |
| 1. 종합 판정 (🟢 정상 / 🟡 주의 / 🔴 위험) + **판정 사유** + 판정 규칙 | **코드** |
| 1. (이어서) 판정 사유 설명 2~3문장 | 🤖 AI |
| 2. 서비스 가용성 표 | 코드 |
| 3. 장애 및 자동 복구: 요약·표 / 패턴 설명 | 코드 / 🤖 AI |
| 4. 리소스 사용량 표 (디스크: 미수집 사유) | 코드 |
| 5. 주의할 점 ≤3 {근거 수치, 조치} + 직전 기간 대비 | 🤖 AI |
| 6. 자동화 운영 현황 | 코드 |
| 부록. 데이터 품질 + **AI 문장 숫자 대조 결과** | 코드 |

**프롬프트 파일** (코드와 분리)

| 파일 | 내용 |
|---|---|
| `report_system.md` | 역할 분담, 판정 규칙, 설계 의도(`healing=off`, 기간 이후 추가된 서비스) |
| `report_user.md` | 입력 틀 |
| `report_schema.json` | 응답 스키마 |
| `report_template.md` | 마크다운 틀 |

## 종합 판정 (코드 규칙)

| 판정 | 조건 (하나라도 해당하면) |
|---|---|
| 🔴 위험 | 미해소 장애 · 서킷 열림/차단 · 자동 복구 실패 |
| 🟡 주의 | 장애 발생(모두 해소) · 데이터 수집률 80% 미만 · 수집 실패 |
| 🟢 정상 | 그 외 |

- **수집 실패는 모니터링 문제**이므로 🟡로만 올립니다. 서비스 장애로 판정하지 않습니다.
- 보고서에는 해당 사유가 모두 수치와 함께 표시됩니다. 예시(`daily-2026-09-19`):
  ```
  ### 🟡 주의
  **판정 사유** (코드 규칙)
  - 🟡 장애 20건 발생 (모두 해소)
  - 🟡 데이터 수집률 7.4% (기준 80% 미만)
  ```
- AI 판정을 코드로 옮긴 이유(같은 데이터에 다른 판정, 모니터링 실패를 장애로 판정)는 [ai-trust.md 원칙 5](ai-trust.md)에 있습니다.
- Slack 알림에도 판정과 사유(최대 4개)를 포함합니다.

## 파일명 · 보관

| 종류 | 파일명 | 보관 |
|---|---|---|
| 정기 | `daily-YYYY-MM-DD.{md,html,json}` (기간이 끝나는 날짜) | **90일** (`REPORT_RETENTION_DAYS`) |
| 수동 실행 | `adhoc-YYYY-MM-DD-HHMM-<N>h.{md,html,json}` | **7일** |
| 목록 | `index.html` (매 실행마다 갱신) | — |
| 내부 상태 | `.state/quota.json`(일일 호출 수), `.events/report.jsonl`(생성 기록) | Nginx에서 403 |

- 보관 기간은 매 실행 때 **파일명의 날짜** 기준으로 정리합니다.
- **검증**: 기준일 09-19에서 정기 91일 경과분은 삭제, 90일 경과분은 유지. 수동 8일 경과분은 삭제, 6일 경과분은 유지. 관련 없는 파일은 그대로 두었습니다.

## 외부 열람: HTML 사전 렌더링 + Nginx `/reports/`

- 브라우저에서 `.md`는 표가 렌더링되지 않습니다. 그래서 생성 시점에 **HTML로 변환**해 둡니다(라이트/다크, 모바일 폭 대응).
- Nginx 설정: `nginx/snippets/reports.conf`. prod HTTPS 서버 블록에서 include합니다.

| 설정 | 내용 |
|---|---|
| **Basic Auth** | 보고서에 호스트명과 에러 로그가 들어가기 때문. `nginx/auth/reports.htpasswd`는 gitignore |
| 읽기 전용 | `reports_data`를 read-only로 마운트. `autoindex off` |
| 노출 차단 | `.state/`, `.events/` → 403 |
| 헤더 | `X-Robots-Tag: noindex`, `nosniff`, `charset=utf-8` |

**로컬 검증** (임시 nginx, 127.0.0.1:8082, 2026-09-19)

| 요청 | 결과 |
|---|---|
| 인증 없음 / 잘못된 비밀번호 | **401** |
| 인증 후 `index.html`, `daily-2026-09-19.html` | **200** (표 5개 렌더링) |
| 인증 후 `.md` | 200 `text/markdown; charset=utf-8` |
| 인증 후 `.json` | 200 `application/json; charset=utf-8` |
| `.state/quota.json`, `.events/report.jsonl`, `.state/` | **403** |

- Slack 알림에 링크를 넣으려면 `.env`에 `REPORT_BASE_URL=https://<도메인>/reports`를 설정합니다. 비어 있으면 파일명만 표시합니다.

## 읽기 전용 Zabbix 계정

- Super admin(`ZABBIX_API_*`)은 프로비저닝 스크립트만 씁니다. **reporter는 읽기 전용 계정만 가집니다.**
- `scripts/zabbix_config.py apply/import`가 생성합니다. 비밀번호는 `.env`의 `ZABBIX_REPORT_PASSWORD`에서 읽고, export 파일에는 남지 않습니다.

| 구성 | 설정 |
|---|---|
| 역할 `Sys-AIMS Report (read-only)` | UI 접근 없음. API는 **허용 목록만**: `event.get`, `history.get`, `host.get`, `item.get`, `problem.get`, `trend.get`, `user.logout` |
| 사용자 그룹 `Sys-AIMS Read-only` | GUI 비활성, `Sys-AIMS` 호스트 그룹 읽기만 |
| 사용자 `sys-aims-report` | 위 역할 + 그룹 |

**검증** (실제 호출, 2026-09-19)

| 호출 | 결과 |
|---|---|
| API 로그인 (GUI 비활성 그룹) | ✅ 성공 |
| 허용된 조회 6종 | ✅ 허용 |
| `host.update`, `item.create` (쓰기) | ✅ 거부: `No permissions to call` |
| `usermacro.get` (Secret 매크로), `user.get`, `mediatype.get`, `action.get`, `configuration.export` | ✅ 거부 |
| 그룹 밖 호스트(`Zabbix server`) 조회 | ✅ 0건 (보이지 않음) |
| 웹 UI 로그인 | ✅ "GUI access disabled" |
| `user.logout` | 처음에는 허용 목록에 없어 **세션이 누적되는 문제**가 있었음 → 추가 |

## 비용 상한 (RCA와 같은 정책)

| 항목 | 상한 | 근거 |
|---|---|---|
| 입력 | 집계 JSON **16,000자** | 원시 로그 없이 집계값만 보냄. 넘치면 장애 목록 → RCA 요약 순으로 오래된 것부터 줄임. 장애가 많은 날에도 입력이 튀지 않음 |
| 출력 | **2,000 토큰** (`REPORT_MAX_OUTPUT_TOKENS`, 추론 토큰 포함) | 실측 556~738 토큰 |
| 횟수 | **하루 5회** (`REPORT_MAX_PER_DAY`, 정기 + 수동 합산) | 초과하면 AI 없이 표만 생성 |

**실측** (2026-09-19, gpt-5.6-luna, 단가는 [rca.md](rca.md) 참고)

| 실행 | 입력 | 출력 (추론) | 소요 | 비용 |
|---|---|---|---|---|
| 정기 (따라잡기) | 3,433 | 578 (82) | 10.6초 | $0.00155 |
| 정기 재생성 ① | 3,832 | 597 (98) | 7.0초 | $0.00167 |
| 정기 재생성 ② | 3,543 | 556 (82) | 4.1초 | $0.00131 |

- 1회 약 **$0.0015**입니다. 매일 1회면 **약 $0.05/월**입니다.
- 가장 비싼 경우(입력 16,000자 ≈ 5천 토큰 + 출력 2,000)도 1회 약 $0.004이고, 일일 5회를 다 써도 하루 $0.02입니다.

## 실패 시 동작: 보고서는 항상 만든다

| 상황 | 결과 | 검증 (2026-09-19) |
|---|---|---|
| **Zabbix 다운** | 네트워크 오류는 2회 재시도(2초, 5초). 실패한 섹션은 "수집 실패(사유)". **자동화(healer/rca) 표는 그대로.** AI에는 어떤 데이터가 없는지 전달 | 존재하지 않는 주소: 3개 섹션 수집 실패, 자동화 표 유지, 20.9초 ✅ |
| **OpenAI 실패** | 코드가 만든 **표와 판정은 전부 유지**, AI 섹션만 "AI 분석 불가(사유)" | 모델 404: 3.0초, 표 5종 유지 ✅ |
| 일일 한도 초과 | AI 없이 표만 생성 | (코드 경로) |
| **데이터 부족** (새로 설치한 환경) | 수집률 0%로 표시 → 판정 🟡 주의 (수집률 80% 미만) | 새 Zabbix + import 직후: 수집률 0.0%, 보고서 정상 생성, 숫자 대조 3/3 ✅ |
| 스케줄러 예외 | 기록 후 5분 뒤 재시도. 컨테이너 `restart: unless-stopped` + 따라잡기 | (코드 경로) |

Slack 알림은 보고서 1건당 1회입니다(판정·최저 가용률·장애·자동 복구·요약·링크). 실패해도 보냅니다.

## 외부 의존성: `markdown` (reporter 전용)

| 항목 | 내용 |
|---|---|
| 위치 | `automation/requirements/reporter.txt` |
| 고정 방식 | `markdown==3.10.3` + 배포 파일 2종(wheel, sdist)의 **sha256 해시** |
| 설치 | Dockerfile `reporter` 스테이지에서 `pip install --require-hashes --no-deps` (해시가 다르면 설치 거부, 의존성 자동 설치 안 함. Markdown 3.10.3은 런타임 의존성 없음) |
| 격리 | healer·rca는 `base` 스테이지를 쓰므로 **설치되지 않음**. 검증: `sys-aims/automation:local` → not installed, `sys-aims/reporter:local` → 3.10.3 |

**갱신 절차**
1. `curl -s https://pypi.org/pypi/Markdown/json`에서 새 버전의 wheel·sdist sha256을 확인합니다.
2. `reporter.txt`의 버전과 해시 2개를 교체합니다.
3. `docker compose build reporter`를 실행합니다.
4. `python -m daily_report.run --no-ai --no-slack`로 HTML이 정상인지 확인합니다.
