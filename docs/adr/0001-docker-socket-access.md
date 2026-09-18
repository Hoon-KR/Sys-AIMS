# ADR-0001: Docker 소켓 접근 방식: socket-proxy 경유 (C안)

- **상태**: 채택 (2026-09-18)
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
| **C** | **원본 소켓을 받는 컨테이너 없음.** `docker-socket-proxy`로 허용 API만 노출 | 🟢 컨테이너 재기동과 조회·로그로 한정 |

## 결정: C안

1. **장애 감지는 소켓 없이 합니다.**
   Zabbix Server의 HTTP agent 아이템으로 `pitwall_web`에 HTTP 체크를 합니다. 컨테이너가 죽으면 응답이 없으므로 감지됩니다.
2. **복구와 로그 조회는 socket-proxy를 거칩니다.**
   원본 소켓은 socket-proxy만 가집니다. 이 프록시는 허용 목록(예: 컨테이너 조회·재기동·로그)에 있는 API 요청만 통과시킵니다.
3. **healer(Python)에서 한 번 더 제한합니다.**
   healer는 프록시만 통해 Docker에 접근하고, 코드에서도 `TARGET_CONTAINER`만 허용합니다(심층 방어).
4. **Agent는 원격 명령을 명시적으로 막습니다.**
   `ZBX_DENYKEY=system.run[*]`을 설정합니다.

## 검증 기록: agent2 Docker 플러그인은 `unix://`만 지원

C안에서 agent2의 Docker 플러그인을 쓰지 않는 이유입니다. 기억에 의존하지 않고 직접 실행해 확인했습니다.

- 대상: `zabbix/zabbix-agent2:alpine-7.0.30`
- 방법: `Plugins.Docker.Endpoint`를 바꿔 가며 `zabbix_agent2 -t docker.ping` 실행

| Endpoint | 결과 |
|---|---|
| `tcp://127.0.0.1:2375` | `ERROR: ... invalid plugin Docker configuration: Invalid endpoint format.` |
| `http://127.0.0.1:2375` | `ERROR: ... invalid plugin Docker configuration: Invalid endpoint format.` |
| `unix:///var/run/docker.sock` | 설정이 통과됨 (`docker.ping [s\|0]`: 소켓이 없어서 0) |

TCP로 동작하는 socket-proxy는 agent2가 연결할 수 없습니다.
agent2의 Docker 플러그인을 쓰려면 원본 소켓을 줄 수밖에 없고, 그렇게 하면 B안이 됩니다. 그래서 사용하지 않습니다.

## 트레이드오프

- **잃는 것**: agent2의 Docker 플러그인(컨테이너 CPU·메모리 자동 수집, LLD).
  → 필요하면 healer가 프록시로 조회해서 Zabbix trapper로 보내는 방식으로 대체할 수 있습니다.
- **늘어나는 것**: 컨테이너 1개(socket-proxy, 수십 MB).
- **남는 위험**: socket-proxy 자체는 원본 소켓을 가지고 있습니다. 대신 외부 포트를 열지 않고 내부 전용 네트워크에만 연결해 노출면을 최소화합니다.

## 구현 단계

- [x] 1단계: 어떤 컨테이너에도 소켓을 마운트하지 않은 상태로 기본 스택 구성
- [ ] 2단계: `docker-socket-proxy` 추가 + healer 컨테이너 + Zabbix Action(webhook) 연동
