#!/usr/bin/env bash
# =============================================================
# bootstrap_vm.sh 검증 — sudo/apt-get 를 스텁으로 바꿔 실제로 실행한다.
#
#   bash scripts/tests/test_bootstrap_vm.sh
#
# 맥에서도 돌아간다 (Ubuntu 전용 명령은 전부 스텁). 검증 목적:
#   1) 설치 경로가 끝까지 도달하는지
#   2) 두 번째 실행에서 모든 단계를 건너뛰는지 (멱등)
#   3) full-upgrade 가 패키지를 제거하려 할 때 중단하는지
#   4) apt 옵션이 apt-get **뒤에** 붙는지
#      (sudo 앞에 두면 sudo 가 -o 를 자기 옵션으로 먹는다 — 실제로 났던 버그)
# =============================================================
set -uo pipefail

SCRIPT="$(cd "$(dirname "$0")/../.." && pwd)/scripts/bootstrap_vm.sh"
STUB="$(mktemp -d)"
trap 'rm -rf "$STUB"' EXIT
fails=0

printf 'PRETTY_NAME="Ubuntu 24.04.4 LTS"\nVERSION_CODENAME=noble\n' > "$STUB/os-release"
printf '#!/bin/sh\necho "docker:x:993:"\n'                                  > "$STUB/getent"
printf '#!/bin/sh\n[ "$1" = "-I" ] && echo "172.31.9.142 " || echo vm-a\n'  > "$STUB/hostname"
printf '#!/bin/sh\necho Avail\necho "  17G"\n'                              > "$STUB/df"
printf '#!/bin/sh\necho amd64\n'                                           > "$STUB/dpkg"
printf '#!/bin/sh\n[ "$1" = showhold ] && { echo docker-ce; exit 0; }\necho "  [apt-mark] $*"\n' > "$STUB/apt-mark"

# 기본 sudo 스텁: 인자를 출력하고, tee 처럼 stdin 을 받는 경우는 끝까지 읽는다
# (읽지 않으면 echo 가 SIGPIPE 로 죽어 pipefail 때문에 조용히 종료된다)
write_sudo() {
    { printf '#!/bin/sh\ncase "$1" in tee) cat >/dev/null;; esac\n'
      [ "${1:-}" = "with-removals" ] && printf 'case "$*" in *"-s full-upgrade"*) echo "Remv obsolete-pkg [1.0]"; exit 0;; esac\n'
      printf 'echo "  [sudo] $*"\n'
    } > "$STUB/sudo"
}

run_case() {   # run_case <이름> <기대 종료코드>
    chmod +x "$STUB"/*
    env -i PATH="$STUB:/usr/bin:/bin:/usr/sbin:/sbin" OS_RELEASE="$STUB/os-release" \
        HOME="${HOME:-/tmp}" LANG=en_US.UTF-8 bash "$SCRIPT" > "$STUB/out" 2>&1
    local code=$? name="$1" want="$2"
    if [ "$code" -ne "$want" ]; then
        echo "✗ $name: 종료코드 $code (기대 $want)"; sed 's/^/    /' "$STUB/out"; fails=$((fails + 1))
    else
        echo "✓ $name (종료코드 $code)"
    fi
}

expect()     { grep -qF "$1" "$STUB/out" && echo "  ✓ 포함: $1"     || { echo "  ✗ 누락: $1";     fails=$((fails + 1)); }; }
expect_not() { grep -qF "$1" "$STUB/out" && { echo "  ✗ 존재해선 안 됨: $1"; fails=$((fails + 1)); } || echo "  ✓ 없음: $1"; }

# ---------------------------------------------------------------- 1. 설치 경로
echo "== 1. 첫 실행 (docker 미설치) =="
write_sudo
printf '#!/bin/sh\ncase "$*" in *SIZE*) echo "  2G 0B";; esac\n' > "$STUB/swapon"
printf '#!/bin/sh\necho "Listing..."\necho "apparmor/noble 4.0.1 amd64 [upgradable from: 4.0.0]"\n' > "$STUB/apt"
rm -f "$STUB/docker" "$STUB/id"
run_case "설치 경로" 0
expect     "apt-get -o DPkg::Lock::Timeout=300 update"
expect     "install -y docker-ce docker-ce-cli containerd.io"
expect     "apt-mark hold docker-ce docker-ce-cli containerd.io"
expect_not "[sudo] -o"            # 회귀 방지: 옵션이 sudo 앞으로 가면 안 된다
expect_not "[sudo] DEBIAN_FRONTEND=noninteractive -o"

# ---------------------------------------------------------------- 2. 멱등
echo "== 2. 두 번째 실행 (모두 완료된 상태) =="
printf '#!/bin/sh\ncase "$*" in *SIZE*) echo "  2G 0B";; *NAME*) echo /swapfile;; esac\n' > "$STUB/swapon"
printf '#!/bin/sh\ncase "$1" in -u) echo 1000;; -un) echo ubuntu;; -nG) echo "ubuntu docker";; esac\n' > "$STUB/id"
printf '#!/bin/sh\n[ "$1" = compose ] && { echo 5.6.0; exit 0; }\necho "Docker version 29.8.2"\n' > "$STUB/docker"
printf '#!/bin/sh\necho "Listing..."\n' > "$STUB/apt"
run_case "멱등 재실행" 0
expect "(건너뜀) 업그레이드 대상 없음"
expect "(건너뜀) /swapfile 이미 활성"
expect "(건너뜀) docker 이미 설치됨"
expect "(건너뜀) 이미 docker 그룹"

# ---------------------------------------------------------------- 3. 제거 감지
echo "== 3. full-upgrade 가 패키지를 제거하려는 경우 =="
write_sudo with-removals
printf '#!/bin/sh\necho "Listing..."\necho "libc6/noble 2.39-1 amd64 [upgradable from: 2.38]"\n' > "$STUB/apt"
run_case "Remv 감지 시 중단" 1
expect "중단: full-upgrade 가 패키지를 제거하려고 합니다"

echo
[ "$fails" -eq 0 ] && { echo "전체 통과"; exit 0; } || { echo "실패 $fails 건"; exit 1; }
