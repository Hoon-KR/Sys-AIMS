# ADR-0002: 다른 서버(계열사 VM)의 컨테이너 재기동 방식: VM별 socket-proxy (A안)

- **상태**: 채택 (2026-10-06)
  - VM-A 에서 socket-proxy 경로 검증 (2026-10-06, 아래 "검증 기록")
  - VM-A·VM-B 에서 자동 복구·장애 시나리오·격리 검증 완료 (2026-10-08, 아래 "검증 기록: 5-A / 5-B")
- **관련 기능**: ① Self-Healing(원격 컨테이너 재기동), ② AI RCA(원격 `docker logs` 수집)
- **전제**: [ADR-0001](0001-docker-socket-access.md) — 원본 소켓을 받는 컨테이너를 두지 않는다

## 배경

감시 대상을 감시 서버(mon)의 컨테이너에서 **별도 EC2 2대**(VM-A = 계열사 A, VM-B = 계열사 B)로
분리합니다. mon 의 healer 가 **다른 호스트**의 컨테이너를 재기동해야 합니다.

ADR-0001 은 같은 호스트 안에서 소켓을 다루는 문제였고, 여기서는 **네트워크를 건너갑니다.**

## 검토한 선택지

| 안 | 방식 | 침해 시 영향 | healer 기능 |
|---|---|---|---|
| **A** | **VM 마다 socket-proxy. healer 가 사설망으로 2375 접속** | 🟢 해당 VM 의 `pitwall_web` 재기동·조회로 한정 | 🟢 전부 유지 |
| B | Zabbix Agent 의 원격 명령(`system.run`)으로 재기동 | 🔴 Agent 에 임의 명령 실행 경로가 생기고 `sudo` 필요 | 🔴 서킷 브레이커·healthy 검증·RCA 연계 **상실** |
| C | mon → VM SSH + `authorized_keys` 의 `command=` 제한 | 🟢 강함 | 🟡 유지 가능하나 healer 에 SSH 클라이언트·키 관리가 들어온다 |

## 결정: A안

B 는 작업량이 가장 적지만 **ADR-0001 이 세운 "셸 없음 · 화이트리스트만" 원칙을 스스로 뒤집습니다.**
또한 복구 주체가 healer 에서 Agent 로 넘어가면서 서킷 브레이커, 재기동 후 healthy 검증,
RCA 스냅샷 연계가 모두 사라집니다. 이 세 가지가 이 시스템의 핵심이라 채택하지 않았습니다.

C 는 보안으로는 가장 단단하지만 발표 일정(10일) 안에서 SSH 키 배포와 회전까지 다루기 어렵습니다.

A 는 **ADR-0001 의 구조를 그대로 확장**합니다. VM 마다 socket-proxy 를 두고, 허용 목록은
그 VM 의 `pitwall_web` 에 대한 3개 요청뿐입니다.

## 완화 수단 4겹

| # | 수단 | 내용 |
|---|---|---|
| 1 | **보안 그룹** | VM 의 2375 는 **mon 의 SG 를 소스로 참조**해서만 허용. CIDR(`172.31.0.0/16`)을 쓰지 않는다 — VPC 의 모든 인스턴스가 들어올 수 있기 때문 |
| 2 | **포트 바인딩** | `${VM_PRIVATE_IP}:2375:2375` — 사설 IP 에만 바인딩. `0.0.0.0` 이면 보안 그룹 실수 한 번에 인터넷에 열린다 |
| 3 | **`-allowfrom`** | `<mon 사설 IP>/32`. 이 플래그는 정규식 호스트명이 아니라 **IP/CIDR** 을 받는다(`-h` 로 확인). 역방향 DNS 에 의존하지 않는다 |
| 4 | **허용 목록** | `GET /containers/pitwall_web/(json\|logs)` 와 `POST /containers/pitwall_web/restart` **3개뿐**. 그 외 메서드는 지정하지 않아 메서드 단계에서 막힌다 |

추가로 **Zabbix Agent 에는 `ZBX_DENYKEY=system.run[*]`** 을 명시해, 재기동 경로가 socket-proxy
하나뿐임을 설정으로도 못 박았습니다.

## 수용된 위험: 2375 는 평문 HTTP 다

- VPC 사설망 안이지만 **암호화되지 않습니다.** 같은 VPC 에서 트래픽을 볼 수 있는 위치를
  확보한 공격자는 재기동 요청을 관찰하거나 위조할 수 있습니다.
