# 트러블슈팅 기록

증상 → 원인 → 해결 순으로 기록합니다. EC2 이전 시에도 똑같이 겪을 수 있는 내용을 우선합니다.

---

## 1. "Linux: Zabbix agent is not available" 알람이 계속 뜬다

- **발생**: 2026-09-18, 로컬 스택 최초 기동 직후. 새로 설치하면 **EC2에서도 똑같이 발생합니다.**
- **처음 생각한 원인**: `zabbix-agent` 컨테이너를 호스트로 등록하지 않아서
- **실제 원인**: Zabbix를 설치하면 자동으로 생기는 기본 호스트 **`Zabbix server`** 때문

```
Zabbix server | agent 인터페이스 127.0.0.1:10050 | 템플릿: Linux by Zabbix agent
```

- 이 기본 호스트는 **자기 자신(127.0.0.1)에 agent가 있다고 가정합니다.** 패키지로 설치하면 이 가정이 맞습니다.
- Docker에서는 server와 agent가 **서로 다른 컨테이너**에 있습니다. server 컨테이너 안의 `127.0.0.1:10050`에는 아무것도 없습니다.
- 그래서 `zabbix-agent` 호스트를 등록해도 **이 알람은 사라지지 않습니다.**

**확인 방법 (API)**
```
host.get → "Zabbix server" 의 interfaces: 127.0.0.1:10050, available=2 (연결 불가)
```

**해결** (`scripts/zabbix_config.py`의 `fix_default_server_host`, `apply`/`import` 시 자동 수행)
1. `Zabbix server` 호스트에서 `Linux by Zabbix agent` 템플릿을 **unlink and clear** 합니다. 템플릿이 만든 아이템과 트리거도 함께 삭제됩니다.
2. 쓰이지 않는 agent 인터페이스(127.0.0.1:10050)를 삭제합니다.
3. `Zabbix server health` 템플릿은 **유지**합니다. internal 아이템이라 agent가 필요 없고, Server 자체 상태를 감시합니다.
4. OS 지표는 별도 호스트 `zabbix-agent`가 담당합니다. 인터페이스는 IP가 아닌 DNS 이름 `zabbix-agent:10050`으로 연결합니다.

**대안으로 검토했지만 채택하지 않은 방법**: 기본 호스트의 인터페이스를 `zabbix-agent`로 바꾸기
- `zabbix-agent` 호스트와 **같은 OS 지표를 중복으로 수집**하게 됩니다.
- 또 호스트 이름(`Zabbix server`)과 agent의 `ZBX_HOSTNAME`(`zabbix-agent`)이 달라서, active check가 실패합니다(`host [zabbix-agent] not found`).

**함께 사라진 로그**: zabbix-server 로그에 반복되던 `cannot send list of active checks ... host [zabbix-agent] not found`
→ `zabbix-agent` 호스트를 등록하자 사라졌습니다.

---

## 2. HTTP 체크가 장애 시 0이 아니라 not supported가 된다

- **증상**: `pitwall_web`을 중지했는데 트리거가 발동하지 않습니다. 아이템은 새 값 없이 오류(not supported) 상태에 머뭅니다.
- **원인**: 전처리 순서
  1. `Check for not supported value` → 연결 오류를 `0`으로 치환 ✅
  2. `Regular expression`(상태 줄에서 코드 추출) → 입력이 `0`이니 **매칭 실패** → 아이템이 다시 오류 상태가 됨 ❌
- **해결**: 2단계에도 `Custom on fail: Set value to 0`을 설정합니다. 해석할 수 없는 응답도 장애로 보는 것이 맞으므로 의미상으로도 문제가 없습니다.
- **참고**: 컨테이너가 멈추면 Docker 내장 DNS에서 이름이 사라집니다. 그래서 오류는 "연결 거부"가 아니라 `Could not resolve host: pitwall_web`으로 나옵니다. 어느 쪽이든 1단계에서 `0`으로 바뀝니다.

---

## 3. 측정 중 데이터가 수십 분간 끊겼다 (로컬 한정)

- **증상**: 감지 시간 측정 도중 아이템 값이 38분간 전혀 수집되지 않았고, 측정 스크립트는 타임아웃으로 끝났습니다.
- **원인**: 맥북이 배터리 상태에서 **잠자기**에 들어갔습니다(`pmset -g log`: 17:46:49 Sleep → 18:25:18 Wake). Docker Desktop VM 전체가 멈춘 것이고, Zabbix 문제가 아닙니다.
- **해결**: 측정할 때는 `caffeinate -i python3 scripts/measure_detection.py`로 실행하고, 덮개를 닫지 않습니다.
- EC2에서는 해당 없습니다.

---

## 4. 🔴 [미해결] 컨테이너 재기동 후 HTTP 체크가 1~2회 더 실패한다

