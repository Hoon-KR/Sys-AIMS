# Self-Healing

`pitwall_web`이 죽으면 **사람이 개입하지 않아도** Zabbix가 감지하고 healer가 재기동합니다.
반복 장애나 healer 장애처럼 자동 복구가 불가능한 상황에서는 **멈추고 사람을 부릅니다.**

## 흐름

```
pitwall_web 다운
  → Zabbix HTTP 체크 2회 연속 실패 → 문제 이벤트 (태그 healing=auto, container=pitwall_web)
  → Action "Sys-AIMS Self-Healing" (조건: 태그 healing=auto)
      1단계 (즉시)  : Webhook "Sys-AIMS Healer" → POST http://healer:8080/heal {container: {EVENT.TAGS.container}}
      2단계 (+5분)  : 문제가 아직 열려 있으면 Webhook "Sys-AIMS Slack" → 사람 호출
  → healer: 토큰 확인 → 허용 목록 확인 → 서킷 확인 → socket-proxy 경유 restart → healthy 확인(최대 30초)
  → Zabbix HTTP 체크 200 → 복구 이벤트 → 에스컬레이션 종료
```

| 구성 요소 | 위치 |
|---|---|
| healer 코드 | `automation/healing/healer.py` (Python 표준 라이브러리만 사용, 비root, read-only FS) |
| 이벤트 이력 | 볼륨 `automation_data`의 `/data/events/healer.jsonl` (아래 참고) |
| Webhook 스크립트 | `zabbix/mediatypes/healer.js`, `slack.js` |
| Zabbix 설정 | `scripts/zabbix_config.py` → export 결과 `zabbix/templates/mediatypes.yaml`, `automation.json` |
| socket-proxy 허용 범위 | [ADR-0001](adr/0001-docker-socket-access.md) |

**네트워크**

| 망 | 연결 | 특성 |
|---|---|---|
| `zbx_heal` | zabbix-server ↔ healer | internal |
| `docker_api` | healer ↔ socket-proxy | internal |
| `egress` | healer → Slack | 외부 통신 |

**비밀값**
- `HEALER_TOKEN`과 `SLACK_WEBHOOK_URL`은 `.env`에서 읽어 Zabbix의 **Secret 전역 매크로**(`{$HEALER.TOKEN}`, `{$SLACK.WEBHOOK}`)로 주입합니다.
- 전역 매크로는 export 대상이 아닙니다. 그래서 커밋되는 YAML에는 매크로 **이름만** 남습니다.
- API로 조회해도 값은 반환되지 않습니다.

## 설계 결정

### 대상 지정은 트리거 이름이 아니라 태그로
- Action 조건은 이벤트 태그 `healing=auto`입니다. 재기동할 대상은 이벤트 태그 `container`(호스트 태그에서 전파됨)에서 읽습니다.
- 감시 대상을 추가할 때는 호스트에 `container` 태그를 달기만 하면 됩니다.
- 트리거 태그 `healing`의 값은 매크로 `{$HEALING.MODE}`입니다. 기본값은 `auto`이고, healer 호스트에서는 `off`로 재정의합니다. **healer가 자기 자신을 고치려 하지 않게 하기 위해서입니다.**

### 무한 재기동 루프 방지

| 장치 | 동작 | 이유 |
|---|---|---|
| **서킷 브레이커** | 컨테이너별로 **600초 안에 3회**까지 재기동합니다. 4번째 요청부터 `429`로 거부하고 Slack을 보냅니다. | 계속 죽는 컨테이너를 무한히 되살리지 않습니다. |
| **수동 해제** | 서킷은 자동으로 닫히지 않습니다. 사람이 `POST /reset`으로 풀어야 합니다. | 시간이 지나 자동으로 풀리면 "3회 → 대기 → 3회"가 반복되는 느린 루프가 됩니다. |
| **상태 영속화** | 서킷 상태를 named volume(`automation_data`의 `/data/state.json`)에 저장합니다. | healer가 재시작되어도 서킷이 풀리지 않습니다. |
| **쿨다운** | 같은 컨테이너에 대해 60초 안에 들어온 재요청은 `409`로 무시합니다. | 중복 이벤트로 인한 연속 재기동을 막습니다. |
| **Action 1회 실행** | 에스컬레이션 1단계에서만 healer를 호출합니다. 반복하지 않습니다. | 같은 문제가 열려 있는 동안에는 다시 호출하지 않습니다. |

