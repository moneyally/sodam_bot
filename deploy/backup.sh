#!/usr/bin/env bash
# data/*.db 온라인 백업 → $BACKUP_DIR/<이름>-YYYYmmdd-HHMMSS.db.gz, KEEP_DAYS 일 지난 것 삭제.
# 봇이 켜져 있어도 안전: sqlite3 백업 API (WAL 의 아직 합쳐지지 않은 내용까지 포함한 일관된 복사본) + integrity_check.
# 사용: deploy/backup.sh   (sodam-backup.timer 가 매일 04:30, User=sodam)
#
# 복원 (deploy/migrate_from_container.md '백업·복원'):
#   systemctl stop sodam sodam-dealer
#   gunzip -c /opt/sodam/backups/sodam-YYYYmmdd-HHMMSS.db.gz > /opt/sodam/data/sodam.db
#   rm -f /opt/sodam/data/sodam.db-wal /opt/sodam/data/sodam.db-shm
#   chown sodam:sodam /opt/sodam/data/sodam.db && systemctl start sodam
set -euo pipefail
APP_DIR=${APP_DIR:-/opt/sodam}
DATA_DIR=${DATA_DIR:-$APP_DIR/data}
BACKUP_DIR=${BACKUP_DIR:-$APP_DIR/backups}
KEEP_DAYS=${KEEP_DAYS:-14}
PYTHON=${PYTHON:-python3}

mkdir -p "$BACKUP_DIR"
stamp=$(date +%Y%m%d-%H%M%S)
shopt -s nullglob
dbs=("$DATA_DIR"/*.db)
[ ${#dbs[@]} -gt 0 ] || { echo "백업할 DB 없음: $DATA_DIR/*.db" >&2; exit 1; }

for db in "${dbs[@]}"; do
    name=$(basename "$db" .db)
    raw="$BACKUP_DIR/.$name-$stamp.db.part"
    out="$BACKUP_DIR/$name-$stamp.db.gz"
    trap 'rm -f "$raw" "$out.part"' EXIT
    "$PYTHON" - "$db" "$raw" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=30)
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)          # 바쁜 DB 면 페이지 단위로 알아서 다시 시도
src.close()
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
dst.execute("PRAGMA journal_mode=DELETE")   # 복원 때 -wal 없이 파일 하나로
dst.close()
if ok != "ok":
    sys.exit(f"integrity_check 실패: {ok}")
PY
    gzip -c -6 "$raw" > "$out.part"
    mv "$out.part" "$out"
    rm -f "$raw"
    trap - EXIT
    echo "ok $out ($(du -h "$out" | cut -f1))"
done

# 예약·인사 미디어 원본 (sodam/mediastore.py, 파일 이름 = 내용 해시라 같은 이름은 같은 파일 → 새 것만 복사)
if [ -d "$DATA_DIR/media" ]; then
    mkdir -p "$BACKUP_DIR/media"
    cp -n "$DATA_DIR"/media/* "$BACKUP_DIR/media/" 2>/dev/null || true
fi

# 오래된 백업 정리 (KEEP_DAYS 일 넘은 것)
find "$BACKUP_DIR" -maxdepth 1 -name '*.db.gz' -type f -mtime +"$((KEEP_DAYS - 1))" -print -delete | sed 's/^/deleted /'

# ── (선택) 서버 밖으로 복사 — 서버가 통째로 날아가도 살리려면 켜세요 ──
# rclone 설치·설정(apt install rclone; sudo -u sodam rclone config → 예: 이름 'gdrive' 구글드라이브) 뒤 아래 주석을 풀고
# sodam-backup.service 의 ReadWritePaths 에 /var/lib/sodam (rclone 설정 위치) 추가:
# rclone copy "$BACKUP_DIR" gdrive:sodam-backups --max-age 25h
# rclone delete gdrive:sodam-backups --min-age "${KEEP_DAYS}d"
