# ADR-0001: Docker 소켓 접근 방식: socket-proxy 경유 (C안)

- **상태**: 채택 (2026-09-18), 2단계 구현 및 검증 완료 (2026-09-18)
- **관련 기능**: ① Self-Healing(컨테이너 재기동), ② AI RCA(`docker logs` 수집)

## 배경

Self-Healing과 RCA를 하려면 누군가 Docker API를 호출해야 합니다(컨테이너 재기동, 로그 조회).
가장 흔한 방법은 `/var/run/docker.sock`을 컨테이너에 마운트하는 것입니다.

## 핵심 사실: `docker.sock:ro`는 읽기 전용이 아니다

```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock:ro   # ← 안전해 보이지만 아니다
```

- `:ro`는 **소켓 파일 자체**를 읽기 전용으로 마운트할 뿐입니다. 파일을 지우거나 교체하는 것만 막습니다.
- 소켓으로 `connect()`해서 요청을 보내는 동작은 그대로 가능합니다. 그래서 **Docker API 전체**(생성·삭제·exec·이미지 pull 등)를 제한 없이 쓸 수 있습니다.
- Docker API를 쓸 수 있으면 사실상 **호스트 root 권한**을 가진 것과 같습니다.
  예를 들어 `-v /:/host --privileged`로 컨테이너를 만들 수 있습니다.

따라서 **소켓을 받은 컨테이너가 침해되면 곧 호스트 전체가 침해됩니다.**

## 검토한 선택지

| 안 | 방식 | 침해 시 영향 |
|---|---|---|
| A | zabbix-agent2에 원본 소켓 + Zabbix 원격 명령(`system.run`)으로 재기동 | 🔴 Server **또는** Agent 중 하나만 뚫려도 호스트 root |
| B | agent2에 원본 소켓(모니터링 전용) + 재기동은 별도 컨테이너 | 🟠 Agent가 뚫리면 호스트 root |
| **C** | **원본 소켓을 받는 컨테이너 없음.** socket-proxy로 허용한 API만 노출 | 🟢 **지정한 컨테이너 1개의 재기동과 상태 조회**로 한정 (아래 검증 결과) |

## 결정: C안

1. **장애 감지는 소켓 없이 합니다.**
   Zabbix Server의 HTTP agent 아이템으로 `pitwall_web`에 HTTP 체크를 합니다. 컨테이너가 죽으면 응답이 없으므로 감지됩니다.
2. **복구는 socket-proxy를 거칩니다.**
   원본 소켓은 socket-proxy만 가집니다. 이 프록시는 허용 목록에 있는 요청만 통과시킵니다.
3. **healer(Python)에서 한 번 더 제한합니다.**
   healer는 프록시를 통해서만 Docker에 접근하고, 코드에서도 `TARGET_CONTAINER`만 허용합니다(심층 방어).
4. **Agent는 원격 명령을 명시적으로 막습니다.**
   `ZBX_DENYKEY=system.run[*]`을 설정합니다.

---

## socket-proxy 선택: 왜 wollomatic인가

C안의 효과는 **프록시가 권한을 얼마나 잘게 제한할 수 있느냐**에 달려 있습니다.
후보 3개의 이미지를 직접 받아 설정 파일과 옵션을 확인했습니다(2026-09-18).

| 이미지 | 권한 제어 단위 | 확인한 사실 | "pitwall_web 재기동만" 허용 가능? |
|---|---|---|---|
| `tecnativa/docker-socket-proxy` | 기능 영역별 on/off (`CONTAINERS`, `POST` 등) | 재기동을 하려면 `CONTAINERS=1` + `POST=1`이 필요합니다. 이렇게 켜면 **컨테이너 생성·삭제 등 모든 POST**가 열립니다. | ❌ |
| `linuxserver/socket-proxy:3.4.4` | 위와 같음 + `ALLOW_RESTARTS`, `ALLOW_START` 등 | haproxy 설정(`/templates/haproxy.cfg` 49번째 줄)이 `containers/[a-zA-Z0-9_.-]+/((stop)\|(restart)\|(kill))` 입니다. 즉 **모든 컨테이너**에 대해 **stop과 kill까지** 허용합니다. | ❌ 컨테이너 이름으로 제한 불가 |
| **`wollomatic/socket-proxy:1.13.1`** | **HTTP 메서드별 정규식 허용 목록**. 지정하지 않은 메서드는 전부 거부 | `-allowPOST=<정규식>`으로 경로를 직접 지정합니다. `-allowfrom`으로 접속 출처도 제한합니다. 비root(65534)로 실행됩니다. | ✅ |