- 위조에 성공해도 **할 수 있는 일은 그 VM 의 `pitwall_web` 재기동뿐**입니다(완화 #4). 컨테이너
  생성·삭제·exec·다른 컨테이너 접근은 불가능합니다. 피해 상한이 "서비스 1개의 재기동"으로 묶여 있어
  발표 범위에서는 수용합니다.
- **socket-proxy 1.13.1 에는 TLS 옵션이 없습니다**(`-h` 전체 확인: `-allowfrom`, `-listenip`,
  `-proxyport`, `-proxysocketendpoint` 등. TLS 관련 플래그 없음).

**업그레이드 경로** (운영 전환 시)

1. **WireGuard** 로 mon ↔ VM 사설 터널을 만들고 2375 를 터널 인터페이스에만 바인딩 — 설정이 가장 단순하고 양방향 인증이 된다
2. **stunnel / Nginx stream** 으로 양쪽에 TLS 종단을 두고 클라이언트 인증서를 요구
3. ADR-0002 의 **C안(SSH + `command=` 제한)** 으로 전환 — 추가 데몬 없이 암호화와 인증을 동시에 얻는다

## healer 변경: 전역 서킷은 멀티 테넌트에서 결함이다

대상이 1개일 때 서킷 브레이커는 전역 하나였습니다. 그대로 VM 2대를 붙이면
**계열사 A 의 플래핑이 계열사 B 의 자동 복구까지 막습니다.** 그래서 다음을 바꿨습니다.

| 항목 | 이전 | 이후 |
|---|---|---|
| 대상 지정 | `TARGET_CONTAINER` 1개 | `HEAL_TARGETS`(JSON). 라우팅 키 `company/container` |
| Docker 접근 | 고정 `socket-proxy:2375` | 대상별 프록시 주소 |
| 서킷 브레이커 | **전역 1개** | **대상별** |
| `/reset` | 전체 해제만 | 대상 지정 해제 + 전체 해제(측정 스크립트 호환) |
| 상태 파일 | `{attempts, circuit_open}` | `{targets: {"<키>": {...}}}` — 구버전은 `internal` 대상으로 **이관** |

- `company` 가 없는 요청은 `internal`(감시 서버 자신)로 봅니다. `HEAL_TARGETS` 가 비어 있으면
  VM 분리 전 구성 그대로 동작하므로, 로컬 개발과 기존 배포가 깨지지 않습니다.
- 목록에 없는 `(company, container)` 조합은 **403** 입니다. 프록시가 이미 막지만 코드에서도 확인합니다(심층 방어).
- 구버전 상태 파일을 그냥 버리면 **열려 있던 서킷이 조용히 풀려** 무한 재기동이 가능해집니다. 그래서 이관합니다.
- 테스트: `automation/tests/test_healer_targets.py` — 특히 "A 의 서킷이 열려도 B 는 복구된다"를 고정합니다.

## 검증 기록: VM-A (2026-10-06)

mon 의 **healer 컨테이너에서** 호출했습니다. 호스트에서 `curl` 로 재는 것은 실제 클라이언트를
검증한 것이 아닙니다 — 컨테이너에서 나가는 트래픽은 브리지 NAT 를 거쳐 호스트 사설 IP 로
바뀌므로, 그 주소가 `-allowfrom` 을 통과하는지가 관건입니다.

| # | 요청 | 기대 | 결과 |
|---|---|---|---|
| 1 | `GET /v1.44/containers/pitwall_web/json` | 허용 | **200** ✅ |
| 2 | `GET /v1.44/containers/pitwall_web/logs?stdout=1&stderr=1&...` | 허용 | **200** (640 bytes) ✅ |
| 3 | `POST /v1.44/containers/pitwall_web/restart?t=5` | 허용 | **204**, 실제로 재기동됨 ✅ |
| 4 | `GET /v1.44/containers/pitwall_api/json` | 거부 | **403** ✅ |
| 5 | `GET /v1.44/containers/json` (전체 목록) | 거부 | **403** ✅ |
| 6 | `POST /v1.44/containers/pitwall_web/kill` | 거부 | **403** ✅ |
| 7 | `POST /v1.44/containers/pitwall_web/stop` | 거부 | **403** ✅ |
| 8 | `GET /v1.44/images/json` | 거부 | **403** ✅ |
| 9 | **VM-B → VM-A:2375** (계열사 간) | 차단 | **타임아웃** ✅ |

**예상과 달랐던 점**: 파라미터 없는 `GET .../logs` 는 403 이 아니라 **400** 이었습니다. Docker API 가
`stdout`/`stderr` 중 하나 이상을 요구하기 때문입니다. 거부(403)가 아니라 **허용 목록을 통과한 뒤
Docker 가 돌려준 응답**이므로, 2번에서 파라미터를 붙여 200 을 확인했습니다.

**9번이 중요합니다.** VM-A 와 VM-B 는 같은 보안 그룹을 공유하지만, 인바운드 규칙에 "소스 = 자기 자신"을
넣지 않았기 때문에 서로 닿지 못합니다. **계열사 간 격리가 보안 그룹 수준에서 보장**됩니다.

## 계열사 활성화 게이트: `HEALING_MODE_<회사>`

VM 을 붙였다고 바로 자동 복구가 켜지지 않습니다.

- `scripts/zabbix_config.py` 는 계열사 서비스 호스트를 **`{$HEALING.MODE}=off` 로 등록**합니다.
  Action 은 `healing=auto` 태그 조건으로만 발동하므로, off 인 동안에는 장애가 **감지만** 되고
  healer 호출도 RCA(외부 AI 호출·비용)도 일어나지 않습니다.
- 연결을 눈으로 확인한 뒤 mon 의 `.env` 에서 `HEALING_MODE_<회사>=auto` 로 바꾸고 `apply` 합니다.
  `<회사>-pitwall_api` 와 `vm-<회사>-os` 는 항상 off 입니다(복구 대상이 아님).
- **실제로 동작을 확인했습니다**(2026-10-08). B 를 켜기 전에 5-B 를 실행하자 Zabbix 는 장애를 감지했지만
  (event 151, `healing=off`) Action·healer·RCA 는 **0건**이었습니다. 켠 뒤에는 정상적으로 이어졌습니다.

## 검증 기록: 5-A / 5-B (2026-10-08)

맥에서 docker context(`vm-a`, `vm-b`, `mon`)와 SSH 터널로 실행했습니다. mon 에는 VM 접속 자격증명이 없습니다.

**5-A 자동 복구 (VM-A, n=5)** — 5회 모두 사람 개입 없이 복구, Action 5/5 `sent`.
감지 → 재기동 0.0~0.1초로, 원격 socket-proxy 경유 비용은 무시할 수준입니다
(수치: [self-healing.md — VM-A 원격 실측](../self-healing.md)).

**5-B 장애 시나리오 (2 시나리오 × 2 VM, 각 1회)** — [chaos-scenarios.md](../chaos-scenarios.md)

| VM | 시나리오 | RCA 분류 | 신뢰도 | 근거 원문 일치 | 복구 후 열린 문제 |
|---|---|---|---|---|---|
| A | ① 의존 서비스 | `dependency` ✅ | high | 3/3 | 0 |
| A | ② 설정 오류 | `config_error` ✅ | high | 2/2 | 0 |
| B | ① 의존 서비스 | `dependency` ✅ | high | 3/3 | 0 |
| B | ② 설정 오류 | `config_error` ✅ | high | 2/2 | 0 |

**격리** — 매 시나리오 전후로 다른 호스트의 `pitwall_web`·`pitwall_api` `StartedAt` 을 비교했습니다.
A 시나리오 동안 VM-B·mon, B 시나리오 동안 VM-A·mon 은 **한 번도 재기동되지 않았습니다.**
대상별 서킷의 시도 횟수는 5-A 에서 확인했습니다(A 1회, B·internal 0회).

> VM-B 에 대해서는 위 9개 요청의 허용/거부 표를 따로 돌리지 않았습니다. 같은 compose·같은 허용 목록이고,
> 5-B 에서 healer 의 조회·로그·재기동이 B 의 프록시로 정상 동작한 것까지만 확인했습니다.

## 기술 부채: `chaos.py` 의 SSH 의존 (수용)

- **무엇**: 5-B 실행을 위해 `scripts/chaos.py` 에 SSH 의존을 추가했습니다(`--target-context`).
  ② 설정 오류 시나리오는 VM 의 `conf.d`(컨테이너에는 `:ro` 바인드 마운트)에 파일을 써야 하므로,
  docker context 의 `ssh://` 주소로 VM 호스트에 직접 쓰고 지웁니다.
- **원칙은 깨지지 않았습니다**: 이 스크립트는 **맥에서만 실행**하는 전제입니다. mon 은 여전히
  VM 자격증명을 갖지 않습니다. "mon 에 VM 키를 두지 않는다"는 원래 근거는 그대로 유효합니다.
- **그래도 부채인 이유**: 코드에 SSH 의존이 들어간 것은 사실입니다. 나중에 mon 이나 CI 에서
  장애 주입을 돌리려 하면, 그곳에 VM 키를 둬야 하는 문제가 다시 생깁니다.
- **갚는 방법**: 원안대로 **주입은 VM 에서 실행하고, 관찰은 mon 에서** 하도록 분리합니다
  (예: VM 에 주입 스크립트를 두고 실행만 트리거, `--watch` 는 mon 의 Zabbix·이벤트 로그만 읽음).
- **판단**: 발표 범위에서는 **수용된 선택**입니다.

## 남는 과제

- 2375 암호화 (위 업그레이드 경로)
- 계열사별 복구 정책 분리(`MAX_RESTARTS`, 쿨다운). 현재는 전 계열사 공통 — [ai-governance.md](../ai-governance.md)에서 설계로 다룹니다
- `chaos.py` 의 SSH 의존 해소 (위 기술 부채)
