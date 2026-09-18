# AWS EC2 이전 가이드

대상: EC2 t4g.medium (arm64), Amazon Linux 2023, 도메인 `sys-aims.duckdns.org`

> 🚧 작성 중입니다. 로컬 개발 중에 발견한 **"맥에서는 드러나지 않지만 EC2에서 문제가 될 항목"**을 먼저 모았습니다.

---

## ⚠️ 맥에서는 드러나지 않는 차이

### 1. `docker.sock` 권한: socket-proxy에 `DOCKER_GID` 설정 필요

| | 소켓 소유자 | 권한 | `.env`의 `DOCKER_GID` |
|---|---|---|---|
| 맥 (Docker Desktop VM) | `root:root` | 660 | `0` |
| **EC2 (Amazon Linux 2023)** | **`root:docker`** | 660 | **docker 그룹의 GID** |

- `wollomatic/socket-proxy`는 **비root 사용자(65534)**로 실행됩니다.
- 권한이 660이면 소유자와 소유 그룹만 소켓에 접근할 수 있습니다. 그래서 compose에서 `group_add`로 **소켓의 소유 그룹**을 추가해 줘야 합니다.
- 맥에서 쓰던 `DOCKER_GID=0`을 EC2에 그대로 가져가면 **권한 오류**가 납니다. EC2 소켓의 소유 그룹은 root(0)가 아니라 docker이기 때문입니다.

**로컬에서 재현해 확인한 증상** (소유 그룹이 아닌 GID 999를 준 임시 컨테이너, 2026-09-18)
```
level=ERROR msg="socket not available" error="dial unix /var/run/docker.sock: connect: permission denied"
→ 컨테이너 종료 (exit code 2)
```
- `-stoponwatchdog` 옵션 때문에 socket-proxy가 **기동 직후 바로 종료**됩니다.
- healer는 socket-proxy가 healthy가 되어야 시작하도록(`depends_on: service_healthy`) 되어 있습니다. 그래서 healer도 기동하지 않고, **Self-Healing 전체가 동작하지 않습니다.**
- 모니터링과 장애 감지는 정상으로 보이기 때문에 알아차리기 어렵습니다. 배포 직후 반드시 `ps`로 socket-proxy와 healer 상태를 확인하세요.

**EC2에서 할 일**
```bash
getent group docker | cut -d: -f3        # 예: 992
stat -c '%U:%G %a' /var/run/docker.sock  # root:docker 660 확인
# .env
DOCKER_GID=992                            # 위에서 확인한 값
```

**확인 방법**
```bash
docker compose ... ps socket-proxy        # (healthy) 인지
docker logs socket-proxy | grep -i -E "permission|denied|watchdog"
```

### 2. 컨테이너 agent의 디스크 지표
- 컨테이너 안에서 돌아가는 agent의 `vfs.fs.*` 지표는 호스트가 아니라 **컨테이너 파일시스템** 기준입니다.
- 호스트 디스크를 보려면 호스트의 `/`를 읽기 전용으로 마운트하는 방안을 검토해야 합니다([troubleshooting.md](troubleshooting.md) 참고).

### 3. 설치 직후 뜨는 "Zabbix agent is not available" 알람
- 새로 설치하면 반드시 발생합니다. `scripts/zabbix_config.py import`가 자동으로 정리합니다([troubleshooting.md #1](troubleshooting.md)).

---

## 이전 순서 (초안)

1. EC2에 Docker와 compose 플러그인을 설치하고, 사용자를 docker 그룹에 추가합니다.
2. 저장소를 clone한 뒤 `cp .env.example .env`로 값을 채웁니다. **`DOCKER_GID`와 `DOMAIN`을 반드시 확인하세요.**
3. `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --wait` (prod override는 작성 예정)
4. Zabbix 웹에서 Admin 비밀번호를 변경하고, `.env`의 `ZABBIX_API_*`를 설정합니다.
5. `python3 scripts/zabbix_config.py import`를 실행합니다. 템플릿, 호스트, 미디어 타입, Action과 Secret 매크로가 적용됩니다.
6. `python3 scripts/measure_detection.py --mode heal --trials 5`로 로컬 측정값과 비교합니다.
