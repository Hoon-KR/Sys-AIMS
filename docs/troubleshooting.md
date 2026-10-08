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

## 4. ⚠️ [원인 확인 · 미조치] 컨테이너 재기동 후 HTTP 체크가 1~2회 더 실패한다

- **상태**: 원인 확인, 조치하지 않음(2026-10-08). **libcurl의 네거티브 DNS 캐시(30초)** 때문입니다. 아래 "진단 결과".
  - 2026-09-18: 재현만 확인했습니다.
  - 2026-10-08 오전: VM-A 원격 실측으로 원인 범위를 **이름 해석 경로**로 좁혔습니다.
  - 2026-10-08 오후: mon에서 디버그 로그 + 컨테이너 IP 기록으로 (a)/(b)를 구분했습니다.
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
- **EC2 mon에서도 재현됐습니다(2026-10-06).** 재기동 완료 평균 20.0초, Zabbix 복구 확인 평균 44.9초.
- **IP로 확인하는 대상에서는 재현되지 않았습니다(2026-10-08, VM-A n=5).** 같은 Zabbix 서버(같은 libcurl)가
  `http://<VM-A 사설 IP>/healthz`를 확인했을 때, 복구 확인은 5회 모두 재기동 후 **정확히 한 주기(14.9~15.0초)**, 즉 추가 실패 0회였습니다
  ([self-healing.md — VM-A 원격 실측](self-healing.md)).
  mon의 pitwall_web과 다른 점은 **이름(`pitwall_web`)이 아니라 IP로 확인한다**는 것입니다.

**지금 데이터로 말할 수 있는 것**
- ~~Zabbix HTTP 체크의 재확인 때문~~ (self-healing.md의 기존 해석)은 **반증**됐습니다. 재확인 방식은 VM-A에서도 같은데 지연이 없었습니다.
- 플랫폼(Docker Desktop) 문제도 아닙니다. EC2 mon에서도 재현됩니다.
- 원인은 **이름 → 컨테이너 IP 해석 경로**로 좁혀집니다.

**가설** (VM-A 결과만으로는 구분 불가였음 — IP 체크는 둘 다 거치지 않으므로)
- (a) libcurl이 **DNS 조회 실패를 캐시**해, 이름이 다시 등록되어도 캐시가 만료될 때까지 실패가 이어진다.
- (b) 재기동으로 **컨테이너 IP가 바뀌고**, 이전 IP로의 연결이 한동안 이어진다.

**진단 결과 (2026-10-08, EC2 mon, heal 모드 n=3)**

방법: `zabbix_server -R log_level_increase="http agent poller"`(디버그 4) 상태에서
`measure_detection.py --mode heal --trials 3 --host pitwall_web`을 돌리고, 1초마다 `pitwall_web`의 상태와 IP를 기록했습니다.

| 회차 | 재기동 완료 | Zabbix 복구 확인 | 재기동 후 추가 실패 |
|---|---|---|---|
| 1 | 26.8s | 71.7s | 2회 |
| 2 | 24.1s | 34.0s | 0회 |
| 3 | 17.3s | 62.2s | 2회 |

- **(b) 배제**: 컨테이너 IP는 세 번의 재기동 내내 `172.20.0.2`로 **바뀌지 않았습니다**.
- **(a) 지지**: 디버그 로그에서 각 체크의 소요 시간과 소켓 이벤트 수가 세 종류로 갈립니다(1회차 예).

  | 시각 | 컨테이너 | 소요 | 소켓 이벤트 | 값 | 해석 |
  |---|---|---|---|---|---|
  | 11:58:28 | 중지 | **5.0s** | 7 | 0 | 캐시된 IP로 연결 시도 → 타임아웃(5s) |
  | 11:58:43 | 중지 | 6ms | 4 | 0 | DNS 질의 → 이름 없음 (**실패가 캐시됨**) |
  | 11:58:58 | **실행 중** | **1ms** | 2 | 0 | DNS 질의 없이 즉시 실패 = **캐시된 실패** |
  | 11:59:13 | **실행 중** | **1ms** | 2 | 0 | 〃 (실패 캐시 후 29.995초) |
  | 11:59:28 | 실행 중 | 6ms | 7 | 200 | 캐시 만료 → 새로 조회 → 성공 |

  3회차도 같은 모양입니다(실패 캐시 12:00:43 → 즉시 실패 2회 → 12:01:28 성공, 간격 29.99초).