값은 `.env`의 `HEALER_MAX_RESTARTS`, `HEALER_WINDOW_SECONDS`, `HEALER_COOLDOWN_SECONDS`로 조정합니다.

### 실패 시 동작

| 상황 | healer 응답 | Zabbix Action | 사람에게 알리는 방법 |
|---|---|---|---|
| 재기동 후 30초 안에 healthy가 되지 않음 | `502` | **Failed** (오류 메시지 표시) | healer가 Slack 전송 + 5분 뒤 에스컬레이션 Slack |
| 서킷 열림 | `429` | **Failed** | 서킷이 열릴 때 healer가 Slack 전송 + 5분 뒤 에스컬레이션 Slack |
| **healer 다운** | (연결 불가) | **Failed** | **5분 뒤 에스컬레이션 Slack** (healer를 거치지 않는 유일한 경로) |
| 허용되지 않은 컨테이너 / 토큰 오류 | `403` / `401` | **Failed** | 5분 뒤 에스컬레이션 Slack |

- Webhook 미디어의 재시도는 1회입니다. 재시도와 차단 정책은 healer의 서킷 브레이커 한 곳에서만 관리합니다.
- 서킷이 열린 뒤 복구하는 절차: 원인을 확인하고 → `POST /reset`으로 서킷을 해제하고 → `docker start pitwall_web`을 실행합니다. 이미 열려 있는 문제에 대해 Action이 다시 실행되지는 않습니다.

```bash
# 서킷 상태 확인 / 수동 해제 (토큰은 컨테이너 환경변수에서 읽음)
docker exec healer python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/status').read().decode())"
docker exec healer python -c "import os,urllib.request;urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8080/reset',method='POST',headers={'Authorization':'Bearer '+os.environ['HEALER_TOKEN']}))"
```

---

## 이벤트 이력 (JSON Lines)

healer의 모든 요청, 결과, 서킷 변화는 볼륨 `automation_data`의 **`/data/events/healer.jsonl`**에 남습니다.
`docker logs`는 컨테이너를 재생성하면 사라지지만, 이 파일은 **이미지를 재빌드하거나 컨테이너를 재생성해도 유지**됩니다.

| 항목 | 내용 |
|---|---|
| 형식 | 한 줄 = 한 이벤트 (JSON). 공통 필드는 `v`(스키마 버전), `ts`, `epoch`, `source`, `event` |
| 파일 | 쓰는 주체별로 나눕니다: `healer.jsonl`, (예정) `rca.jsonl`. 여러 프로세스가 한 파일을 로테이트하면 경합이 생기기 때문입니다. |
| 연결 키 | `event_id`(Zabbix 이벤트 ID). 같은 장애에 대한 healer 기록과 RCA 기록을 이 키로 묶습니다. |
| 크기 제한 | 크기 기반 로테이션 5MB × 5개(`EVENT_LOG_MAX_BYTES`, `EVENT_LOG_BACKUPS`). 쓰는 주체당 최대 약 30MB입니다. |
| 실패 시 | 파일 기록이 실패해도 복구 작업은 계속됩니다. stdout(`docker logs`)에는 항상 같은 줄이 남습니다. |
| 구현 | `automation/common/eventlog.py`. RCA도 같은 모듈을 사용합니다. |

**healer 이벤트 종류**

