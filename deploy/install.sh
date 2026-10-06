#!/usr/bin/env bash
# 소담 서버 설치 (Ubuntu 24.04, root). 여러 번 돌려도 안전 (이미 된 건 건너뜀, 코드는 최신으로).
#
#   bash install.sh                                  1단계: 서버 준비만 (.env 없으면 봇은 안 켬)
#   bash install.sh --bundle /root/sodam-migrate-….tar.gz.enc   이사 꾸러미(.env + DB) 넣고 켜기 (비밀번호는 물어봄)
#   bash install.sh --env /root/.env [--db /root/sodam.db.gz]   파일을 따로 올린 경우
#
# 옵션: --branch 브랜치(기본 main) · --repo URL · --env-dealer 파일 · --replace-db(이미 있는 DB 바꾸기)
#       --auto-update(GitHub 새 커밋 2분마다 자동 배포) · --yes(확인 질문 생략)
# 비밀값(토큰·키)은 명령줄에 쓰지 않는다: 파일로 올리고 경로만 알려준다.
set -euo pipefail

REPO=https://github.com/moneyally/sodam_bot
BRANCH=main
APP=/opt/sodam
USR=sodam
ENV_SRC=""; ENV_DEALER_SRC=""; DB_SRC=""; BUNDLE=""; REPLACE_DB=0; AUTO_UPDATE=0; YES=0
while [ $# -gt 0 ]; do
    case $1 in
        --branch) BRANCH=$2; shift ;;
        --repo) REPO=$2; shift ;;
        --env) ENV_SRC=$2; shift ;;
        --env-dealer) ENV_DEALER_SRC=$2; shift ;;
        --db) DB_SRC=$2; shift ;;
        --bundle) BUNDLE=$2; shift ;;
        --replace-db) REPLACE_DB=1 ;;
        --auto-update) AUTO_UPDATE=1 ;;
        --yes|-y) YES=1 ;;
        -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
        *) echo "모르는 옵션: $1  (도움말: bash install.sh --help)" >&2; exit 2 ;;
    esac
    shift
done

step() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m!! %s\033[0m\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "root 로 실행하세요:  sudo bash install.sh"
for f in "$ENV_SRC" "$ENV_DEALER_SRC" "$DB_SRC" "$BUNDLE"; do
    [ -z "$f" ] || [ -f "$f" ] || die "파일이 없어요: $f"
done
grep -q 'VERSION_ID="24.04"' /etc/os-release 2>/dev/null || echo "※ Ubuntu 24.04 가 아니에요. 계속하지만 확인 안 된 환경입니다."
export DEBIAN_FRONTEND=noninteractive

step "시간대 Asia/Seoul"
timedatectl set-timezone Asia/Seoul

step "패키지 설치"
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip sqlite3 git ufw unattended-upgrades ca-certificates curl openssl
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "python 3.11 이상이 필요해요 ($(python3 -V))"

step "보안 자동 업데이트 (unattended-upgrades)"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

step "방화벽 (ssh 만 허용)"
ufw allow OpenSSH >/dev/null 2>&1 || ufw allow 22/tcp >/dev/null
for p in $(sshd -T 2>/dev/null | awk '$1=="port"{print $2}'); do ufw allow "$p/tcp" >/dev/null; done
ufw --force enable >/dev/null
ufw status | head -5

step "스왑 (메모리 2GB 미만이면 2GB)"
mem_kb=$(awk '/^MemTotal/{print $2}' /proc/meminfo)
if [ "$mem_kb" -lt 2000000 ] && [ -z "$(swapon --noheadings)" ]; then
    [ -f /swapfile ] || { fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048; }
    chmod 600 /swapfile
    if mkswap /swapfile >/dev/null && swapon /swapfile; then
        grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
        echo 'vm.swappiness=10' > /etc/sysctl.d/90-sodam-swap.conf
        sysctl -q -p /etc/sysctl.d/90-sodam-swap.conf || true
    else
        echo "※ 이 VPS 는 스왑을 못 켜요 (가상화 제한) — 건너뜀"; rm -f /swapfile
    fi
fi
free -m | head -3

step "로그 크기 제한 (journald 200MB)"
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=200M\nMaxRetentionSec=1month\n' > /etc/systemd/journald.conf.d/sodam.conf
systemctl restart systemd-journald

step "사용자 $USR"
id "$USR" >/dev/null 2>&1 || useradd --system --home-dir /var/lib/sodam --create-home --shell /usr/sbin/nologin "$USR"

step "코드 $APP ($BRANCH)"
if [ -d "$APP/.git" ]; then
    git -C "$APP" fetch -q origin "$BRANCH"
    git -C "$APP" checkout -q "$BRANCH" 2>/dev/null || git -C "$APP" checkout -q -b "$BRANCH" FETCH_HEAD
    git -C "$APP" merge -q --ff-only FETCH_HEAD || die "코드 갱신 실패 (서버에서 파일을 고쳤나요?): git -C $APP status"
elif [ -e "$APP" ] && [ -n "$(ls -A "$APP")" ]; then
    die "$APP 가 이미 있는데 git 저장소가 아니에요. 옮기거나 지운 뒤 다시 실행하세요."
else
    git clone -q --branch "$BRANCH" "$REPO" "$APP"
fi
# 코드는 root 소유(봇은 읽기만), 봇이 쓰는 곳만 sodam 소유
git config --system --get-all safe.directory 2>/dev/null | grep -qx "$APP" || git config --system --add safe.directory "$APP"
install -d -o "$USR" -g "$USR" -m 700 "$APP/data" "$APP/backups"
git -C "$APP" rev-parse --short HEAD > "$APP/VERSION"
chmod +x "$APP"/deploy/*.sh
ln -sf "$APP/deploy/update.sh" /usr/local/bin/sodam-update

step "파이썬 가상환경 + 패키지"
[ -x "$APP/.venv/bin/python" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --disable-pip-version-check --upgrade pip
"$APP/.venv/bin/pip" install -q --disable-pip-version-check -r "$APP/requirements.txt"

# ── 비밀값·DB 가져오기 ──
TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT
if [ -n "$BUNDLE" ]; then
    step "이사 꾸러미 풀기"
    case $BUNDLE in
        *.enc) echo "Claude 가 알려준 꾸러미 비밀번호를 붙여넣고 Enter (화면엔 안 보여요):"
               openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in "$BUNDLE" -out "$TMPD/b.tgz" \
                   || die "비밀번호가 틀렸거나 파일이 깨졌어요." ;;
        *) cp "$BUNDLE" "$TMPD/b.tgz" ;;
    esac
    tar -xzf "$TMPD/b.tgz" -C "$TMPD"
    [ -f "$TMPD/bundle/.env" ] || die "꾸러미에 .env 가 없어요."
    ENV_SRC=$TMPD/bundle/.env
    [ -f "$TMPD/bundle/.env.dealer" ] && ENV_DEALER_SRC=$TMPD/bundle/.env.dealer
    [ -f "$TMPD/bundle/sodam.db" ] && DB_SRC=$TMPD/bundle/sodam.db
fi

put_env() {   # 윈도우 메모장이 붙이는 BOM·CRLF 제거, 권한 600
    local src=$1 dst=$2 v
    tr -d '\r' < "$src" | sed '1s/^\xEF\xBB\xBF//' > "$TMPD/env"
    grep -q '^TELEGRAM_BOT_TOKEN=.' "$TMPD/env" || die "$src 에 TELEGRAM_BOT_TOKEN 이 비어 있어요."
    for k in DB_PATH BACKUP_DIR; do
        v=$(sed -n "s/^$k=//p" "$TMPD/env" | tail -1 | tr -d "\"' ")
        case $v in ''|data/*) ;; *) die "$src 의 $k=$v → data/ 아래 상대 경로여야 해요 (예: $k=data/sodam.db). 고친 뒤 다시." ;; esac
    done
    install -o root -g "$USR" -m 640 "$TMPD/env" "$dst"
}
[ -z "$ENV_SRC" ] || { step ".env 설치"; put_env "$ENV_SRC" "$APP/.env"; }
[ -z "$ENV_DEALER_SRC" ] || { step ".env.dealer 설치"; put_env "$ENV_DEALER_SRC" "$APP/.env.dealer"; }

if [ -n "$DB_SRC" ]; then
    step "DB 가져오기"
    case $DB_SRC in *.gz) gunzip -c "$DB_SRC" > "$TMPD/in.db" ;; *) cp "$DB_SRC" "$TMPD/in.db" ;; esac
    [ "$(sqlite3 "$TMPD/in.db" 'PRAGMA integrity_check;')" = ok ] || die "DB 무결성 검사 실패: $DB_SRC"
    if [ -f "$APP/data/sodam.db" ] && cmp -s "$TMPD/in.db" "$APP/data/sodam.db"; then
        echo "같은 DB 가 이미 있어요 — 건너뜀"
    else
        if [ -f "$APP/data/sodam.db" ]; then
            [ "$REPLACE_DB" -eq 1 ] || die "이미 $APP/data/sodam.db 가 있어요. 정말 바꾸려면 --replace-db 를 붙이세요 (기존 DB 는 backups/ 에 보관)."
            systemctl stop sodam sodam-dealer 2>/dev/null || true
            sqlite3 "$APP/data/sodam.db" ".backup '$APP/backups/before-replace-$(date +%Y%m%d-%H%M%S).db'"
        fi
        systemctl stop sodam sodam-dealer 2>/dev/null || true
        install -o "$USR" -g "$USR" -m 600 "$TMPD/in.db" "$APP/data/sodam.db"
        rm -f "$APP/data/sodam.db-wal" "$APP/data/sodam.db-shm"
    fi
    echo "DB: $(du -h "$APP/data/sodam.db" | cut -f1), 방 $(sqlite3 "$APP/data/sodam.db" 'SELECT count(*) FROM chats;' 2>/dev/null || echo ?)개"
fi

step "systemd 등록"
cp "$APP"/deploy/sodam*.service "$APP"/deploy/sodam*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q sodam.service sodam-health.timer sodam-backup.timer
systemctl start sodam-health.timer sodam-backup.timer
if [ -f "$APP/.env.dealer" ]; then systemctl enable -q sodam-dealer.service; fi
if [ "$AUTO_UPDATE" -eq 1 ]; then systemctl enable -q --now sodam-autoupdate.timer; echo "자동 배포 켜짐 (2분마다 $BRANCH 확인)"; fi

if [ ! -f "$APP/.env" ]; then
    step "준비 끝 (봇은 아직 안 켬)"
    echo ".env 가 없어서 봇을 켜지 않았어요. 이사 꾸러미를 올린 뒤:  bash $APP/deploy/install.sh --bundle /root/<파일>"
    exit 0
fi

if ! systemctl is-active -q sodam && [ "$YES" -eq 0 ] && [ -t 0 ]; then
    echo
    echo "⚠️  같은 봇 토큰으로 두 곳에서 켜면 텔레그램이 서로 끊어요(409 Conflict) — 메시지를 놓치고 DB 가 둘로 갈라져요."
    read -r -p "옛 봇(클라우드 컨테이너)을 끄셨나요? [y/N] " ans
    case $ans in y|Y|yes|ㅇ) ;; *) die "옛 봇을 먼저 끄고 다시 실행하세요 (deploy/migrate_from_container.md 4단계)." ;; esac
fi

step "봇 켜기"
since=$(date +%s)
# shellcheck disable=SC2046
systemctl restart sodam $( [ -f "$APP/.env.dealer" ] && echo sodam-dealer )
ver=$(cat "$APP/VERSION")
for _ in $(seq 60); do
    sleep 1
    line=$(journalctl -u sodam --since "@$since" -o cat --no-pager | grep -F "시작! (버전 $ver" || true)
    [ -n "$line" ] && break
done
if [ -n "${line:-}" ] && systemctl is-active -q sodam; then
    printf '\n\033[1;32m✅ %s\033[0m\n' "$line"
    journalctl -u sodam --since "@$since" -o cat --no-pager | grep -F "/owner" || true
    echo "로그 보기: journalctl -u sodam -f   (나가기 Ctrl+C)"
else
    journalctl -u sodam --since "@$since" -n 40 -o cat --no-pager || true
    die "60초 안에 '시작! (버전 $ver' 가 안 보여요. 위 로그를 Claude 에게 보여주세요."
fi
