# 장애 시나리오 (발표·검증용)

`docker stop` 장애는 로그가 SIGQUIT 종료뿐이라 AI가 분석할 내용이 빈약합니다.
그래서 **실제 운영에서 흔하고, 재기동으로 해결되지 않는** 장애 두 가지를 재현할 수 있게 만들었습니다.

```bash
python3 scripts/chaos.py dependency --watch                     # ① 의존 서비스 연결 실패
python3 scripts/chaos.py config --watch                         # ② 설정 파일 오류
python3 scripts/chaos.py dependency --watch --until-escalation  # 5분 뒤 에스컬레이션 Slack까지 관찰
python3 scripts/chaos.py restore                                # 원상 복구 (주입 파일 제거, pitwall_api 기동, 서킷 해제, pitwall_web 기동)
```

- `--watch`는 Zabbix 문제·Action 기록과 healer/rca 이벤트를 시간순으로 출력합니다.
- macOS에서는 스크립트가 스스로 `caffeinate -i`를 적용합니다.

## 구성: pitwall_web → pitwall_api 의존 관계

```
Zabbix HTTP 체크 ─▶ pitwall_web /healthz ─(proxy)─▶ pitwall_api /status.json
Docker healthcheck ─▶ pitwall_web /healthz
```

**딥 헬스체크**
- `/healthz`는 pitwall_api까지 응답해야 200을 반환합니다.
- Zabbix(`{$SERVICE.URL}`)와 Docker healthcheck가 모두 이 경로를 씁니다.

**의존 서비스 이름은 "요청 시점"에 조회**
- nginx 설정에서 `resolver 127.0.0.11` + 변수 `proxy_pass`를 씁니다.
- 기동 시점에 조회하면 pitwall_api가 없을 때 nginx 자체가 뜨지 않습니다. 그러면 의존 서비스 장애가 설정 오류처럼 보이게 됩니다.

**pitwall_api**
- 자동 복구 대상이 **아닙니다**(`healing=off`, socket-proxy 허용 목록 밖).
- Zabbix에는 별도 문제로 표시되어, 사람이 **근본 원인 서비스**를 바로 볼 수 있습니다.

## ① 의존 서비스 연결 실패 (주 시나리오)

- **주입**: `docker stop pitwall_api`
- **증상**: pitwall_web `/healthz` → **502** (`pitwall_api could not be resolved`)

**흐름** (실측, 2026-09-19, event 757)

| t | 일어난 일 |
|---|---|
| 0s | `docker stop pitwall_api` |
| +24.7s | Zabbix 문제: `pitwall_api: HTTP service is down` (event 756, healing=off → Action 없음) |
| +25.7s | Zabbix 문제: `pitwall_web: HTTP service is down` (event 757) → Action step 1 |
| +26.5s | healer: 재기동 **전** 로그 스냅샷 39줄 |
| +26.5s ~ +57.2s | healer: pitwall_web 재기동 → 30초 동안 healthy가 되지 않음 → **`502` 응답, Action Failed** |
| +57.9s | healer: `heal.failed` → Slack "자동 복구 실패" |
| +61.8s | rca: **분류 `dependency`, 신뢰도 high, 근거 3/3 원문 일치**, 입력/출력 토큰 2,102/446, 4.1초 → Slack |
| +5분 | Zabbix 에스컬레이션 step 2 → Slack "사람 개입 필요" (1차 실행 event 753에서 +324초에 확인) |

