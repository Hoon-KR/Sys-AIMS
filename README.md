# Sys-AIMS

> **Sys**tem **AI** **M**onitoring & **S**elf-healing
> Zabbix 기반 인프라 모니터링에 생성형 AI를 결합한 자율복구형(Self-Healing) 시스템

---

## 1. 프로젝트 소개

Sys-AIMS는 컨테이너 장애를 **감지하고, 스스로 복구하고, 원인을 설명하는** 운영 자동화 시스템입니다.

| # | 기능 | 동작 |
|---|------|------|
| 1 | **Self-Healing** | Zabbix Trigger → Action이 장애 컨테이너를 자동으로 재기동 |
| 2 | **AI RCA** | 장애가 나면 `docker logs`를 OpenAI API로 분석해 근본 원인을 Slack으로 전송 |
| 3 | **AI 일일점검 보고서** | 매일 08:00 cron이 Zabbix API로 24시간 지표를 수집하고 AI가 마크다운 보고서 생성 |

---

## 2. 아키텍처

```
                  ┌──────────────────────── Docker Compose ────────────────────────┐
  사용자 ──HTTPS──▶│  Nginx ──▶ Zabbix Web ──▶ Zabbix Server ◀──▶ PostgreSQL        │
                  │  (리버스 프록시/SSL)             ▲    │                         │
                  │                                 │    │ Action                  │
                  │                        Zabbix Agent  ▼                         │
                  │                                 │   alertscripts ──▶ automation │
                  │                                 ▼        (재기동 / RCA)         │
                  │                          pitwall_web (모니터링 대상)            │
                  └───────────────────────────────────────────────────────────────┘
                                                              │
                                             OpenAI API ◀─────┤─────▶ Slack
                                                              │
                                   cron 08:00 ──▶ daily_report ──▶ reports/*.md
```

### 장애 대응 흐름
1. Zabbix Server가 HTTP 체크로 `pitwall_web`의 상태 이상을 감지합니다(Docker 소켓 불필요).
2. Zabbix Server의 Trigger가 발동하고 Action이 실행됩니다.
3. **Self-Healing**: Action(태그 `healing=auto`)이 webhook으로 healer를 호출하고, healer가 socket-proxy를 거쳐 대상 컨테이너를 재기동합니다. 반복 장애는 서킷 브레이커가 차단하고, 5분 동안 미해소 시 Slack으로 사람을 호출합니다([docs/self-healing.md](docs/self-healing.md)).
4. **RCA**: `alertscripts` 래퍼가 `automation/rca`를 호출합니다. 로그를 수집하고 OpenAI로 분석한 뒤 Slack으로 전송합니다.

### 설계 원칙
- **환경 분리**: local(HTTP)과 prod(HTTPS)의 차이는 `.env`, Compose override, Nginx conf 디렉터리 세 곳에만 둡니다.
- **설정 재현성**: Zabbix 템플릿과 Action을 `zabbix/templates/`에 export해서 커밋합니다. UI에서 수동으로 설정하지 않습니다.
- **얇은 래퍼**: `zabbix/alertscripts/`는 호출만 담당하고 실제 로직은 `automation/`에 둡니다.
- **시크릿 분리**: 모든 민감정보는 `.env`로만 주입하고 커밋하지 않습니다.

---

## 3. 기술 스택

| 영역 | 기술 |
|------|------|
| 인프라 | AWS EC2 t4g.medium (ARM64), Amazon Linux 2023 |
| 컨테이너 | Docker, Docker Compose |
| 모니터링 | Zabbix Server / Web / Agent |
| DB | PostgreSQL |
| 프록시 / TLS | Nginx, Let's Encrypt (certbot) |
| DNS | DuckDNS (`sys-aims.duckdns.org`) |
| 자동화 | Python 3, cron |
| AI | OpenAI API |
| 알림 | Slack Incoming Webhook |
| 개발 환경 | macOS (Apple Silicon M1, ARM64) |

> 개발 환경(M1)과 운영 환경(t4g)이 모두 **arm64**라서 동일한 이미지를 사용할 수 있습니다.

---

## 4. 폴더 구조

