# AWS EC2 이전 가이드

- **대상**: EC2 **t4g.large** (arm64, 2 vCPU, 8GB), gp3 30GB, Amazon Linux 2023 (ARM), 서울 리전(ap-northeast-2)
- **도메인**: `sys-aims.duckdns.org` (Let's Encrypt HTTPS)

로컬(맥)에서 구현과 검증을 마친 뒤 EC2로 옮길 때 해야 할 작업입니다.
**⛔ 차단**으로 표시된 항목은 하지 않으면 해당 기능이 동작하지 않습니다.

---

## 0. 한눈에 보기

| # | 작업 | 구분 | 관련 |
|---|---|---|---|
| 1 | EC2·보안 그룹·Elastic IP 준비 | ⛔ 차단 | — |
| 2 | Docker·compose 플러그인 설치 | ⛔ 차단 | — |
| 3 | `.env` prod 값 (**`DOCKER_GID`**, `APP_ENV`, `DOMAIN`, `REPORT_BASE_URL`) | ⛔ 차단 | 아래 A |
| 4 | **`docker-compose.prod.yml` 작성**: Nginx(80/443) + certbot, 내부 포트 비공개 | ⛔ 차단 (작성 필요) | — |
| 5 | Nginx prod 설정: HTTPS, Zabbix Web 프록시, `/reports/` (Basic Auth) | ⛔ 차단 (작성 필요) | `nginx/snippets/reports.conf` |
| 6 | certbot 최초 발급 + 자동 갱신 | ⛔ 차단 | — |
| 7 | DuckDNS가 EC2 IP를 가리키게 설정 + IP 갱신 | ⛔ 차단 | — |
| 8 | Zabbix 초기화: Admin 비밀번호 변경 → `zabbix_config.py import` | ⛔ 차단 | troubleshooting #1, #9 |
| 9 | **배포 중 오탐 방지 (maintenance 모드)** | 권장 | troubleshooting #8 |
| 10 | **디스크 지표 수집** (호스트 `/` 읽기 전용 마운트) | 권장 | 아래 B |
| 11 | 스왑 파일 (2GB) | 선택 | t4g.large 8GB라 여유 있음 |
| 12 | 검증: 측정·시나리오·보고서·HTTPS | 필수 | 아래 C |
| 13 | 복구 확인 지연(#4) EC2에서 재측정 | 후속 | troubleshooting #4 |

---

## 1. 인프라 준비

**EC2**
- t4g.large (8GB), Amazon Linux 2023 (arm64), EBS gp3 30GB, 서울 리전 — 2026-09-19 확정

**보안 그룹 (인바운드)**
| 포트 | 대상 | 용도 |
|---|---|---|
| 22 | **내 IP만** | SSH |
| 80 | 0.0.0.0/0 | ACME 챌린지, HTTPS 리다이렉트 |
| 443 | 0.0.0.0/0 | HTTPS |

- 아웃바운드는 기본값(전체 허용)으로 둡니다. OpenAI, Slack, DuckDNS, Let's Encrypt, Docker Hub, PyPI에 접속해야 합니다.

**Elastic IP 권장**
- 인스턴스를 중지했다 시작하면 퍼블릭 IP가 바뀝니다. 그러면 DuckDNS 레코드가 틀어지고 인증서 갱신도 실패합니다.
- Elastic IP를 붙이면 DuckDNS는 한 번만 설정하면 됩니다.
- 그래도 IP가 바뀌는 경우를 대비해 갱신 스크립트(7번)는 둡니다.

## 2. Docker 설치 (Amazon Linux 2023)
```bash
sudo dnf install -y docker git
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user        # 재로그인 필요
# compose v2 플러그인 (arm64). 설치 시점의 최신 버전을 확인해 고정하세요
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -SL https://github.com/docker/compose/releases/download/<버전>/docker-compose-linux-aarch64 \
     -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
docker compose version
```

## A. `.env` prod 값 — ⚠️ 맥과 다른 것

| 키 | 로컬(맥) | **EC2** | 틀리면 |
|---|---|---|---|
| `DOCKER_GID` | `0` | **`getent group docker \| cut -d: -f3`** (예: 992) | socket-proxy가 기동 직후 종료 → **Self-Healing 전체 불능** (아래 설명) |
| `APP_ENV` | `local` | `prod` | — |
| `DOMAIN` | `localhost` | `sys-aims.duckdns.org` | 인증서·링크 오류 |
| `REPORT_BASE_URL` | (비움) | `https://sys-aims.duckdns.org/reports` | Slack에 보고서 링크 대신 파일명만 표시 |
| `ZABBIX_API_URL` | `http://localhost:8080/...` | prod에서 Zabbix Web 포트를 여는 방식에 맞춤 (4번 참고) | 설정 스크립트가 접속 실패 |

### `DOCKER_GID`: 맥에서는 드러나지 않는 차이

| | 소켓 소유자 | 권한 | `.env`의 `DOCKER_GID` |
|---|---|---|---|
| 맥 (Docker Desktop VM) | `root:root` | 660 | `0` |
| **EC2 (Amazon Linux 2023)** | **`root:docker`** | 660 | **docker 그룹의 GID** |

- `wollomatic/socket-proxy`는 **비root 사용자(65534)**로 실행됩니다. 그래서 `group_add`로 소켓의 소유 그룹을 추가해야 소켓을 읽을 수 있습니다.
- 맥에서 쓰던 `DOCKER_GID=0`을 그대로 쓰면 권한 오류가 납니다.

**로컬에서 재현해 확인한 증상** (소유 그룹이 아닌 GID 999를 준 임시 컨테이너, 2026-09-18)
```
level=ERROR msg="socket not available" error="dial unix /var/run/docker.sock: connect: permission denied"
→ 컨테이너 종료 (exit code 2)
```
- `-stoponwatchdog` 때문에 socket-proxy가 **기동 직후 종료**됩니다.
- healer는 socket-proxy가 healthy가 되어야 시작하므로 healer도 뜨지 않습니다.
- **모니터링과 장애 감지는 정상으로 보이기 때문에 알아차리기 어렵습니다.** 배포 직후 반드시 확인하세요.

```bash
getent group docker | cut -d: -f3        # 예: 992
stat -c '%U:%G %a' /var/run/docker.sock  # root:docker 660
docker compose ... ps socket-proxy healer   # 둘 다 (healthy)
```

## 4. `docker-compose.prod.yml` (작성 필요)

- **Nginx** 서비스 추가: 80, 443 공개. `reports_data:/srv/reports:ro`, `./nginx/snippets`, `./nginx/conf.d/prod`, `./nginx/auth`(htpasswd), 인증서 볼륨을 마운트합니다.
- **certbot** 서비스 추가: webroot 방식, 12시간마다 `certbot renew`.
- **내부 포트 비공개**: local override에서 열던 Zabbix Web 8080과 pitwall 8081은 prod에서 **호스트에 열지 않습니다.**
  - Zabbix Web은 Nginx를 거쳐 HTTPS로만 접근합니다. 공개 범위(교수님 열람 필요 여부)는 결정이 필요합니다.
  - 설정 스크립트용 접근: `127.0.0.1:8080` 바인딩만 유지하거나, SSH 터널을 씁니다.
- Nginx와 zabbix-web이 통신할 수 있도록 공용 네트워크(예: `zbx_front`)를 추가합니다.

## 5. Nginx prod 설정 (작성 필요)
- `:80`: `/.well-known/acme-challenge/` (certbot webroot), 나머지는 301로 HTTPS 리다이렉트
- `:443`: 인증서, 보안 헤더(HSTS 등), `location /` → zabbix-web:8080 프록시, `include snippets/reports.conf;`

**Basic Auth 파일 생성** (EC2, gitignore 대상)
```bash
mkdir -p nginx/auth
printf 'professor:%s\n' "$(openssl passwd -apr1)" > nginx/auth/reports.htpasswd   # 비밀번호 입력
```
- 이 ID와 비밀번호를 교수님께 전달합니다.

## 6. certbot
- 최초 발급: Nginx를 80만 연 상태(인증서 없이)로 띄우고 webroot로 발급합니다. 그다음 443 설정을 켭니다.
- 자동 갱신: certbot 컨테이너 루프 + 갱신 후 Nginx reload

## 7. DuckDNS
```bash
curl "https://www.duckdns.org/update?domains=${DUCKDNS_DOMAIN}&token=${DUCKDNS_TOKEN}&ip="   # 응답 OK
```
- Elastic IP를 쓰면 최초 1회면 됩니다.
- 쓰지 않으면 5분 주기 갱신 컨테이너(또는 systemd timer)가 필요합니다. 토큰은 `.env`에서 읽고 로그에 남기지 않습니다.

## 8. Zabbix 초기화
1. `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --wait`
2. 웹에서 Admin 비밀번호를 변경하고 `.env`의 `ZABBIX_API_PASSWORD`에 반영합니다.
3. `python3 scripts/zabbix_config.py import`를 실행합니다.
   - 템플릿, 호스트, 미디어 타입, Action, Secret 매크로, **읽기 전용 보고서 계정**이 한 번에 적용됩니다.
   - 새 Zabbix에서는 템플릿 연결이 15초를 넘을 수 있어, import 호출에만 120초 타임아웃을 줍니다([troubleshooting #9](troubleshooting.md)).
4. 설치 직후 뜨는 "Zabbix agent is not available" 알람은 import가 자동으로 정리합니다([troubleshooting #1](troubleshooting.md)).

## 9. 배포 중 오탐 방지: maintenance 모드 (권장)
- **문제**: `compose up`으로 컨테이너를 재생성하는 동안 Zabbix가 장애로 감지합니다. 그러면 healer가 불필요하게 재기동하고, RCA가 호출되고, Slack이 발송됩니다([troubleshooting #8](troubleshooting.md)).
  - 이때 남은 종료 로그가 다음 장애의 RCA 입력을 오염시킨 사례도 있습니다(#6 → [ai-trust.md](ai-trust.md)).
- **방안**: `scripts/deploy.sh`
  1. Admin API로 `maintenance.create`(대상 호스트 그룹 `Sys-AIMS`, 10분)를 실행합니다.
  2. `compose up -d --wait`를 실행합니다.
  3. 정상 확인 후 `maintenance.delete`를 실행합니다.
  - Action의 "maintenance 중 일시 중지"(`pause_suppressed`)가 기본으로 켜져 있어, 그 사이에는 자동 복구가 실행되지 않습니다.

## B. 디스크 지표 (권장)
- **현재**: 컨테이너 agent의 파일시스템 탐색에서 호스트 `/`가 잡히지 않습니다. 보고서에는 "미수집(사유)"로 표시됩니다.
- **방안**
  1. zabbix-agent에 `/:/hostfs:ro`를 마운트합니다.
  2. `zabbix-agent` 호스트에 `vfs.fs.size[/hostfs,pused]` 아이템을 추가합니다(`zabbix_config.py`).
  3. 보고서 수집 키(`collect.py`의 `disk`)를 교체합니다.
- **확인**: `docker exec zabbix-agent zabbix_agent2 -t 'vfs.fs.size[/hostfs,pused]'`

## 11. 스왑 (선택)
- 컨테이너 메모리 상한 합계는 약 1.7GB입니다(postgres 512M, zabbix-server 384M, web 256M, reporter 128M 등).
- t4g.large(8GB)로 확정되어 여유가 충분합니다. 이미지 빌드가 몰릴 때를 대비한 2GB 스왑은 선택 사항입니다.

## C. 이전 후 검증 (로컬과 같은 절차)

| 항목 | 명령 | 기대 |
|---|---|---|
| 전체 상태 | `docker compose ... ps` | 10개 서비스 healthy (Nginx, certbot 포함 시 +2) |
| socket-proxy 권한 | `docker logs socket-proxy` | `permission denied` 없음 |
| HTTPS | `curl -I https://sys-aims.duckdns.org` | 200/302, 유효한 인증서 |
| 보고서 인증 | `/reports/` 인증 없이 / 있이 | 401 / 200 |
| 자동 복구 측정 | `python3 scripts/measure_detection.py --mode heal --trials 5` | 로컬(평균 24.8초)과 비교 |
| 장애 시나리오 | `python3 scripts/chaos.py dependency --watch` → `restore` | 분류 `dependency` |
| 보고서 | `docker exec reporter python -m daily_report.run --hours 1` | 수집률 ~100%, Slack에 링크 |
| 복구 확인 지연 (#4) | 위 측정의 Zabbix 기준 다운타임 | 로컬 평균 45.8초와 비교 |