> 1차 실행(event 753)은 로그 창 버그(#6) 때문에 AI가 `external_stop`으로 **잘못 분류**했습니다. 위 결과는 수정 후 재실행입니다. 이 오답 사례 자체가 발표 소재입니다: [rca.md — 근거 대조를 통과한 그럴듯한 오답](rca.md)

**AI 분석 결과 (event 757)**
> pitwall_web은 의존 대상인 pitwall_api를 이름 해석하지 못해 `/healthz`가 502를 반환하면서 비정상 상태로 판단되었습니다. 컨테이너 자체는 running·exit code 0이었지만 자동 복구 후에도 30초 내 healthy가 되지 않아 복구에 실패했습니다.
>
> 1. `[error] 26#26: *18 pitwall_api could not be resolved (3: Host not found), … request: "GET /healthz HTTP/1.1"` ↳ 의존 대상 연결 실패
> 2. `"GET /healthz HTTP/1.1" 502 157 "-" "Wget"` ↳ healthz가 502 반환
> 3. `[error] 23#23: *4 pitwall_api could not be resolved …, client: 172.18.0.3` ↳ 다른 요청(Zabbix)에서도 반복 → 일시적 오류 아님

**발표 포인트**
- **재기동으로 해결되지 않는 장애**입니다. healer는 재기동은 했지만 healthy 확인에 실패해 **실패를 정직하게 보고**합니다(Action Failed, Slack).
- AI는 "pitwall_web 문제가 아니라 **의존 서비스 pitwall_api** 문제"라고 짚습니다. 사람은 Zabbix에서 pitwall_api 문제를 바로 확인할 수 있습니다.
- 자동 복구가 끝까지 실패하면 5분 뒤 사람을 부릅니다.

## ② 설정 파일 오류 (보조 시나리오)

- **주입**: `services/pitwall_web/conf.d/zz-chaos-broken.conf`에 오타(`proxy_pas`)가 있는 설정을 쓰고, pitwall_web을 재기동합니다(설정 배포 흉내). 이 파일은 gitignore 대상입니다.
- **증상**: nginx `[emerg] unknown directive "proxy_pas"` → 기동 직후 종료(exit 1). 재기동해도 계속 종료됩니다.

**흐름** (실측, 2026-09-19, event 760)

| t | 일어난 일 |
|---|---|
| 0s | 잘못된 설정 작성 + `docker restart pitwall_web` |
| +23.6s | Zabbix 문제 (event 760) → Action step 1 |
| +24.3s | healer: 스냅샷 10줄 → 재기동 → 30초 동안 healthy가 되지 않음(exited) → `502`, Action Failed |
| +55.4s | healer: `heal.failed` → Slack |
| +75.7s | rca: **분류 `config_error`, 신뢰도 high, 근거 2/2 원문 일치**, 입력/출력 토큰 1,241/233 |

**AI 분석 결과 (event 760)**
> Nginx가 잘못된 설정 지시어 `proxy_pas`를 파싱하지 못해 기동 직후 종료되었고, 그 결과 HTTP 서비스가 중단되었습니다. 컨테이너는 exit code 1로 종료되었으며 자동 복구도 30초 내 healthy 상태를 확인하지 못해 실패했습니다.

## 서킷 브레이커와의 관계

위 두 시나리오는 **장애가 계속되는 경우**입니다.
- Zabbix 문제가 해소되지 않고 열려 있으므로 Action은 **1회만** 실행됩니다.
- 그래서 무한 재기동이 애초에 일어나지 않고, 대신 **healthy 확인 실패 → Action Failed → 5분 뒤 에스컬레이션**으로 사람을 부릅니다.

서킷 브레이커가 동작하는 것은 **장애가 반복되는 경우**입니다.
- 예: 재기동하면 잠깐 살아났다가 다시 죽기를 반복하면, 매번 새 문제가 생깁니다.
- 이 경우는 [self-healing.md 검증 기록 A](self-healing.md)에서 확인했습니다: 4번째 장애에서 `429`, 서킷 열림, Slack 전송.

발표에서 두 장치를 함께 보여주려면 다음 순서를 권장합니다.
1. 시나리오 ①로 "재기동으로 안 되는 장애 → AI 분석 → 사람 호출"을 보여줍니다.
2. 검증 기록 A의 Slack/Action 기록으로 "반복 장애 → 서킷 차단"을 보여줍니다.
