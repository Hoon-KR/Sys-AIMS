# Zabbix 모니터링 구성

모든 설정은 **Zabbix API로 적용**하고 **YAML로 export해 커밋**합니다. 웹 UI에서 수동으로 설정하지 않습니다.

```bash
python3 scripts/zabbix_config.py apply    # 현재 Zabbix에 적용 (반복 실행해도 결과 동일)
python3 scripts/zabbix_config.py export   # zabbix/templates/*.yaml 갱신
python3 scripts/zabbix_config.py import   # 새 환경(EC2)에 재현
```

인증 정보는 `.env`의 `ZABBIX_API_URL`, `ZABBIX_API_USER`, `ZABBIX_API_PASSWORD`에서 읽습니다. 같은 이름의 환경변수가 있으면 그 값이 우선합니다.

---

## 구성 요소

| 대상 | 내용 |
|---|---|
| 템플릿 `Sys-AIMS HTTP Service` | HTTP 상태 코드 아이템 1개 + 트리거 1개 + 매크로 |
| 호스트 `zabbix-agent` | `Linux by Zabbix agent` 템플릿, 인터페이스는 DNS 이름 `zabbix-agent:10050` |
| 호스트 `pitwall_web` | `Sys-AIMS HTTP Service` 템플릿, 인터페이스 없음, 태그 `container=pitwall_web` |
| 호스트 `pitwall_api` | pitwall_web의 의존 서비스. `/status.json` 감시, `{$HEALING.MODE}=off` |
| 호스트 `healer` | `Sys-AIMS HTTP Service` 템플릿 (`/health` 감시), `{$HEALING.MODE}=off` |
| 호스트 `rca` | `/health` 감시, `{$HEALING.MODE}=off` |
| Self-Healing Action, 미디어 타입, 전용 사용자 | [self-healing.md](self-healing.md) |
| 보고서용 읽기 전용 계정 (역할·그룹·사용자) | [daily-report.md](daily-report.md) |
| 기본 호스트 `Zabbix server` | Linux 템플릿 unlink+clear, agent 인터페이스 제거 ([troubleshooting #1](troubleshooting.md)) |

### HTTP 체크 아이템 `http.status.code`
- **방식**: HTTP agent 아이템. 헤더만 받아 상태 줄에서 코드를 추출합니다. Docker 소켓은 필요 없습니다([ADR-0001](adr/0001-docker-socket-access.md)).
- **URL**: `{$SERVICE.URL}`. 호스트에서 재정의하며, 컨테이너 DNS 이름을 사용합니다. pitwall_web은 **딥 헬스체크** `http://pitwall_web/healthz`(의존 서비스 pitwall_api까지 확인)입니다([chaos-scenarios.md](chaos-scenarios.md)).
- **값**: HTTP 상태 코드. 연결 실패나 타임아웃이면 `0`입니다.
  - 전처리 1단계 `Check for not supported value`: 오류를 `0`으로 바꿉니다.
  - 전처리 2단계 `Regex`: 상태 코드를 추출하고, 실패하면 `0`을 유지합니다.
- **주기 15초, 타임아웃 5초** (매크로 `{$HTTP.CHECK.INTERVAL}`, `{$HTTP.CHECK.TIMEOUT}`)

### 트리거 `{HOST.NAME}: HTTP service is down`
```
장애: count(/Sys-AIMS HTTP Service/http.status.code,#2,"ne","200")=2
복구: last(/Sys-AIMS HTTP Service/http.status.code)=200
```
- **심각도**: High
- **수동 닫기**: 허용
- **태그**: `scope=availability`, `component=http`, `healing={$HEALING.MODE}`
  - 매크로 기본값은 `auto`입니다. 자동 복구에서 제외할 호스트(예: healer)는 `off`로 재정의합니다.
- 이벤트에는 호스트 태그 `container=<이름>`도 함께 전파됩니다.

---

## 값 선정 근거

**부하와 감지 지연의 균형: 15초 × 연속 2회**

- **부하**: HTTP GET 한 번의 비용은 사실상 0입니다. 15초 주기면 하루 5,760개 값이 쌓이는데, DB에도 부담이 되지 않습니다. 부하만 보면 더 짧게 해도 됩니다.
- **실제 제약은 오탐(false positive)입니다.** 이 트리거는 **컨테이너 자동 재기동**으로 이어집니다. 1회 실패로 판정하면 순간적인 지연에도 멀쩡한 서비스를 재시작하게 됩니다. 그래서 주기는 짧게 두고, 연속 실패 횟수로 오탐을 거릅니다.
- **이론상 감지 시간은 15~35초**입니다. 장애 직후 첫 체크까지 0~15초, 두 번째 체크까지 15초, 타임아웃 최대 5초가 걸리기 때문입니다.
- **타임아웃 5초**: 정적 nginx는 밀리초 단위로 응답합니다. 5초 안에 응답이 없으면 느린 게 아니라 멈춘 것으로 봅니다. 또 주기(15초)보다 충분히 짧아 수집이 겹치지 않습니다.
- **복구는 1회 성공으로 판정합니다.** 재기동 후 복구를 빨리 확인하기 위해서입니다. 장애와 복구 조건을 따로 두어, 경계값에서 문제가 켜졌다 꺼졌다 반복하는 것(flapping)도 막습니다.

**Action 조건은 이름 대신 태그 `healing=auto`로 잡습니다.**
트리거 이름은 바뀔 수 있지만 태그는 의도를 드러내는 계약처럼 유지됩니다. 새 감시 대상을 추가할 때 태그만 달면 자동 복구 대상이 됩니다.

---

## 측정 결과: 장애 감지 및 복구 확인 시간

- **일시**: 2026-09-18
- **환경**: 로컬, MacBook M1, Docker Desktop, Zabbix 7.0.30
- **방법**: `caffeinate -i python3 scripts/measure_detection.py --trials 5`
  1. 정상 상태를 확인합니다.
  2. 0~15초 무작위로 기다립니다. 장애 시점이 수집 주기와 겹치는 위치를 분산시켜, 평균이 한쪽으로 치우치지 않게 하기 위해서입니다.
  3. `docker stop`을 실행합니다.
  4. Zabbix 문제 이벤트의 발생 시각(서버 시각)을 기록합니다.
  5. `docker start`를 실행합니다.
  6. 복구 이벤트의 발생 시각을 기록합니다.

| 회차 | 감지 (s) | 복구 확인 (s) |
|---|---|---|
| 1 | 24.2 | 29.8 |
| 2 | 21.7 | 9.5 |
| 3 | 22.0 | 44.9 |
| 4 | 25.7 | 14.2 |
| 5 | 29.7 | 29.8 |

| | 평균 | 중앙값 | 최소 | 최대 |
|---|---|---|---|---|
| **감지** | **24.7s** | 24.2s | 21.7s | 29.7s |
| 복구 확인 | 25.6s | 29.8s | 9.5s | 44.9s |

- **감지 시간**은 5회 모두 이론 범위(15~35초) 안에 있습니다. → 발표 수치: **"평균 감지 시간 24.7초 (n=5, 로컬)"**
- **복구 확인 시간**은 설계 기대치(다음 체크 1회, 15초 이내)보다 길고 편차가 큽니다.
  - `docker start` 직후 서버 컨테이너에서 직접 DNS 조회와 HTTP 요청을 해 보면 **즉시 성공**합니다.
  - 그런데 Zabbix HTTP agent는 1~2회 더 `0`을 기록했습니다.
  - Zabbix 쪽 DNS 캐시(libcurl 8.21.0)를 의심하고 있지만, **원인은 아직 검증하지 않았습니다.**
  - 이 문제는 장애 감지와 Self-Healing 동작에는 영향이 없습니다. 복구 이벤트가 늦게 닫힐 뿐입니다.
  - Self-Healing 단계에서 재기동 후 복구 확인 시간을 다시 측정할 때 원인을 확인합니다.
- n=5인 로컬 측정값입니다. EC2로 옮긴 뒤 같은 스크립트로 다시 측정해 비교합니다.

---

## 재현성 검증: export → 새 Zabbix에 import

AWS 이전 계획의 전제 조건입니다. 2026-09-18에 검증했습니다.

1. 기존 스택과 분리된 **새로 설치한 Zabbix 7.0.30**(임시 컨테이너)을 띄웁니다. 기본 호스트 `Zabbix server`가 `127.0.0.1:10050`을 바라보는 초기 상태입니다.
2. `scripts/zabbix_config.py import`를 실행합니다. → 템플릿, 호스트, 그룹이 생성되고 기본 호스트가 정리됩니다.
3. `import`를 한 번 더 실행합니다. → 오류 없이 결과가 같습니다(idempotent).
4. 새 인스턴스에서 다시 export해 커밋본과 `diff`합니다. → **두 파일 모두 완전히 동일합니다.**
5. **기능 확인**: 새 인스턴스에는 `pitwall_web`이 없습니다. 그래서 아이템 값이 `0`, `0`, `0`으로 기록되고, 트리거가 **High 문제를 발생**시킵니다. 이벤트 태그 `healing=auto`, `container=pitwall_web`도 정상적으로 붙습니다.
6. 임시 인스턴스를 삭제합니다.

→ EC2에서는 스택을 기동한 뒤 `import` 한 번이면 동일한 구성이 재현됩니다.

### 재검증 (Self-Healing 추가 후, 2026-09-18)
- 대상: export 파일 4개(`sys-aims-http-service.yaml`, `hosts.yaml`, `mediatypes.yaml`, `automation.json`)
- 방법: 새로 설치한 Zabbix에 import → 다시 export → 비교
  - 비밀값은 더미로 대체해 실제 값이 임시 인스턴스에 들어가지 않게 했습니다.
- 결과: **4개 파일 모두 완전히 동일**합니다.
  - 처음에는 `mediatypes.yaml`에서 스크립트 끝 줄바꿈 차이가 있었습니다([troubleshooting #5](troubleshooting.md)).
  - 이를 수정한 뒤 **완전히 새 인스턴스에서 처음부터** 다시 확인했습니다.
- Action, 사용자, 사용자 그룹은 Zabbix `configuration.export`가 지원하지 않습니다. 그래서 이름 기반 JSON(`automation.json`)으로 직접 export/import합니다.
