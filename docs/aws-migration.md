# AWS EC2 이전 가이드

| 항목 | 확정 스펙 (2026-09-19, AWS 제약) |
|---|---|
| 인스턴스 | **t3.small**: 2 vCPU, **2GB RAM**, **x86_64** (버스트형) |
| 스토리지 | **gp3 40GiB** (제약: 50GiB 이하) |
| OS | **Ubuntu Server 24.04 LTS** (x86) |
| 네트워크 | 서울 리전(ap-northeast-2), Default VPC, 퍼블릭 서브넷 |
| 도메인 | `sys-aims.duckdns.org` (Let's Encrypt HTTPS) |
| 퍼블릭 IP | **탄력적 IP 없음**(IAM 권한 제약) → 중지·시작 때 바뀌는 IP를 **DuckDNS 컨테이너가 5분마다 갱신** |

로컬(맥, arm64)에서 구현과 검증을 마친 뒤 EC2로 옮길 때 해야 할 작업입니다.
**⛔ 차단**으로 표시된 항목은 하지 않으면 해당 기능이 동작하지 않습니다.

> **이전 계획(t4g, arm64, Amazon Linux 2023)에서 바뀐 점**: RAM 8GB → **2GB**, arm64 → **x86_64**, Amazon Linux → **Ubuntu**.
> 메모리 상한 재조정, **스왑 필수**, apt 기반 설치, 사용자 `ubuntu`를 아래에 반영했습니다.

---

## 0. 한눈에 보기

| # | 작업 | 구분 | 관련 |
|---|---|---|---|
| 1 | EC2 · 보안 그룹 준비 (탄력적 IP 없음) | ⛔ 차단 | 1장 |
| 2 | **스왑 2GB** | ⛔ **필수** (2GB RAM) | 2장 |
| 3 | Docker Engine · compose 플러그인 설치 (apt) | ⛔ 차단 | 3장 |
| 4 | `.env` prod 값 (**`DOCKER_GID`**, `APP_ENV`, `DOMAIN`, `REPORT_BASE_URL`) | ⛔ 차단 | A장 |
| 5 | `docker-compose.prod.yml`: Nginx(80/443) + certbot + duckdns, 내부 포트 비공개 | ⛔ 차단 (작성 완료) | 4장 |
| 6 | Nginx prod 설정: HTTPS, Zabbix Web 프록시, `/reports/` (Basic Auth) | ⛔ 차단 (작성 완료) | 5장 |
| 7 | certbot 최초 발급 + 자동 갱신 | ⛔ 차단 | 6장 |
| 8 | DuckDNS 자동 갱신 (탄력적 IP 대체) | ⛔ 차단 | 7장 |
| 9 | Zabbix 초기화: Admin 비밀번호 변경 → `zabbix_config.py import` | ⛔ 차단 | 8장 |
| 10 | 배포 중 오탐 방지 (maintenance 모드) | 권장 | 9장, troubleshooting #8 |
| 11 | 디스크 지표 수집 (호스트 `/` 읽기 전용 마운트) | 권장 | B장 |
| 12 | 검증: 측정 · 시나리오 · 보고서 · HTTPS · **메모리** | 필수 | C장 |
| 13 | 복구 확인 지연(#4) EC2에서 재측정 | 후속 | troubleshooting #4 |

---

## 메모리 예산 (2GB RAM + 스왑 2GB)

### 측정 방법: 유휴 상태가 아니라 "부하 시 peak"으로 정했다

2GB에 맞추려면 상한을 줄여야 하는데, **무엇을 기준으로 줄이느냐**가 문제였습니다.

| 기준 | 값 (전체 합계) | 문제 |
|---|---|---|
| `docker stats` 현재값 (유휴) | 약 377MB | 순간 사용량이 아니라 **그 순간의 값**일 뿐. 여기에 맞추면 부하 때 OOM |
| **cgroup `memory.peak`** (기동 이후 최대) | 컨테이너별 측정 | 채택. 실제로 얼마나 치솟았는지가 남아 있음 |

**부하를 준 뒤에 측정했습니다.** 유휴 상태의 peak만 보면 실제 위험을 놓칩니다.
1. 장애 시나리오: 의존 서비스 중단 → 자동 복구 시도 → AI 원인 분석
2. **웹 부하: 300 요청 동시 30** + API 100 요청 동시 30
3. 일일 보고서 생성 (Zabbix 24시간 집계 + AI)

그리고 `memory.events`의 `oom_kill` 카운터로 **실제로 죽지 않았는지**를 함께 확인했습니다(결과 0건).

### 유휴 측정만 했으면 놓쳤을 것: php-fpm 워커 50개

- `docker stats`로 본 zabbix-web은 **88.8MB**였습니다. 상한 160MB면 넉넉해 보입니다.
- 그런데 이미지 기본 설정을 열어 보니 **php-fpm 최대 워커가 50개**(`PHP_FPM_PM_MAX_CHILDREN=50`)였고, **워커 하나가 약 35MB**를 썼습니다.
- 즉 트래픽이 몰리면 이론상 **1.7GB**까지 늘어날 수 있습니다. 2GB 인스턴스에서는 이것 하나로 전체가 멈춥니다.
- 유휴 상태에서는 워커가 12개뿐이라 이 위험이 드러나지 않습니다. **구성값을 직접 확인하고 부하를 걸어 보고 나서야** 보였습니다.

**조치**: 최대 워커 50 → **6**, `pm.max_requests=500`(워커 재생성으로 메모리 누적 방지).

| | 유휴 | 부하 시 peak | 워커 수 |
|---|---|---|---|
| 조정 전 | 88.8MB | **109MB** | 12개 (최대 50 가능) |
| 조정 후 | 54MB | **65MB** | 3개 (최대 6) |

- 검증: 워커 6개로도 **300/300 요청, API 100/100이 모두 200**이었고 4초에 끝났습니다. 이 규모에서 워커 50개는 필요하지 않았습니다.
- **교훈**: 제약 환경에 맞출 때는 "지금 얼마나 쓰는가"가 아니라 **"최악의 경우 얼마나 쓸 수 있는가"**를 봐야 합니다. 그 값은 실측 peak과 **설정값의 상한**(워커 수 × 워커당 메모리) 양쪽에서 나옵니다.

### 서비스별 상한 (조정 결과)

| 서비스 | 이전 상한 | 실측 peak (조정 전) | **새 상한** | 실측 peak (조정 후, 부하 포함) | 억제 수단 |
|---|---:|---:|---:|---:|---|
| postgres | 512M | 369M (파일 캐시 약 92M 포함) | **320M** | 119M (37%) | `shared_buffers` 128→**64MB**, `max_connections` 100→**50**, `work_mem` 2MB, `maintenance_work_mem` 32MB |
| zabbix-server | 384M | 55M | **128M** | 54M (42%) | 프로세스 수 축소(기존) |
| zabbix-web | 256M | 109M (php-fpm 12개) | **160M** | 65M (41%) | php-fpm 최대 워커 50→**6** |
| zabbix-agent | 128M | 31M | **64M** | 34M (53%) | |
| reporter | 128M | 43M | **96M** | 43M (44%) | |
| rca | 64M | 44M | 64M | 44M (69%) | peak이 이미 상한에 가까워 유지 |
| healer | 64M | 31M | **48M** | 31M (65%) | |
| socket-proxy | 32M | 12M | 32M | 12M | |
| pitwall_web / api | 64M / 32M | 18M / 18M | **32M / 32M** | 16M / 18M | |
| **합계** | **1.63G** | | **976M** | 실사용 347M | |
| Nginx + certbot + duckdns | | | +112M (32/64/16) | | prod 추가분 → **총 약 1.09G** |

- **OOM kill 0건**(`memory.events`). 웹 요청 300/300, API 100/100이 모두 200이었고, 장애 시나리오와 보고서도 정상 동작했습니다(2026-09-19, 로컬).
- **OS 몫**: 2GB에서 상한 합계 1.09G를 빼면 약 0.9GB가 남습니다. Ubuntu, Docker Engine, containerd, 페이지 캐시가 이 안에서 씁니다.
- **스왑 2GB는 필수입니다.** 상한 합계가 RAM에 들어가더라도 다음 상황에서 순간 초과가 날 수 있습니다.
  - 이미지 빌드(`docker compose build`)
  - postgres의 housekeeper 대량 삭제
  - apt 업그레이드

### PostgreSQL: 데이터가 쌓여도 메모리가 늘지 않게
| 설정 | 값 | 효과 |
|---|---|---|
| `shared_buffers` | 64MB | 공유 메모리의 **상한**. 데이터가 커져도 이 이상 늘지 않습니다(조정 전 실측 83MB 사용 중이었음) |
| `max_connections` | 50 | 연결당 메모리의 상한. 실측 연결 31~33개(Zabbix 27 + web) |
| `work_mem` | 2MB | 정렬·해시 1건당 메모리. 쿼리가 몰려도 순간 사용량이 작음 |
| `maintenance_work_mem` | 32MB | housekeeper 삭제, VACUUM 때 사용량 |
| `effective_cache_size` | 192MB | 메모리 할당이 아니라 플래너 힌트 |

- 데이터가 더 쌓이면 **메모리가 아니라 디스크 I/O**가 늘어납니다. 파일 캐시는 cgroup 상한에 닿으면 회수됩니다.

### 데이터 보관 기간: 이력 7일 / 추세 90일 (전역 override)
- `Linux by Zabbix agent` 템플릿 아이템은 기본값이 **이력 31일 / 추세 365일**이고, 템플릿 상속 아이템이라 개별 수정이 안 됩니다.
- 그래서 **housekeeper 전역 override**로 모든 아이템을 **7일 / 90일**로 맞췄습니다. `scripts/zabbix_config.py`가 `housekeeping.update`로 적용하고, `automation.json`의 `housekeeping` 항목으로 export·import됩니다.
- **근거**
  - 일일 보고서는 48시간(이번 + 직전 기간)만 씁니다.
  - 장애 사후 분석은 1주면 충분합니다.
  - 추세(시간 단위 요약)는 월간 비교용으로 90일을 둡니다.
- **데이터량 추정**
  - 감시 아이템 236개 기준, 수집률 100%일 때 **하루 약 22.5만 값**입니다.
  - 실측 행당 약 100바이트(인덱스 포함) → 이력 7일 약 160MB, 추세 90일 약 45MB입니다.
  - **DB는 약 300MB 이하**로 유지됩니다. 40GiB 디스크에 충분하고, `shared_buffers` 상한 덕분에 메모리 영향도 없습니다.

---

## 1. 인프라 준비 (AWS 콘솔)

**인스턴스**
| 항목 | 설정 |
|---|---|
| AMI | **Ubuntu Server 24.04 LTS (HVM), SSD Volume Type**, 아키텍처 **64비트(x86)** |
| 인스턴스 유형 | **t3.small** |
| 키 페어 | ED25519, `.pem`. 저장소 밖(`~/.ssh/`)에 두고 `chmod 400` |
| 네트워크 | Default VPC, 퍼블릭 서브넷, 퍼블릭 IP 자동 할당 |
| 스토리지 | **gp3 40GiB**, 암호화 권장 |
| 크레딧 사양 | t3는 기본이 **Unlimited**입니다. 기준 성능(vCPU당 20%)을 넘겨 쓰면 추가 요금이 붙습니다. 비용을 고정하려면 **Standard**로 바꾸세요(로컬 실측 CPU 평균 1.7%, p95 3.3%) |
| 사용자 데이터 | 비움 |

**보안 그룹 (인바운드)**
| 포트 | 소스 | 용도 |
|---|---|---|
| 22 | **내 IP만** | SSH |
| 80 | 0.0.0.0/0 | ACME 챌린지, HTTPS 리다이렉트 |
| 443 | 0.0.0.0/0 | HTTPS |

- 아웃바운드는 기본값(전체 허용)으로 둡니다. OpenAI, Slack, DuckDNS, Let's Encrypt, Docker Hub, PyPI에 접속해야 합니다.
- 내부 포트(8080, 10051 등)는 열지 않습니다.

**퍼블릭 IP (탄력적 IP 없음)**
- 부여된 IAM 권한으로는 탄력적 IP를 할당할 수 없어 **자동 할당 퍼블릭 IP**를 씁니다.
- 인스턴스를 **중지·시작하면 IP가 바뀝니다**(재부팅은 유지). 그래서 도메인이 IP를 따라오도록 **`duckdns` 컨테이너가 5분마다 갱신**합니다(7장).
- 인증서는 도메인에 발급되므로 IP가 바뀌어도 그대로 유효합니다. DNS만 따라가면 됩니다.
- **보안 그룹의 SSH(22) 소스(내 IP)** 는 내 쪽 IP가 바뀔 때 직접 고쳐야 합니다.

**태그 정책(학원 계정)**: `username`/`group` 태그는 시스템이 자동으로 붙입니다. 사용자가 직접 붙이면 거부되고, 새 보안 그룹은 태그가 자동으로 붙은 뒤에야 규칙을 추가할 수 있습니다.

**접속**: 기본 사용자가 **`ubuntu`**입니다(Amazon Linux의 `ec2-user`가 아님).
```bash
ssh -i ~/.ssh/sys-aims-09.pem ubuntu@<퍼블릭 IP>      # 최초 (DNS 갱신 전)
ssh -i ~/.ssh/sys-aims-09.pem ubuntu@sys-aims.duckdns.org   # DuckDNS 갱신 후
```

## 2. 스왑 2GB (필수)
```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
# 스왑은 비상용: 평소에는 RAM 우선
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf && sudo sysctl --system
free -h    # Swap: 2.0Gi 확인
```

## 3. Docker Engine + compose 플러그인 (Ubuntu, apt)
Docker 공식 저장소를 씁니다.
- **snap의 docker나 Ubuntu 기본 저장소의 `docker.io`는 쓰지 않습니다.** 버전이 늦고 compose 플러그인 구성이 다릅니다.
- **Amazon Linux와 달리 compose 플러그인이 apt 패키지로 함께 설치**되므로 바이너리를 따로 받을 필요가 없습니다.
```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl git python3
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker ubuntu      # 재로그인 후 적용
docker version && docker compose version
```
- `python3`는 호스트에서 `scripts/*.py`(Zabbix 설정, 측정, 장애 시나리오)를 실행하는 데 씁니다. Ubuntu 24.04 기본은 3.12이고, 스크립트는 표준 라이브러리만 씁니다.

### Ubuntu에서만 다른 점 (Amazon Linux 계획 대비)
| 항목 | 내용 | 조치 |
|---|---|---|
| 기본 사용자 | `ubuntu` | SSH·`usermod`·경로(`/home/ubuntu`)를 그에 맞춤 |
| 패키지 | apt, Docker 공식 저장소. compose 플러그인이 패키지에 포함 | 위 3장 |
| docker 그룹 GID | `docker-ce` 패키지가 설치할 때 **시스템 그룹으로 생성**합니다. 값은 기존 시스템 그룹에 따라 달라지므로 **추측하지 말고 확인**합니다 | A장 |
| **unattended-upgrades** | 보안 업데이트가 자동 설치되며, `docker-ce`가 업그레이드되면 **Docker 데몬이 재시작**됩니다 | 아래 설명 |
| UFW | EC2 Ubuntu에서는 기본 비활성. **Docker가 공개한 포트는 UFW 규칙을 우회**하므로 방화벽은 보안 그룹으로 관리합니다 | UFW를 켜지 않음 |
| cgroup | 24.04 기본 cgroup v2 → compose의 `deploy.resources.limits.memory`가 그대로 적용됨 | 확인: `docker info \| grep -i cgroup` |
| 시간대 | 컨테이너는 `TZ=Asia/Seoul`로 동작하므로 호스트 시간대는 무관 | (선택) `sudo timedatectl set-timezone Asia/Seoul` |

**unattended-upgrades와 `pitwall_web` (`restart: "no"`)**
- Docker 데몬이 재시작되거나 호스트가 재부팅되면, 다른 서비스는 `unless-stopped`라 다시 뜹니다.
- `pitwall_web`은 **의도적으로 다시 뜨지 않습니다.**
- 이때 Zabbix가 장애로 감지하고 healer가 재기동하므로 **Self-Healing이 처리**합니다(RCA와 Slack 알림도 발생).
- 알림을 피하려면 Docker 패키지를 업그레이드 대상에서 빼고(`sudo apt-mark hold docker-ce docker-ce-cli containerd.io`), 수동 점검 시간에 올리는 방법을 권장합니다.

## A. `.env` prod 값 — ⚠️ 맥과 다른 것

| 키 | 로컬(맥) | **EC2 (Ubuntu)** | 틀리면 |
|---|---|---|---|
| `DOCKER_GID` | `0` | **`getent group docker \| cut -d: -f3`** | socket-proxy가 기동 직후 종료 → **Self-Healing 전체 불능** |
| `APP_ENV` | `local` | `prod` | — (기록용. 읽는 코드 없음) |
| `DOMAIN` | `localhost` | `sys-aims.duckdns.org` | — (기록용. 읽는 코드 없음) |
| `REPORT_BASE_URL` | (비움) | `https://sys-aims.duckdns.org/reports` | Slack에 보고서 링크 대신 파일명만 표시 |
| `ZABBIX_API_URL` | `http://localhost:8080/...` | `http://127.0.0.1:8080/api_jsonrpc.php` (루프백 바인딩, 4장) | 설정 스크립트 접속 실패 |

**도메인을 바꿀 때 실제로 고쳐야 하는 곳**: `nginx/conf.d/prod/10-https.conf.off`(`server_name` 1곳 + 인증서 경로 2곳), `.env`의 `REPORT_BASE_URL`, `DUCKDNS_DOMAIN`. `DOMAIN`과 `APP_ENV`는 어떤 코드도 읽지 않으므로 바꿔도 동작에 영향이 없습니다.

### `DOCKER_GID`: 맥에서는 드러나지 않는 차이
| | 소켓 소유자 | 권한 | `.env`의 `DOCKER_GID` |
|---|---|---|---|
| 맥 (Docker Desktop VM) | `root:root` | 660 | `0` |
| **EC2 (Ubuntu, docker-ce)** | **`root:docker`** | 660 | **docker 그룹의 GID (설치 후 확인)** |

```bash
getent group docker | cut -d: -f3         # 이 값을 .env DOCKER_GID 에
stat -c '%U:%G %a' /var/run/docker.sock   # root:docker 660 확인
```
- `wollomatic/socket-proxy`는 **비root 사용자(65534)**로 실행됩니다. 그래서 `group_add`로 소켓의 소유 그룹을 추가해야 소켓을 읽을 수 있습니다.

**GID가 틀렸을 때의 증상** (로컬에서 재현, 2026-09-18)
```
level=ERROR msg="socket not available" error="dial unix /var/run/docker.sock: connect: permission denied"
→ 컨테이너 종료 (exit code 2)
```
- healer는 socket-proxy가 healthy가 되어야 시작하므로 healer도 뜨지 않습니다.
- **모니터링과 장애 감지는 정상으로 보여 알아차리기 어렵습니다.** 배포 직후 `docker compose ... ps socket-proxy healer`로 둘 다 healthy인지 확인하세요.

## x86(amd64) 전환 영향 — 확인 완료 (2026-09-19)
| 항목 | 결과 |
|---|---|
| 사용 이미지 7종 | **모두 amd64 + arm64 멀티 아키텍처** (Docker Hub manifest 확인): postgres, zabbix-server/web/agent2 7.0.30, nginx, wollomatic/socket-proxy 1.13.1, python 3.14.7-alpine |
| 자체 이미지 (automation / reporter) | 로컬에서 `docker buildx build --platform linux/amd64`로 빌드 성공(에뮬레이션). 해시 고정 `pip install` 통과, `platform.machine()=x86_64`에서 markdown HTML 변환 정상 |
| `markdown` 패키지 | `py3-none-any` 휠(순수 Python)이라 아키텍처와 무관. 해시 2개도 그대로 유효 |
| 빌드 위치 | EC2에서 `docker compose build`로 직접 빌드하므로 자동으로 amd64. 로컬(arm64)과 같은 compose 파일을 씀 |
| `DOCKER_GID` | 아키텍처가 아니라 **OS/패키지**에 따라 달라짐 (A장) |
| Docker API 버전 | healer는 `/v1.44`로 고정. docker-ce 최신은 이를 지원(로컬 29.x의 지원 범위 1.40~1.56) |

## 4. `docker-compose.prod.yml` (작성 완료)
base와 함께 씁니다. 두 파일을 매번 나열하므로 셸 별칭을 하나 두면 편합니다.
```bash
alias dc='docker compose -f docker-compose.yml -f docker-compose.prod.yml'
```

| 서비스 | 추가 내용 | 상한 |
|---|---|---|
| `nginx` | 유일한 공개 진입점(80/443). `reports_data:/srv/reports:ro`, `nginx/snippets`, `nginx/conf.d/prod`, `nginx/auth`(htpasswd), 인증서 볼륨을 마운트. 6시간마다 스스로 `nginx -s reload` (인증서 갱신 반영) | 32M |
| `certbot` | webroot 방식, 12시간마다 `certbot renew` | 64M |
| `duckdns` | 5분마다 퍼블릭 IP 갱신 (탄력적 IP 대체, 7장) | 16M |
| `zabbix-web` | `127.0.0.1:8080`에만 바인딩. 호스트에서 `scripts/zabbix_config.py`가 API를 부르는 통로이며 외부에서는 닿지 않습니다 | (base와 동일) |

- `pitwall_web`/`pitwall_api`는 호스트에 포트를 열지 않습니다. 감시는 컨테이너 망 안에서 이뤄집니다.
- `zbx_front` 망을 추가해 nginx ↔ zabbix-web을 잇습니다. **`internal: true`로 두지 않습니다** — internal 망은 외부에서 들어오는 공개 포트(80/443) 트래픽까지 막습니다.

## 5. Nginx prod 설정 (작성 완료)

| 파일 | 역할 |
|---|---|
| `nginx/conf.d/prod/00-acme.conf` | `:80` — ACME 챌린지 + HTTPS 301 리다이렉트, 헬스체크 경로(`/nginx-health`). **인증서가 없어도 뜹니다** |
| `nginx/conf.d/prod/10-https.conf.off` | `:443` — 인증서, HSTS, Zabbix Web 프록시, `include snippets/reports.conf`. **발급 후 `.off`를 떼어 활성화** |

- 부트스트랩 순서를 지키려고 HTTPS 설정을 `.off`로 둡니다. 인증서 파일이 없는 상태로 `ssl_certificate`를 읽으면 nginx가 아예 기동하지 않기 때문입니다.
- Zabbix Web 프록시는 `upstream` 대신 **변수 `proxy_pass` + Docker 내장 DNS(127.0.0.11)** 를 씁니다. 이름을 기동 시 한 번만 해석하면 재배포로 컨테이너 IP가 바뀐 뒤 502가 계속 납니다.
- 공개 범위를 좁히려면 `10-https.conf.off`의 `allow`/`deny` 주석을 풉니다(기본값: HTTPS + Zabbix 로그인으로만 보호).

**Basic Auth 파일 생성** (EC2, gitignore 대상)
```bash
mkdir -p nginx/auth
printf 'professor:%s\n' "$(openssl passwd -apr1)" > nginx/auth/reports.htpasswd   # 비밀번호 입력
```

## 6. certbot
**최초 발급 (1회, 수동)** — DuckDNS가 현재 IP를 가리키고 80이 열린 뒤에 실행합니다.
```bash
dc run --rm --entrypoint certbot certbot certonly \
  --webroot -w /var/www/certbot -d sys-aims.duckdns.org \
  --email <메일주소> --agree-tos --no-eff-email
cp nginx/conf.d/prod/10-https.conf.off nginx/conf.d/prod/10-https.conf
dc exec nginx nginx -t && dc exec nginx nginx -s reload
```
- `--entrypoint certbot`이 필요합니다. 서비스의 기본 entrypoint가 갱신 루프(`sh -c`)로 덮여 있기 때문입니다.
- 실패를 반복하면 Let's Encrypt 요청 한도에 걸립니다. 발급 전에 `curl http://sys-aims.duckdns.org/.well-known/acme-challenge/ping`으로 경로가 열렸는지 먼저 확인하세요.

**자동 갱신**: `certbot` 컨테이너가 12시간마다 `renew`를 시도하고(만료 30일 전부터 실제 갱신), `nginx`가 6시간마다 reload해 새 인증서를 집습니다.

## 7. DuckDNS (탄력적 IP 대체)
`duckdns` 컨테이너가 5분마다 갱신합니다. `ip=`를 **비워서** 보내면 DuckDNS가 요청의 출발지 IP를 쓰므로, 바뀐 퍼블릭 IP를 직접 알아낼 필요가 없습니다.

```bash
dc logs --tail 5 duckdns          # "duckdns sys-aims: OK"
dig +short sys-aims.duckdns.org   # 현재 퍼블릭 IP와 일치
```
- 인스턴스를 중지·시작하면 최대 5분 + DNS TTL(60초) 안에 도메인이 새 IP를 가리킵니다.
- 토큰은 `.env`에서만 읽고 로그에는 응답(OK/KO)만 남깁니다. 셸에서 직접 호출할 때는 히스토리에 토큰이 남지 않도록 주의하세요.
- 주기는 `.env`의 `DUCKDNS_INTERVAL_SECONDS`(기본 300)로 조절합니다.

## 8. Zabbix 초기화
1. `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build --wait`
   - t3.small에서 첫 빌드는 몇 분 걸릴 수 있습니다. 스왑이 켜져 있는지 먼저 확인하세요.
2. 웹에서 Admin 비밀번호를 변경하고 `.env`의 `ZABBIX_API_PASSWORD`에 반영합니다.
3. `python3 scripts/zabbix_config.py import`를 실행합니다.
   - 템플릿, 호스트, 미디어 타입, Action, Secret 매크로, 읽기 전용 보고서 계정, **보관 기간 override**가 한 번에 적용됩니다.
   - 새 Zabbix에서는 템플릿 연결이 15초를 넘길 수 있어 import에만 120초 타임아웃을 줍니다(로컬 실측 18.7초, [troubleshooting #9](troubleshooting.md)).
4. 설치 직후 "Zabbix agent is not available" 알람은 import가 자동으로 정리합니다([troubleshooting #1](troubleshooting.md)).

## 9. 배포 중 오탐 방지: maintenance 모드 (권장)
- **문제**: `compose up`으로 컨테이너를 재생성하는 동안 Zabbix가 장애로 감지합니다. 그러면 불필요한 재기동, RCA, Slack이 발생합니다([troubleshooting #8](troubleshooting.md)). 이때 남은 종료 로그가 다음 RCA 입력을 오염시킨 사례도 있습니다(#6).
- **방안**: `scripts/deploy.sh`
  1. Admin API `maintenance.create`(호스트 그룹 `Sys-AIMS`, 10분)
  2. `compose up -d --wait`
  3. `maintenance.delete`
  - Action의 "maintenance 중 일시 중지"가 기본으로 켜져 있습니다.
- 참고: 2026-09-19 메모리 상한 변경 때의 재생성은 빨라서(연속 2회 실패 전에 복구) 오탐이 발생하지 않았습니다. 재생성이 느리면 발생합니다.

## B. 디스크 지표 (권장)
- **현재**: 컨테이너 agent의 파일시스템 탐색에서 호스트 `/`가 잡히지 않아 보고서에 "미수집(사유)"로 표시됩니다.
- **방안**
  1. zabbix-agent에 `/:/hostfs:ro`를 마운트합니다.
  2. `zabbix-agent` 호스트에 `vfs.fs.size[/hostfs,pused]` 아이템을 추가합니다(`zabbix_config.py`).
  3. 보고서 수집 키(`collect.py`의 `disk`)를 교체합니다.
- **확인**: `docker exec zabbix-agent zabbix_agent2 -t 'vfs.fs.size[/hostfs,pused]'`

## D. 다음 단계(감시 대상 VM 분리)를 막지 않기 위해 지금 해 둔 것

감시 대상을 같은 서버의 컨테이너가 아니라 **별도 EC2 2대(VM-A = 계열사 A, VM-B = 계열사 B)** 로 옮기는 변경이 예정되어 있습니다. 이번 배포에서는 구현하지 않지만, 나중에 되돌릴 일이 없도록 아래만 맞춰 두었습니다.

| 예정 변경 | 이번 배포에서 해 둔 것 | 그때 추가로 할 일 |
|---|---|---|
| VM-A/B의 Zabbix Agent가 이 서버로 접속 | `docker-compose.prod.yml`에 `zabbix-server`의 `10051` 공개 블록을 **주석으로** 남겨 둠 | 주석 해제 + 보안 그룹에 **VPC 내부 CIDR로만** 10051 허용 (0.0.0.0/0 금지) |
| 계열사별 호스트 그룹·권한·알림 | 호스트·그룹·Action을 전부 `scripts/zabbix_config.py`(API)로 관리. 웹 UI 수동 설정이 없어 그룹을 늘려도 코드 한 곳만 바뀜 | `HOST_GROUP` 상수를 계열사별 그룹으로 확장, 장애에 `company` 태그 추가 |
| 계열사별 AI 정책(사용 여부·한도·마스킹) | 한도와 키를 `.env`로만 주입(`RCA_MAX_PER_DAY`, `REPORT_MAX_PER_DAY`). 코드에 상수로 박아두지 않음 | 계열사별 정책 파일/테이블로 분리 |
| 외부 AI 전송 데이터 보호 | `automation/rca/compress.py`의 `redact()`가 단일 경로에서 마스킹을 담당. 전송 기록은 `automation_data` 볼륨(`rca.jsonl`)에 영속 | IP·이메일·전화·주민번호·카드번호·내부 서버명 패턴 추가, 전송 본문 감사 기록 |

- 공개 진입점은 nginx 하나이고 도메인도 하나입니다. 계열사별 화면을 나눠야 하면 서브도메인 대신 **Zabbix 권한(사용자 그룹)으로 분리**하는 편이 인증서를 늘리지 않아 간단합니다.

## C. 이전 후 검증

| 항목 | 명령 | 기대 |
|---|---|---|
| 아키텍처 / OS | `uname -m; lsb_release -ds` | `x86_64`, Ubuntu 24.04 |
| 스왑 | `free -h` | Swap 2.0Gi |
| 전체 상태 | `docker compose ... ps` | 10개 서비스 + Nginx/certbot healthy |
| socket-proxy 권한 | `docker logs socket-proxy` | `permission denied` 없음 |
| **메모리** | 각 컨테이너 `cat /sys/fs/cgroup/memory.peak`, `memory.events`의 `oom_kill` | 상한 이하, **oom_kill 0** |
| 호스트 여유 | `free -h`, `vmstat 5 3` | 스왑 사용이 지속 증가하지 않음 |
| HTTPS | `curl -I https://sys-aims.duckdns.org` | 유효한 인증서 |
| DNS 갱신 | `dig +short sys-aims.duckdns.org` | 현재 퍼블릭 IP와 일치 |
| 보고서 인증 | `/reports/` 인증 없이 / 있이 | 401 / 200 |
| 자동 복구 측정 | `python3 scripts/measure_detection.py --mode heal --trials 5` | 로컬(평균 24.8초)과 비교 |
| 장애 시나리오 | `python3 scripts/chaos.py dependency --watch` → `restore` | 분류 `dependency` |
| 보고서 | `docker exec reporter python -m daily_report.run --hours 1` | 수집률 ~100%, Slack에 링크 |
| 복구 확인 지연 (#4) | 위 측정의 Zabbix 기준 다운타임 | 로컬 평균 45.8초와 비교 |
