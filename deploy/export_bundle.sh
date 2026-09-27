#!/usr/bin/env bash
# 이사 꾸러미 만들기 (옛 서버·클라우드 컨테이너에서, 봇을 끈 뒤): .env(+.env.dealer) + DB 일관된 복사본
# → tar.gz → AES-256 암호화(.enc). 비밀번호는 화면에 한 번만 출력 (꾸러미와 따로 전달).
#   deploy/export_bundle.sh [앱 폴더(기본 .)] [--telegram]
#   --telegram: 봇 토큰으로 오너(OWNER_IDS 첫 번째) 1:1 에 파일 전송 (sendDocument 는 polling 과 충돌 안 함)
# 새 서버: bash /opt/sodam/deploy/install.sh --bundle /root/<파일>.enc
set -euo pipefail
DIR=.; SEND=0
for a in "$@"; do case $a in --telegram) SEND=1 ;; *) DIR=$a ;; esac; done
DIR=$(cd "$DIR" && pwd)
PYTHON=${PYTHON:-python3}
OUT_DIR=${OUT_DIR:-$DIR}

[ -f "$DIR/.env" ] || { echo "$DIR/.env 가 없어요" >&2; exit 1; }
env_get() { sed -n "s/^$1=//p" "$DIR/.env" | tail -1 | tr -d "\"'\r "; }
DB_REL=$(env_get DB_PATH); DB_REL=${DB_REL:-data/sodam.db}
case $DB_REL in /*) DB=$DB_REL ;; *) DB=$DIR/$DB_REL ;; esac
[ -f "$DB" ] || { echo "DB 가 없어요: $DB" >&2; exit 1; }

if pgrep -f -- "-m sodam$" >/dev/null && [ "${ALLOW_RUNNING:-0}" != 1 ]; then
    echo "!! 봇이 아직 돌고 있어요 (pgrep -af 'm sodam\$'). 감시(supervise.sh)·봇을 먼저 끄세요 — 켠 채로 옮기면" >&2
    echo "   그 뒤 대화·포인트가 빠지고, 새 서버와 동시에 돌면 409 Conflict. (정말이면 ALLOW_RUNNING=1)" >&2
    exit 1
fi

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
mkdir "$TMP/bundle"
cp "$DIR/.env" "$TMP/bundle/.env"
[ -f "$DIR/.env.dealer" ] && cp "$DIR/.env.dealer" "$TMP/bundle/.env.dealer"
"$PYTHON" - "$DB" "$TMP/bundle/sodam.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=30)
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
dst.execute("PRAGMA journal_mode=DELETE")
n = dst.execute("SELECT count(*) FROM chats").fetchone()[0]
dst.close()
if ok != "ok":
    sys.exit(f"integrity_check 실패: {ok}")
print(f"DB ok (방 {n}개)")
PY
stamp=$(date +%Y%m%d-%H%M)
OUT="$OUT_DIR/sodam-migrate-$stamp.tar.gz.enc"
tar -czf "$TMP/b.tgz" -C "$TMP" bundle
PASS=$(openssl rand -hex 12)
PASS=$PASS openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -in "$TMP/b.tgz" -out "$OUT" -pass env:PASS
chmod 600 "$OUT"
echo "꾸러미: $OUT ($(du -h "$OUT" | cut -f1))"
echo "비밀번호: $PASS"

if [ "$SEND" -eq 1 ]; then
    TOKEN=$(env_get TELEGRAM_BOT_TOKEN)
    OWNER=$(env_get OWNER_IDS | cut -d, -f1)
    if [ -z "$OWNER" ]; then
        OWNER=$("$PYTHON" -c "import sqlite3,sys; r=sqlite3.connect(sys.argv[1]).execute('SELECT user_id FROM owners LIMIT 1').fetchone(); print(r[0] if r else '')" "$TMP/bundle/sodam.db")
    fi
    [ -n "$TOKEN" ] && [ -n "$OWNER" ] || { echo "토큰 또는 오너 ID 를 못 찾아서 전송 안 함" >&2; exit 1; }
    # 토큰이 명령줄(ps)에 안 보이게 curl 설정을 stdin 으로
    printf 'url = "https://api.telegram.org/bot%s/sendDocument"\n' "$TOKEN" \
        | curl -sS --fail -K - -F "chat_id=$OWNER" -F "document=@$OUT" \
               -F "caption=소담 이사 꾸러미 (암호화됨). 서버에 올린 뒤 이 메시지는 지우세요." -o /dev/null \
        && echo "텔레그램 전송 완료 → 오너($OWNER) 1:1"
fi