**정정**: 이 ADR의 초안에는 "재기동만 허용할 수 있는지 미검증"이라고 적었습니다.
조사해 보니 linuxserver에는 재기동 전용 옵션(`ALLOW_RESTARTS`)이 **있었습니다.** 하지만 **컨테이너 이름으로 제한할 수 없고**, stop과 kill까지 함께 열립니다.
이 옵션으로 막을 수 있는 것은 "호스트 탈취"까지이고, **다른 컨테이너(예: postgres)를 멈추는 것은 막지 못합니다.**
그래서 컨테이너 이름 단위로 제한할 수 있는 유일한 후보인 wollomatic을 선택했습니다.

**적용한 설정** (`docker-compose.yml`)
```
-allowfrom=healer
-allowGET=/v1\.[0-9]+/containers/${TARGET_CONTAINER}/json       # 재기동 후 상태 확인
-allowPOST=/v1\.[0-9]+/containers/${TARGET_CONTAINER}/restart   # 재기동
```
- 프록시는 정규식을 `^...$`로 감싸서 **경로 전체가 일치해야만** 통과시킵니다(기동 로그로 확인).
- 컨테이너 이름은 `.env`의 `TARGET_CONTAINER`를 compose가 치환해 넣습니다. 이름을 한 곳에서만 관리합니다.

---

## 검증 기록 1: 허용 목록 실제 호출 결과

- **일시**: 2026-09-18
- **환경**: 로컬 Docker Desktop, `wollomatic/socket-proxy:1.13.1`
- **방법**: healer 컨테이너(허용된 출처) 안에서 Python `http.client`로 프록시에 직접 요청

| # | 요청 | 기대 | 실제 응답 | 결과 |
|---|---|---|---|---|
| 1 | `GET /v1.44/containers/pitwall_web/json` | 허용 | **200** | ✅ 허용 |
| 2 | `POST /v1.44/containers/pitwall_web/restart?t=5` | 허용 | **204** (실제로 재기동됨) | ✅ 허용 |
| 3 | `POST /v1.44/containers/zabbix-agent/restart` | 거부 | **403** Forbidden | ✅ 거부 |
| 4 | `POST /v1.44/containers/postgres/restart` | 거부 | **403** Forbidden | ✅ 거부 |
| 5 | **`POST /v1.44/containers/create`** (alpine 이미지) | 거부 | **403** Forbidden | ✅ 거부, 컨테이너 생성 안 됨 |
| 6 | **`DELETE /v1.44/containers/pitwall_web`** | 거부 | **405** Method Not Allowed | ✅ 거부 (DELETE 메서드 자체가 차단됨) |
| 7 | `POST /v1.44/containers/pitwall_web/kill` | 거부 | **403** | ✅ 거부 |
| 8 | `POST /v1.44/containers/pitwall_web/stop` | 거부 | **403** | ✅ 거부 |
| 9 | `POST /v1.44/containers/pitwall_web/exec` | 거부 | **403** | ✅ 거부 |
| 10 | `GET /v1.44/containers/pitwall_web/logs` | 거부 | **403** | ✅ 거부 (RCA 단계에서 추가 예정) |
| 11 | `GET /v1.44/containers/json` (전체 목록) | 거부 | **403** | ✅ 거부 |
| 12 | `GET /v1.44/images/json` | 거부 | **403** | ✅ 거부 |
| 13 | `POST .../pitwall_web/../postgres/restart` (경로 조작) | 거부 | **403** | ✅ 거부 |
| 14 | `POST .../pitwall_web%2F..%2Fpostgres/restart` (URL 인코딩) | 거부 | **403** | ✅ 거부 |
| 15 | `POST .../pitwall_web/restart/../../postgres/restart` | 거부 | **403** | ✅ 거부 |