- **libcurl 문서와 수치가 맞습니다.** [`CURLOPT_DNS_CACHE_TIMEOUT`](https://curl.se/libcurl/c/CURLOPT_DNS_CACHE_TIMEOUT.html):
  기본 60초, *"Since curl 8.16.0, failed name resolves are stored in the DNS cache for half the set timeout period"* → **30초**.
  mon의 zabbix-server는 `libcurl-8.21.0`입니다. 15초 주기로 보면 실패가 캐시된 뒤 **두 번(+15s, +30s 직전)** 더 실패하고 세 번째에 성공합니다.
- **재현이 들쭉날쭉했던 이유**(2회차): 정상일 때 캐시된 IP(양성 캐시 60초)가 장애 동안 만료되지 않으면, DNS를 다시 묻지 않고
  같은 IP로 연결만 실패하다가(5초 타임아웃 2회) 재기동 직후 바로 성공합니다. **장애 중에 이름을 새로 조회했는지**가 추가 실패 여부를 가릅니다.
  로컬의 9.5~44.9초 분산, "중지 20초 후 수동 재기동은 바로 성공"도 이것으로 설명됩니다.
- VM-A에 지연이 없던 것도 같은 설명입니다. IP 리터럴 URL은 DNS 캐시를 거치지 않습니다.

**한계**
- curl 오류 문자열(`Could not resolve host`) 자체는 이 로그 레벨에서 남지 않았습니다. "캐시된 실패"라는 판단은
  **소요 1ms·DNS 소켓 활동 없음**과 **정확히 30초 경계**라는 두 정황에 근거합니다.
- 이 경계에서 15초 주기의 두 번째 체크가 29.99초로 30초 직전에 걸렸습니다. 주기 위상이 조금만 달라도 추가 실패는 1회가 됩니다.

**조치 (보류)**
- curl 8.22.0부터는 "이름 없음" 응답만 캐시하지만, 컨테이너가 멈추면 Docker DNS가 바로 그 응답을 주므로 **업그레이드로는 해결되지 않습니다**.
- Zabbix HTTP agent는 `CURLOPT_DNS_CACHE_TIMEOUT`을 설정으로 노출하지 않습니다.
- 감지·자동 복구에는 영향이 없고 복구 이벤트가 최대 약 30초 늦게 닫힐 뿐이므로, 지금은 **알고 쓰는 것**으로 둡니다.
  발표에서 "Zabbix 화면 기준 다운타임"을 말할 때는 이 30초를 함께 설명합니다.

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

## 9. 새 Zabbix에 import 할 때 hosts.yaml 에서 타임아웃

- **발생**: 2026-09-19, 보고서의 데이터 부족 검증을 위해 새로 설치한 Zabbix에 import하던 중
- **증상**: `configuration.import`(hosts.yaml)이 클라이언트 타임아웃(15초)으로 실패했습니다. 다시 실행하니 1.9초 만에 끝났습니다. 서버에서는 첫 요청이 계속 처리되어 완료되어 있었습니다.
- **원인**: 새로 기동한 Zabbix에서 `Linux by Zabbix agent` 템플릿을 연결하면 아이템 약 150개를 생성합니다. 호스트가 늘어나면서(pitwall_api, rca) 15초를 넘기게 되었습니다.
- **영향**: **EC2에서 최초 import할 때 그대로 재현될 수 있습니다.**
- **해결**: `ZabbixAPI.call()`에 호출별 타임아웃을 추가하고, `configuration.import`에만 120초를 줍니다. 나머지 API 호출은 15초를 유지합니다.

---

## 10. EC2 첫 배포 직후 스케줄 보고서가 "계정이 차단되었습니다"로 실패했다

- **발생**: 2026-10-06, EC2 배포 당일 11:20 정기 보고서
- **증상**: 수집률 0%로 보고서가 생성되고, reporter 로그에 `Incorrect user name or password or account is temporarily blocked`가 남았습니다. 같은 날 13:46 수동 실행은 정상이었습니다.
- **원인**: **계정 차단이 아니라 계정이 아직 없었던 것입니다.** 읽기 전용 보고서 계정(`ZABBIX_REPORT_USER`)은 `scripts/zabbix_config.py import`가 만드는데, 11:20에는 import 전이었습니다. Zabbix는 사용자 열거(user enumeration)를 막기 위해 **"없는 계정"과 "틀린 비밀번호"에 같은 메시지**를 돌려줍니다.
- **차단 여부를 구분하는 방법**: `user.get`의 `attempt_failed`(연속 실패 횟수)와 `attempt_clock`(마지막 실패 시각)을 봅니다. Zabbix 기본값은 **5회 연속 실패 시 30초 차단**이고 자동으로 풀립니다. 스케줄러는 실행당 1회만 시도하므로 5회에 도달할 수 없습니다.
  ```bash
  cd ~/sys-aims && python3 -c "
  import sys,time; sys.path.insert(0,'automation')
  from common.zabbix_api import ZabbixAPI
  api=ZabbixAPI.from_env()
  for u in api.call('user.get',{'output':['username','attempt_failed','attempt_clock']}):
      print(u['username'], '실패', u['attempt_failed'], '회')
  api.logout()"
  ```
  실제로 차단된 경우에만 `user.unblock`(또는 웹 UI의 Users → Unblock)으로 해제합니다.
- **대응**: 순서를 지키면 발생하지 않습니다 — **`import`를 먼저, 그 다음 스케줄 보고서**. 첫 배포가 `REPORT_TIME` 직전이면 첫 정기 보고서 1회는 건너뛰고 수동 실행(`docker exec reporter python -m daily_report.run`)으로 확인합니다.

---

## 11. 🔴 Action 조건이 맞는데 알림이 0건이었다 — 알림 계정의 호스트 권한

- **발생**: 2026-10-07, 계열사 VM 분리 후 `A-pitwall_web` 의 첫 자동 복구 검증
- **증상**: 아래가 모두 정상인데 **`alert.get` 이 0건**이고 healer 에 요청이 들어오지 않았습니다.
  - 열린 문제 존재, 태그 `healing=auto` / `company=A` / `container=pitwall_web`
  - Action `status=0`(enabled), 조건 `conditiontype=26`(이벤트 태그 값) `healing == auto`
  - `sys-aims-bot` 활성(`users_status=0`), 미디어 2개 `active=0`, `severity=63`, 24/7
  - 미디어 타입 둘 다 `status=0`, escalator/alerter 프로세스 정상
  - **Zabbix 서버 로그에 아무 기록도 없음**
- **원인**: **알림 대상 사용자는 이벤트가 발생한 호스트에 최소 읽기 권한이 있어야 합니다.**
  `sys-aims-bot` 의 사용자 그룹(`Sys-AIMS Automation`)은 `Sys-AIMS` 에만 권한이 있었고,
  새로 만든 계열사 호스트는 `Sys-AIMS/A` 에 있었습니다. `sys-aims-bot` 의 역할은
  `User role` 이라 Super admin 처럼 권한을 우회하지 못합니다.
  **Zabbix 는 이 거부를 로그에 남기지 않습니다.** 그래서 설정이 전부 맞는데도 조용히 아무 일도
  일어나지 않습니다. 호스트 그룹은 **상위 그룹 권한이 하위(`Sys-AIMS/A`)로 자동 전파되지 않습니다.**
- **왜 어제까지 괜찮았나**: mon 의 `pitwall_web` 은 `Sys-AIMS` 에 있어 권한 범위 안이었습니다.
  계열사 그룹을 새로 만든 순간부터 그 그룹의 호스트만 알림이 끊겼습니다.
- **해결**: 봇 사용자 그룹의 `hostgroup_rights` 를 계열사 그룹까지 확장했습니다
  (`scripts/zabbix_config.py` 의 `AUTOMATION`). 보고서 읽기 전용 계정(`REPORT_ACCESS`)은 같은
  변경을 이미 했는데 알림 계정을 빠뜨린 것이 원인이었습니다. **호스트 그룹을 추가할 때는
  두 사용자 그룹을 함께 늘려야 합니다.**
- **진단 순서**(같은 증상이 또 나오면): Action 조건보다 **권한을 먼저** 봅니다.
  ```bash
  python3 -c "
  import sys; sys.path.insert(0,'automation')
  from common.zabbix_api import ZabbixAPI
  api = ZabbixAPI.from_env()
  for name in ['Sys-AIMS Automation', 'Sys-AIMS Read-only']:
      ug = api.call('usergroup.get', {'filter': {'name': name}, 'selectHostGroupRights': 'extend'})[0]
      ids = [r['id'] for r in ug['hostgroup_rights']]
      groups = api.call('hostgroup.get', {'groupids': ids, 'output': ['name']})
      print(name, '→', sorted(g['name'] for g in groups))
  api.logout()"
  ```

---

## 12. 서버의 `git pull` 이 분기로 실패해 수정 코드가 반영되지 않았다

- **발생**: 2026-10-07, 4-E 디버깅 중. mon 에서 `git pull` 이 실패하거나 분기된 상태였고,
  고친 코드가 서버에 없는 채로 원인을 찾고 있었습니다.
- **원인**: mon 에서 직접 커밋한 뒤(작성자 `Ubuntu <ubuntu@ip-...>`), 맥에서 그 커밋을
  `git am` 으로 가져와 **다른 해시**로 다시 만들고 `--force-with-lease` 로 origin 을 고쳤습니다.
  mon 의 로컬 커밋과 origin 의 커밋은 내용이 같아도 **다른 커밋**이라 pull 이 병합하지 못합니다.
- **해결**: mon 의 작업 트리는 버릴 수 있으므로 origin 으로 맞춥니다.
  ```bash
  cd ~/sys-aims && git fetch origin && git reset --hard origin/main
  git log -1 --oneline        # origin/main 과 같은 해시인지 확인
  ```
  `reset --hard` 는 추적되지 않는 파일(`.env`, `.env.agent`, `nginx/auth/`)을 지우지 않습니다.
  그래도 실행 전에 `git status --short` 로 커밋하지 않은 변경이 없는지 확인하세요.
- **재발 방지**: **mon 에서는 커밋하지 않습니다.** 서버에서 새로 생기는 파일은
  `zabbix/templates/` 의 export 결과뿐이므로, 맥으로 복사해 맥에서 커밋합니다.
  ```bash
  # ⬇️ 맥에서
  scp 'sys-aims-mon:~/sys-aims/zabbix/templates/*' ./zabbix/templates/
  git add zabbix/templates/ && git commit && git push
  ```
  서버에 GitHub 자격증명을 두지 않아도 되고(공개 저장소라 `pull` 은 인증이 필요 없음),
  커밋 작성자가 EC2 내부 호스트명으로 찍히는 문제도 사라집니다.
- **덧붙여**: EC2 기본 작성자 이메일(`ubuntu@ip-172-31-45-141.ap-northeast-2.compute.internal`)은
  **사설 IP 를 담은 내부 호스트명**입니다. `common/redact()` 가 AI 전송에서 가리는 것과 같은
  종류의 식별자이므로 공개 저장소 이력에 남기지 않는 편이 일관됩니다.

---

## 13. ⚠️ 맥에서 SSH 터널이 조용히 실패해 로컬 Zabbix에 접속했다

- **발생**: 2026-10-08, 5-A(VM-A 원격 측정) 준비 중. 맥에서 `scripts/measure_detection.py`를 돌리자
  `ZABBIX_API_*` 환경변수를 서버 값으로 설정했는데도 `user.login`이 실패했습니다. 같은 코드가 mon에서는 정상이었습니다.
- **원인**: 맥의 `127.0.0.1:8080`은 **로컬 Docker Zabbix**(`zabbix-web`)가 이미 점유하고 있습니다.
  `ssh -L 8080:...`는 bind에 실패해도 경고 한 줄만 남기고 연결을 유지하므로, 터널이 없는 채로
  `http://127.0.0.1:8080/api_jsonrpc.php` 요청이 전부 **로컬 Zabbix**로 갔습니다.
  - 환경변수/`.env` 우선순위 문제는 아니었습니다. `load_env()`는 환경변수가 `.env`를 덮어씁니다.
- **위험**: 이번에는 비밀번호가 달라 로그인 실패로 막혔지만, 같았다면 오류 없이 진행됐습니다.
  - `zabbix_config.py`가 **운영 설정을 로컬 Zabbix에 적용**하거나,
  - **로컬 측정값을 EC2 값으로 착각**해 문서에 남길 수 있었습니다.
- **확인**: 터널을 열기 전에 로컬 포트가 비어 있는지 봅니다.
  ```bash
  lsof -nP -iTCP:8080 -sTCP:LISTEN     # com.docke… 가 보이면 로컬 Docker가 점유 중
  ```
  터널을 연 뒤에는 `ssh` 프로세스가 그 포트를 잡고 있는지 확인합니다(`lsof` 결과의 COMMAND가 `ssh`).
- **해결**: 터널은 로컬 Zabbix와 겹치지 않는 **18080**으로 엽니다.
  ```bash
  ssh -N -o ExitOnForwardFailure=yes -L 18080:127.0.0.1:8080 sys-aims-mon
  export ZABBIX_API_URL=http://127.0.0.1:18080/api_jsonrpc.php
  ```
  `ExitOnForwardFailure=yes`를 주면 bind에 실패할 때 ssh가 바로 종료되어 "조용한 실패"가 사라집니다.
- **교훈**: 이번 건의 핵심은 포트 충돌이 아니라 **실패가 조용했다는 것**입니다.
  터널은 항상 `ExitOnForwardFailure=yes`로 열어, 연결이 안 됐으면 그 자리에서 멈추게 합니다.
  `~/.ssh/config`의 해당 호스트에 `ExitOnForwardFailure yes`를 넣어 두면 빠뜨릴 일이 없습니다.

---

## 참고: 컨테이너 agent에서 not supported인 아이템

`Linux by Zabbix agent`를 컨테이너 agent에 적용하면 일부 아이템이 not supported가 됩니다(로컬에서 154개 중 10개).
예: `system.sw.packages.get`(패키지 DB 없음). 컨테이너 환경에서 예상되는 동작입니다.
EC2에서 호스트 디스크 같은 지표가 필요하면 호스트의 `/`를 읽기 전용으로 마운트하는 방안을 검토합니다.