- **상태**: 미해결. Self-Healing 단계(2026-09-18)에서는 **재현만 확인**했고 원인 조사는 하지 않았습니다. 아래 "다음에 확인할 것"부터 이어서 진행합니다.
- **증상**: `docker start pitwall_web` 이후 Zabbix HTTP agent가 1~2회(15~30초) 더 `0`을 기록합니다.
  그 결과 복구 확인 시간이 설계 기대치(15초 이내)보다 길고 편차가 큽니다.
  5회 측정값은 9.5 / 14.2 / 29.8 / 29.8 / 44.9초입니다([zabbix-monitoring.md](zabbix-monitoring.md#측정-결과-장애-감지-및-복구-확인-시간)).
- **영향**: 장애 **감지**와 Self-Healing 동작에는 영향이 없습니다. 복구 이벤트가 늦게 닫힐 뿐입니다.
  다만 "재기동 후 복구 확인까지 N초" 같은 지표를 발표할 때는 이 문제를 알고 있어야 합니다.

**관찰된 사실**
- 3회차 예시: 18:28:22에 `start` → 18:28:37 `0`, 18:28:52 `0`, 18:29:07 `200`
- 같은 조건에서 `docker start` 직후 **zabbix-server 컨테이너 안에서 직접** 확인하면 **t+0초부터 모두 성공**했습니다.
  - `nslookup pitwall_web 127.0.0.11`
  - `wget http://pitwall_web/`
  - Docker 내장 DNS 등록이나 nginx 기동이 늦은 것은 아닙니다.
- 재현이 일정하지 않습니다. 중지 20초 후 수동으로 재기동했을 때는 다음 체크에서 바로 `200`이 나왔습니다.
- zabbix-server 이미지의 libcurl 버전은 `8.21.0`입니다.
- **Self-Healing 측정에서도 재현됐습니다(2026-09-18).** healer가 재기동을 마친 뒤에도 Zabbix 복구 이벤트까지 **10~45초가 더 걸렸습니다**(재기동 완료 평균 24.8초, Zabbix 복구 확인 평균 45.8초, [self-healing.md](self-healing.md)). 멈춰 있던 시간이 짧아도 발생합니다.
- 컨테이너가 멈춘 동안의 오류는 `Could not resolve host: pitwall_web`입니다. 연결 거부가 아니라 DNS 조회 실패입니다.

**가설 (미검증)**
- Zabbix HTTP agent poller가 libcurl 핸들을 재사용하면서 **DNS 조회 결과(실패 포함)를 캐시**하는 것으로 의심합니다.
  그렇다면 재기동으로 IP가 바뀌거나 이름이 다시 등록되어도, 캐시가 만료될 때까지 실패가 이어질 수 있습니다.

**다음에 확인할 것**
1. HTTP agent poller의 로그 레벨을 올려 재기동 직후 실패의 **실제 오류 메시지**를 확인합니다.
   `zabbix_server -R log_level_increase="http agent poller"`
   지금은 전처리가 오류를 `0`으로 바꿔서 원래 메시지가 남지 않습니다.
2. `docker stop`/`start` 전후로 컨테이너 IP가 바뀌는지 기록해, IP 변경과 실패가 관련 있는지 확인합니다.
3. 가설이 맞다면 대응책을 검토합니다. 예: compose에서 고정 IP를 할당하거나, 복구 조건을 확인하는 방식을 바꾸는 것.

---

## 5. export한 미디어 타입을 다시 import하면 스크립트가 달라진다

- **증상**: 새 Zabbix에 import한 뒤 다시 export하면 `mediatypes.yaml`만 원본과 다릅니다.
- **원인**: `zabbix/mediatypes/*.js` 파일은 끝에 줄바꿈이 있습니다. `apply`는 이 줄바꿈을 그대로 보내지만, YAML literal block은 import할 때 마지막 빈 줄을 지웁니다.
- **해결**: `apply`에서 스크립트 끝의 공백을 제거(`rstrip()`)합니다. 이렇게 하면 두 경로의 결과가 같아집니다. 수정 후 새 Zabbix에서 다시 검증해 4개 파일이 모두 동일함을 확인했습니다.

---

## 6. ⚠️ RCA가 의존 서비스 장애를 "외부 종료(SIGQUIT)"로 잘못 분류했다 — 근거 대조를 통과한 오답

> **핵심**: 신뢰도 high, 근거 3/3 원문 일치인데도 **틀린 답**이었습니다. 환각 검증은 "출력이 입력에 충실한가"만 보므로, **입력 자체가 잘못된 경우는 잡지 못합니다.** 발표용 정리: [rca.md — 근거 대조를 통과한 그럴듯한 오답](rca.md)

- **발생**: 2026-09-19, 시나리오 ① 첫 실행(event 753)
- **증상**: 원인은 pitwall_api 중단인데, AI 요약 첫 문장이 "외부에서 전달된 SIGQUIT에 따라 정상 종료"였습니다.
- **원인**: healer의 로그 스냅샷이 **이전 실행의 종료 로그**를 포함했습니다.
  - Docker API `logs?since=`는 **초 단위**입니다.
  - 이전 실행이 17:46:52.**76**에 종료(SIGQUIT)되고, 새 실행이 17:46:52.**99**에 시작되었습니다. 같은 1초 안이라 `since=17:46:52`에 두 실행의 로그가 섞였습니다.
  - `docker restart`는 종료와 시작이 같은 초에 일어나는 경우가 많습니다. 그래서 **재기동 이후의 다음 장애마다 재현될 수 있는 버그**였습니다.
- **해결**: 로그를 `timestamps=1`로 받아서, 컨테이너 `StartedAt`과 **나노초 단위로 비교**해 이후 줄만 남깁니다(`healer.logs_since_start`).
- **검증**: 수정 후 재실행(event 757)에서 분류 `dependency`, 근거 3/3 원문 일치를 확인했습니다.
- **처리 시점 확인**: event 753은 수정 전 healer(17:45:10 기동)가 처리했습니다(17:48:37). 수정은 17:50:59에 작성해 17:54:17에 배포했고, event 757(17:55:07)부터 수정된 healer가 처리했습니다(`healer.jsonl`의 `healer.started` 기록).
- **연쇄 관계**: 섞여 들어간 SIGQUIT는 배포 오탐(#8, event 750)으로 healer가 재기동하면서 남긴 종료 로그였습니다. #8의 불필요한 재기동이 #6 버그를 통해 다음 장애의 분석을 오염시켰습니다.

---

## 7. Slack 알림이 유실됐다 (외부 DNS 일시 실패)

- **발생**: 2026-09-19 17:47, 17:49 (로컬 Docker Desktop)
- **증상**
  - rca의 OpenAI 호출이 `[Errno -3] Try again`으로 2회 모두 실패했고, 그 Slack 대체 메시지도 실패했습니다(event 750).
  - healer의 "자동 복구 실패" Slack도 실패했습니다(event 753, `slack_notified: false`).
- **원인**: 컨테이너에서 외부 도메인 DNS 조회가 **순간적으로 실패**했습니다(EAI_AGAIN).
  - 14초 뒤 rca의 Slack 전송은 성공했고, 이후 조회도 정상이었습니다.
  - Docker Desktop 쪽의 일시적인 현상으로 보이며, 근본 원인은 확인하지 못했습니다.
- **문제**: `slack.post`가 **1회만 시도**해서, 일시적인 오류에도 알림이 유실되었습니다.
- **해결**: 네트워크 오류, 429, 5xx는 최대 3회 시도합니다(2초, 5초 간격). 4xx는 즉시 포기합니다.
  - 실제 전송 없이 검증했습니다: 존재하지 않는 호스트 → 3회 시도 후 7.1초에 실패, Slack 4xx → 0.3초에 즉시 실패.
- **남는 위험**: DNS 장애가 7초 이상 이어지면 알림은 여전히 유실됩니다. 이때는 Zabbix 에스컬레이션(Zabbix 서버가 직접 전송, 미디어 재시도 3회)이 보완합니다.

---

## 8. 배포(컨테이너 재생성)가 장애로 감지되어 자동 복구가 동작했다

- **발생**: 2026-09-19 17:46 (event 750)
- **증상**: 설정 변경을 반영하려고 `docker compose up`으로 pitwall_web을 재생성했습니다. 이때 약 16초 동안 서비스가 중단되었고, Zabbix가 이를 장애로 감지했습니다. 그 결과 healer가 **이미 정상으로 뜬 pitwall_web을 다시 재기동**했습니다.
- **영향**: 불필요한 재기동, RCA 호출(비용), Slack 알림이 발생합니다. EC2에서 배포할 때도 똑같이 일어납니다.
- **상태**: 🟡 **미해결 (대응 방안 제안)**
  - Zabbix **maintenance** 기능을 씁니다. 배포 전에 API로 대상 호스트를 maintenance에 넣고, 끝나면 해제합니다.
  - Action의 "maintenance 중 일시 중지" 설정(`pause_suppressed`)이 기본으로 켜져 있어, maintenance 동안에는 자동 복구가 실행되지 않습니다.
  - 배포 스크립트(`scripts/deploy.sh`)에 포함하는 것을 EC2 이전 단계에서 검토합니다.

---

## 참고: 컨테이너 agent에서 not supported인 아이템

`Linux by Zabbix agent`를 컨테이너 agent에 적용하면 일부 아이템이 not supported가 됩니다(로컬에서 154개 중 10개).
예: `system.sw.packages.get`(패키지 DB 없음). 컨테이너 환경에서 예상되는 동작입니다.
EC2에서 호스트 디스크 같은 지표가 필요하면 호스트의 `/`를 읽기 전용으로 마운트하는 방안을 검토합니다.