**부작용 확인**
- `allowlist-test` 컨테이너가 생성되지 않았습니다.
- `postgres`와 `zabbix-agent`의 `StartedAt`이 바뀌지 않았습니다. 재기동되지 않았다는 뜻입니다.
- 재기동된 것은 허용한 2번 요청의 `pitwall_web`뿐이었습니다.

**예상과 달랐던 점**: 6번은 403이 아니라 **405**였습니다. `-allowDELETE`를 지정하지 않아서 메서드 단계에서 먼저 막힌 것입니다. 거부된다는 결과는 같습니다.

## 검증 기록 2: 접속 출처와 네트워크 격리

| # | 상황 | 결과 |
|---|---|---|
| 16 | `docker_api` 망에 붙인 **healer가 아닌 컨테이너**에서 허용된 경로(#1) 요청 | **403**, 프록시 로그 `reason="forbidden IP"` ✅ |
| 17 | healer를 재생성해 **IP가 바뀐 뒤**(.3 → .4) 요청 | **200** ✅ (프록시가 요청마다 `healer` 이름을 다시 조회함) |
| 18 | 다른 컨테이너가 **healer의 예전 IP(.3)를 차지한 뒤** 요청 | **403** `forbidden IP` ✅ (예전 IP를 차지해도 사칭할 수 없음) |
| 19 | zabbix-server, zabbix-agent에서 `socket-proxy:2375` 접속 | 이름 조회 실패 ✅ (같은 망에 없음) |
| 20 | 호스트에서 `127.0.0.1:2375` 접속 | 연결 불가 ✅ (포트를 열지 않음) |

## 검증 기록 3: agent2 Docker 플러그인은 `unix://`만 지원

C안에서 agent2의 Docker 플러그인을 쓰지 않는 이유입니다.

- 대상: `zabbix/zabbix-agent2:alpine-7.0.30`
- 방법: `Plugins.Docker.Endpoint`를 바꿔 가며 `zabbix_agent2 -t docker.ping` 실행

| Endpoint | 결과 |
|---|---|
| `tcp://127.0.0.1:2375` | `ERROR: ... invalid plugin Docker configuration: Invalid endpoint format.` |
| `http://127.0.0.1:2375` | `ERROR: ... invalid plugin Docker configuration: Invalid endpoint format.` |
| `unix:///var/run/docker.sock` | 설정이 통과됨 (`docker.ping [s\|0]`: 소켓이 없어서 0) |

TCP로 동작하는 socket-proxy에는 agent2가 연결할 수 없습니다.
agent2의 Docker 플러그인을 쓰려면 원본 소켓을 줄 수밖에 없고, 그렇게 하면 B안이 됩니다. 그래서 사용하지 않습니다.

---

## 트레이드오프

- **잃는 것**: agent2의 Docker 플러그인(컨테이너 CPU·메모리 자동 수집, LLD).
  → 필요하면 healer가 프록시로 조회해서 Zabbix trapper로 보내는 방식으로 대체할 수 있습니다.
- **늘어나는 것**: 컨테이너 1개(socket-proxy, 메모리 상한 32M).
- **남는 위험**
  - socket-proxy 자체는 원본 소켓을 가지고 있습니다. 완화책: 외부 포트 없음, `docker_api` 내부 망에만 연결, 파일시스템 `read_only`, `cap_drop: ALL`, `no-new-privileges`, 비root 실행.
  - healer가 침해되면 공격자가 할 수 있는 일은 **`pitwall_web` 재기동**뿐입니다. healer의 서킷 브레이커가 반복 재기동도 제한합니다.
- **환경별 차이**: 프록시는 비root로 실행되므로 소켓 소유 그룹을 `group_add`로 추가해야 합니다.
  맥은 `root:root`, EC2는 `root:docker`입니다([aws-migration.md](../aws-migration.md)).

## 구현 단계

- [x] 1단계: 어떤 컨테이너에도 소켓을 마운트하지 않은 상태로 기본 스택 구성
- [x] 2단계: `docker-socket-proxy`(wollomatic) + healer 컨테이너 + Zabbix Action(webhook) 연동 ([self-healing.md](../self-healing.md))
- [ ] RCA 단계: 허용 목록에 `GET /containers/${TARGET_CONTAINER}/logs` 추가 후 재검증
