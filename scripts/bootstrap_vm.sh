#!/usr/bin/env bash
# =============================================================
# 감시 대상 VM 부트스트랩 (Ubuntu 24.04 / t3.small)
#
#   ssh sys-aims-vm-a
#   git clone https://github.com/Hoon-KR/Sys-AIMS.git sys-aims && cd sys-aims
#   bash scripts/bootstrap_vm.sh
#
# 하는 일: apt 전체 업데이트 → 스왑 2GB → Docker(공식 저장소) → Docker 고정 → docker 그룹
# 재부팅은 하지 않는다. 필요하면 마지막 요약에서 알려준다 (실행 시점은 사람이 정한다).
# 여러 번 실행해도 안전하다 (각 단계가 이미 끝났으면 건너뛴다).
#
# mon 서버에서 수동으로 진행한 절차(docs/aws-migration.md 2~3장)와 같은 내용이며,
# VM 2대에 같은 상태를 재현하기 위해 스크립트로 묶었다.
# =============================================================
set -euo pipefail

APT_OPTS=(-o DPkg::Lock::Timeout=300)
DPKG_OPTS=(-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)
SWAP_FILE=/swapfile
SWAP_SIZE=2G
HOLD_PACKAGES=(docker-ce docker-ce-cli containerd.io)

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
skip() { printf '    (건너뜀) %s\n' "$1"; }

if [ "$(id -u)" -eq 0 ]; then
    echo "오류: root 로 실행하지 마세요. ubuntu 사용자로 실행합니다 (docker 그룹 대상이 달라집니다)." >&2
    exit 1
fi
TARGET_USER="$(id -un)"

if ! grep -q '^VERSION_CODENAME=noble' /etc/os-release 2>/dev/null; then
    echo "오류: Ubuntu 24.04(noble)에서만 검증했습니다. 현재: $(. /etc/os-release && echo "$PRETTY_NAME")" >&2
    exit 1
fi

# ---------------------------------------------------------------- 1. apt 전체 업데이트
step "1/5 apt 전체 업데이트"
sudo "${APT_OPTS[@]}" apt-get update
if [ -n "$(apt list --upgradable 2>/dev/null | tail -n +2)" ]; then
    # 제거되는 패키지가 있으면 멈춘다 (사람이 확인해야 할 상황)
    removals=$(sudo apt-get -s full-upgrade | grep '^Remv' || true)
    if [ -n "$removals" ]; then
        echo "중단: full-upgrade 가 패키지를 제거하려고 합니다. 직접 확인하세요." >&2
        echo "$removals" >&2
        exit 1
    fi
    sudo DEBIAN_FRONTEND=noninteractive "${APT_OPTS[@]}" "${DPKG_OPTS[@]}" apt-get -y full-upgrade
else
    skip "업그레이드 대상 없음"
fi
sudo "${APT_OPTS[@]}" apt-get install -y ca-certificates curl git python3

# ---------------------------------------------------------------- 2. 스왑 2GB
step "2/5 스왑 ${SWAP_SIZE}"
if swapon --show=NAME --noheadings | grep -qx "$SWAP_FILE"; then
    skip "$SWAP_FILE 이미 활성"
else
    [ -f "$SWAP_FILE" ] || sudo fallocate -l "$SWAP_SIZE" "$SWAP_FILE"
    sudo chmod 600 "$SWAP_FILE"      # mkswap 이 권한을 요구한다
    sudo mkswap "$SWAP_FILE" >/dev/null
    sudo swapon "$SWAP_FILE"
fi
if grep -q "^${SWAP_FILE}[[:space:]]" /etc/fstab; then
    skip "fstab 항목 존재"
else
    echo "$SWAP_FILE none swap sw 0 0" | sudo tee -a /etc/fstab >/dev/null
fi
# 스왑은 비상용: 평소에는 RAM 우선
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf >/dev/null
sudo sysctl --system >/dev/null

# ---------------------------------------------------------------- 3. Docker Engine
step "3/5 Docker Engine + compose 플러그인"
if command -v docker >/dev/null 2>&1; then
    skip "docker 이미 설치됨 ($(docker --version))"
else
    sudo install -m 0755 -d /etc/apt/keyrings
    sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    sudo chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable" \
        | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
    sudo "${APT_OPTS[@]}" apt-get update
    sudo "${APT_OPTS[@]}" apt-get install -y \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

# ---------------------------------------------------------------- 4. Docker 고정
# unattended-upgrades 나 실수로 돌린 apt upgrade 가 데몬을 재시작하지 못하게 한다.
# 데몬이 재시작되면 restart:"no" 인 pitwall_web 이 올라오지 않아 오탐이 난다.
# buildx/compose 플러그인은 데몬을 재시작하지 않으므로 고정하지 않는다.
step "4/5 Docker 패키지 고정 (apt-mark hold)"
sudo apt-mark hold "${HOLD_PACKAGES[@]}"

# ---------------------------------------------------------------- 5. docker 그룹
step "5/5 ${TARGET_USER} 를 docker 그룹에 추가"
if id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx docker; then
    skip "이미 docker 그룹"
else
    sudo usermod -aG docker "$TARGET_USER"
    echo "    ⚠️ 재로그인해야 적용됩니다 (exit 후 다시 ssh)"
fi

# ---------------------------------------------------------------- 요약
step "요약"
printf '%-18s %s\n' "호스트명"    "$(hostname)"
printf '%-18s %s\n' "사설 IP"     "$(hostname -I | awk '{print $1}')"
printf '%-18s %s\n' "커널"        "$(uname -r)"
printf '%-18s %s\n' "스왑"        "$(swapon --show=SIZE,USED --noheadings | tr -s ' ' | paste -sd' ' -)"
printf '%-18s %s\n' "디스크 여유" "$(df -h --output=avail / | tail -1 | tr -d ' ')"
printf '%-18s %s\n' "docker"      "$(docker --version 2>/dev/null || echo '(재로그인 필요)')"
printf '%-18s %s\n' "compose"     "$(docker compose version --short 2>/dev/null || echo '(재로그인 필요)')"
printf '%-18s %s\n' "DOCKER_GID"  "$(getent group docker | cut -d: -f3)"
printf '%-18s %s\n' "hold"        "$(apt-mark showhold | paste -sd' ' -)"

if [ -f /var/run/reboot-required ]; then
    printf '\n\033[1m재부팅이 필요합니다\033[0m (커널/libc 갱신). 다음 단계 전에:\n  sudo reboot\n'
    printf '요구 패키지: %s\n' "$(paste -sd' ' - < /var/run/reboot-required.pkgs 2>/dev/null)"
else
    printf '\n재부팅 필요 없음.\n'
fi