| event | 의미 |
|---|---|
| `heal.requested` | 재기동 요청을 받음 |
| `heal.succeeded` | 재기동하고 healthy 확인까지 완료 |
| `heal.failed` | 재기동했지만 healthy 확인에 실패 |
| `heal.skipped` | 쿨다운 중이라 무시 |
| `heal.blocked` | 서킷이 열려 있어 거부 |
| `heal.rejected` | 허용되지 않은 컨테이너 |
| `auth.failed` | 토큰 인증 실패 |
| `circuit.opened` / `circuit.reset` | 서킷 열림 / 수동 해제 |
| `healer.started` | healer 기동 |

```bash
# 원본 보기
docker exec healer cat /data/events/healer.jsonl

# 집계 예: 이벤트 종류별 건수 (로테이트된 파일 포함)
docker exec healer sh -c 'cat /data/events/healer.jsonl*' | python3 -c "
import sys, json, collections
print(collections.Counter(json.loads(l)['event'] for l in sys.stdin))"

# 로컬로 복사 (logs/ 는 gitignore 대상)
docker cp healer:/data/events ./logs/events
```

**검증 (2026-09-18)**
- **로테이션**: 한도를 400B × 2개로 낮춘 임시 컨테이너에서 30건을 기록했습니다. 파일은 3개(각 368B)만 남았고, 최신 기록(seq 29)은 현재 파일에 있으며, 모든 줄이 유효한 JSON이었습니다.
- **실제 장애 기록**: `pitwall_web`을 중지하자 30초 만에 자동 복구되었고, `heal.requested`와 `heal.succeeded`(event_id 184, 5.2초)가 기록되었습니다.
- **재생성 후 보존**: `--build --force-recreate`로 컨테이너가 새로 만들어졌습니다. 새 컨테이너의 `docker logs`에는 복구 이벤트가 0건이지만, 파일에는 event 184 기록이 그대로 남아 있었습니다.

---

## 측정 결과: 자동 복구 다운타임

- **일시**: 2026-09-18
- **환경**: 로컬, MacBook M1, Docker Desktop, Zabbix 7.0.30
- **방법**: `python3 scripts/measure_detection.py --mode heal --trials 5`
  - macOS에서는 스크립트가 스스로 `caffeinate -i`를 적용합니다.
  - 스크립트는 `docker stop`만 실행합니다. **재기동은 전적으로 Zabbix Action과 healer가 수행**합니다.
  - 매 회차 서킷을 초기화합니다(5회 측정은 서킷 한도 3회를 넘기 때문).

| 회차 | 감지 (s) | 재기동 완료 (s) | Zabbix 복구 확인 (s) | Event ID | Action |
|---|---|---|---|---|---|
| 1 | 32.9 | 32.9 | 42.9 | 157 | Sys-AIMS Healer: sent |
| 2 | 28.2 | 28.2 | 38.2 | 162 | Sys-AIMS Healer: sent |
| 3 | 15.8 | 15.8 | 45.8 | 164 | Sys-AIMS Healer: sent |
| 4 | 28.9 | 29.0 | 38.9 | 166 | Sys-AIMS Healer: sent |
| 5 | 18.0 | 18.1 | 63.0 | 168 | Sys-AIMS Healer: sent |

모든 시간은 `docker stop` 시점(t=0)부터 잽니다.

| 지표 | 평균 | 중앙값 | 최소 | 최대 |
|---|---|---|---|---|
| 감지 (문제 이벤트 발생) | 24.8s | 28.2s | 15.8s | 32.9s |
| **서비스 다운타임** (컨테이너 재기동 완료, Docker `StartedAt`) | **24.8s** | 28.2s | 15.8s | 32.9s |
| Zabbix 기준 다운타임 (복구 이벤트) | 45.8s | 42.9s | 38.2s | 63.0s |

- **5회 모두 사람 개입 없이 자동 복구**되었고, Action 상태는 5회 모두 `sent`였습니다.
- **감지 → 재기동은 약 0.05초**입니다. 서비스 다운타임은 사실상 감지 시간과 같습니다.
  - 다운타임을 줄이려면 healer가 아니라 **감지 주기와 연속 실패 횟수**를 조정해야 합니다(오탐과의 트레이드오프, [zabbix-monitoring.md](zabbix-monitoring.md)).