```
sys-aims/
├── .env.example              # 환경변수 템플릿 (키 이름과 설명만)
├── docker-compose.yml        # 공통 base (포트 노출 없음)
├── docker-compose.local.yml  # 로컬 override (HTTP, 127.0.0.1 바인딩)
├── nginx/
│   ├── snippets/             # 공통 proxy / SSL 설정 조각
│   └── conf.d/
│       ├── local/            # HTTP 전용 (개발)
│       └── prod/             # HTTPS + 리다이렉트 + ACME (운영)
├── zabbix/
│   ├── alertscripts/         # Action이 호출하는 래퍼 스크립트
│   ├── externalscripts/      # 외부 체크 스크립트
│   ├── agent/                # Agent UserParameter 설정
│   ├── mediatypes/           # Webhook 미디어 타입 스크립트 (healer, Slack)
│   └── templates/            # 템플릿 / 호스트 / 미디어 타입 / Action export
├── postgres/
│   └── init/                 # DB 초기화 SQL
├── services/
│   └── pitwall_web/          # 모니터링 대상 샘플 서비스
├── automation/               # Python 자동화 스크립트
│   ├── common/               # 설정 로더, Zabbix / OpenAI / Slack 클라이언트
│   ├── healing/              # healer (Self-Healing 재기동 서비스)
│   ├── rca/                  # 로그 분석 → Slack
│   ├── daily_report/         # 일일점검 보고서 생성
│   ├── prompts/              # LLM 프롬프트 템플릿
│   ├── cron/                 # crontab 정의
│   └── tests/
├── scripts/                  # 운영 스크립트 (인증서 발급, DuckDNS 갱신, 배포)
├── docs/                     # 아키텍처, 런북, AWS 이전 가이드
│   └── adr/                  # 의사결정 기록 (Architecture Decision Records)
├── reports/                  # (gitignore) 생성된 보고서
└── logs/                     # (gitignore) 런타임 로그
```

런타임에 생성되며 커밋하지 않는 경로:
- `zbx_env/`: Zabbix와 PostgreSQL 볼륨 데이터
- `certbot/`: Let's Encrypt 인증서와 ACME webroot

---

## 5. 실행 방법

> 🚧 로컬 실행만 작성되어 있습니다. 나머지 절차는 구현하면서 채웁니다.

### 5.1 사전 준비
```bash
cp .env.example .env
# .env에 실제 값 입력
```

### 5.2 로컬 실행 (HTTP)
```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --wait
docker compose -f docker-compose.yml -f docker-compose.local.yml ps
```

| 서비스 | 주소 |
|---|---|
| Zabbix Web | http://127.0.0.1:8080 (`ZABBIX_WEB_PORT`) |
| pitwall_web | http://127.0.0.1:8081 (`PITWALL_WEB_PORT`) |

> `pitwall_web`은 의도적으로 `restart: "no"`입니다. 복구 주체는 Zabbix Action이어야 하기 때문입니다.
> Docker 소켓 접근 방식은 [ADR-0001](docs/adr/0001-docker-socket-access.md)을 참고하세요.

### 5.3 프로덕션 배포 (HTTPS)
```bash
# TODO
# docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

### 5.4 Zabbix 설정 가져오기
```bash
# 스택 기동 후, 웹에서 Admin 비밀번호 변경 → .env의 ZABBIX_API_* 설정
python3 scripts/zabbix_config.py import
```
템플릿, 호스트, 트리거가 `zabbix/templates/*.yaml`에서 재현됩니다. 설계 근거, 측정 결과, 재현성 검증은 [docs/zabbix-monitoring.md](docs/zabbix-monitoring.md)를 참고하세요.
설치 직후 뜨는 "Zabbix agent is not available" 알람의 원인은 [docs/troubleshooting.md](docs/troubleshooting.md)에 정리되어 있습니다.

### 5.5 일일 보고서 cron 등록
> TODO: `automation/cron/` 적용 절차

---

## 6. 보안 유의사항
- `.env`, `*.pem`, 인증서, `zbx_env/`, `certbot/`, `reports/`는 `.gitignore`에 등록되어 있습니다.
- Zabbix API는 Admin 계정 대신 최소 권한 전용 계정을 사용하세요.
- 커밋 전에 `git status`로 민감 파일이 포함되지 않았는지 확인하세요.

---

## 7. 로드맵
- [x] 프로젝트 골격 및 문서
- [x] Docker Compose base + local
- [ ] Docker Compose prod (HTTPS / certbot)
- [ ] Nginx 설정 (local HTTP / prod HTTPS)
- [x] Zabbix 호스트, 템플릿, 트리거 (API + YAML export)
- [x] Self-Healing (socket-proxy + healer + Zabbix Action, 서킷 브레이커, 에스컬레이션)
- [ ] AI RCA → Slack
- [ ] AI 일일점검 보고서
- [ ] AWS EC2 이전 + Let's Encrypt