- healer 로그상 재기동에서 healthy 확인까지는 약 5초입니다. 이 5초는 pitwall_web의 Docker healthcheck 주기 때문이고, 서비스는 재기동 직후부터 응답합니다.
- **Zabbix 기준 다운타임이 평균 21초 더 긴 것**은 [troubleshooting #4 (미해결)](troubleshooting.md)의 복구 확인 지연 때문입니다. 컨테이너는 이미 살아 있는데 Zabbix HTTP 체크가 1~3회 더 실패합니다.
- 발표 수치(로컬, n=5)
  - **"장애 발생부터 자동 복구까지 평균 24.8초, 사람 개입 0회"**
  - Zabbix 화면 기준으로 말할 경우: 평균 45.8초

---

## 검증 기록: 실패 시나리오

모두 2026-09-18에 로컬에서 실제로 실행했습니다. 실제 Slack 채널로 메시지가 전송되었습니다.

### A. 서킷 브레이커 (반복 장애)
`pitwall_web`을 65초 간격으로 4번 중지했습니다(쿨다운 60초를 피하기 위해).

| # | Event ID | healer Action | 결과 |
|---|---|---|---|
| 1 | 170 | sent | 자동 재기동 |
| 2 | 172 | sent | 자동 재기동 |
| 3 | 174 | sent | 자동 재기동 |
| 4 | 176 | **failed** — `healer HTTP 429: 3 restarts within 600s — circuit opened` | **재기동 안 함, 컨테이너 다운 유지** ✅ |
| | 176 | 5분 뒤 **step 2: Sys-AIMS Slack — sent** | 사람 호출 ✅ |

이어서 수동 해제(`/reset` → `was_open: true`)와 `docker start`로 복구했습니다.

### B. healer 다운
healer를 중지한 뒤 `pitwall_web`을 중지했습니다.

| Event ID | 결과 |
|---|---|
| 181 (pitwall_web) | healer Action **failed** — `cannot get URL: Could not resolve host: healer` |
| 181 | 5분 뒤 **step 2: Sys-AIMS Slack — sent** ✅ |
| healer 자신의 문제 이벤트 | 태그 `healing=off` → **Action 0건** ✅ (healer가 자기 자신을 고치려 하지 않음) |

### C. healer 단독 검증
| 확인 항목 | 결과 |
|---|---|
| 서킷이 열린 상태로 healer 재시작 | 재시작 후에도 `circuit_open=true` 유지 ✅ |
| 서킷이 열린 상태에서 `/heal` | `429` ✅ |
| `/heal` 대상이 `postgres` | `403 container 'postgres' is not allowed` ✅ |
| 잘못된 토큰 | `401` ✅ |
| 컨테이너 파일시스템 쓰기 | `/app` 은 `Read-only file system`, `/data` 만 쓰기 가능 ✅ |
| healer → Slack 외부 통신 | `slack.post → True` ✅ |

---

## 발표 스크린샷 가이드 (Zabbix 웹)

- **이벤트별 Action 기록**: *Monitoring → Problems* → 필터에서 *Show: History* → `pitwall_web` 이벤트의 시각 클릭 → 이벤트 상세의 **Actions** 목록
  - 성공 예: Event **157~168** → `Sys-AIMS Healer` / **Sent**
  - 서킷 차단 예: Event **176** → `Sys-AIMS Healer` / **Failed** (429 메시지) + `Sys-AIMS Slack` / **Sent** (step 2)
  - healer 다운 예: Event **181** → `Failed` (Could not resolve host) + Slack **Sent**
- **전체 Action 로그**: *Reports → Action log*
- **healer 쪽 로그**: `docker logs healer` 의 JSON 로그 (`heal.requested` → `heal.succeeded`, `circuit.opened`)
