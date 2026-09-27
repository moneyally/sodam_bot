#!/usr/bin/env bash
# 서버 갱신 (root): git fetch → 새 코드를 임시 폴더에서 테스트 → 통과해야만 적용·재시작 →
# 로그에 '시작! (버전 <새 커밋>' 이 안 뜨면 이전 커밋으로 되돌리고 다시 켬.
# 사용: sodam-update [브랜치]      (= /opt/sodam/deploy/update.sh, install.sh 가 링크)
#       sodam-update --force      새 커밋이 없어도 테스트·재시작
#       sodam-update --quiet      새 커밋 없으면 조용히 끝 (sodam-autoupdate.timer 용)
set -euo pipefail
APP_DIR=${APP_DIR:-$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)}
PY=${PY:-$APP_DIR/.venv/bin/python}
RUN_AS=${RUN_AS-sodam}              # 테스트를 돌릴 사용자 (빈 값 = 지금 사용자, 테스트용)
SYSTEMCTL=${SYSTEMCTL:-systemctl}
JOURNALCTL=${JOURNALCTL:-journalctl}
START_WAIT=${START_WAIT:-90}
LOCK=${LOCK:-/run/lock/sodam-update.lock}
FORCE=0; QUIET=0; BRANCH=""
for a in "$@"; do
    case $a in
        --force) FORCE=1 ;;
        --quiet) QUIET=1 ;;
        -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
        -*) echo "모르는 옵션: $a" >&2; exit 2 ;;
        *) BRANCH=$a ;;
    esac
done
[ -z "$RUN_AS" ] || [ "$(id -u)" -eq 0 ] || { echo "root 로 실행하세요 (sudo sodam-update)." >&2; exit 1; }

say() { [ "$QUIET" -eq 1 ] || echo "$@"; }
log() { echo "$(date '+%F %T') [update] $*"; }
g() { git -C "$APP_DIR" "$@"; }

exec 9>"$LOCK"
flock -n 9 || { say "다른 갱신이 진행 중"; exit 0; }

units() {   # 켜 둔 봇들 (딜러는 .env.dealer 가 있고 켜 둔 경우만)
    echo sodam
    if [ -f "$APP_DIR/.env.dealer" ] && $SYSTEMCTL is-enabled -q sodam-dealer 2>/dev/null; then echo sodam-dealer; fi
}

# 재시작 후 서비스가 살아 있고 그 뒤 로그에 '시작! (버전 X' 가 뜨면 성공
restart_and_verify() {
    local ver=$1 since u ok
    since=$(date +%s)
    # shellcheck disable=SC2046
    $SYSTEMCTL restart $(units)
    for _ in $(seq "$START_WAIT"); do
        sleep 1
        ok=1
        for u in $(units); do
            $SYSTEMCTL is-active -q "$u" || { ok=0; break; }
            $JOURNALCTL -u "$u" --since "@$since" -o cat --no-pager 2>/dev/null \
                | grep -qF "시작! (버전 $ver" || { ok=0; break; }
        done
        [ "$ok" -eq 1 ] && return 0
    done
    return 1
}

BRANCH=${BRANCH:-$(g rev-parse --abbrev-ref HEAD)}
PREV=$(g rev-parse HEAD)
g fetch -q origin "$BRANCH"
NEW=$(g rev-parse FETCH_HEAD)
if [ "$PREV" = "$NEW" ] && [ "$FORCE" -eq 0 ]; then
    say "새 커밋 없음 ($(g rev-parse --short HEAD), $BRANCH)"
    exit 0
fi
if ! g merge-base --is-ancestor "$PREV" "$NEW"; then
    log "!! origin/$BRANCH 가 지금 커밋에서 이어지지 않음 (강제 푸시?) → 중단. 확인 후 수동: git -C $APP_DIR reset --hard $NEW"
    exit 1
fi
log "$(g rev-parse --short "$PREV") → $(g rev-parse --short "$NEW") ($BRANCH)"

# 1) 새 코드를 임시 폴더에 풀어 테스트 (실제 폴더·data·.env 는 안 건드림, 네트워크·AI 호출 없음)
TMP=$(mktemp -d "${TMPDIR:-/var/tmp}/sodam-test.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
g archive "$NEW" | tar -x -C "$TMP"
"$PY" -m pip install -q --disable-pip-version-check -r "$TMP/requirements.txt"
as_user=()
if [ -n "$RUN_AS" ]; then chown -R "$RUN_AS:" "$TMP"; as_user=(runuser -u "$RUN_AS" --); fi
log "tests…"
if ! (cd "$TMP" && "${as_user[@]}" env HOME="$TMP" TMPDIR="$TMP" "$PY" tests/run_all.py > "$TMP/tests.log" 2>&1); then
    tail -n 40 "$TMP/tests.log"
    log "!! 테스트 실패 → 적용 안 함. 봇은 이전 코드($(g rev-parse --short "$PREV"))로 계속 돔"
    exit 1
fi
log "tests ok"

# 2) 적용 + 재시작 + 시작 확인
g merge -q --ff-only "$NEW"
VER=$(g rev-parse --short HEAD)
echo "$VER" > "$APP_DIR/VERSION"
if restart_and_verify "$VER"; then
    log "ok: 버전 $VER 실행 중"
    exit 0
fi

# 3) 시작 실패 → 되돌리기
log "!! 버전 $VER 시작 확인 실패 → 이전 커밋으로 되돌림"
$JOURNALCTL -u sodam -n 30 -o cat --no-pager 2>/dev/null || true
g reset -q --hard "$PREV"
"$PY" -m pip install -q --disable-pip-version-check -r "$APP_DIR/requirements.txt" || true
VER=$(g rev-parse --short HEAD)
echo "$VER" > "$APP_DIR/VERSION"
if restart_and_verify "$VER"; then
    log "되돌림 완료: 버전 $VER 실행 중 (실패한 커밋은 GitHub 에서 고친 뒤 다시 sodam-update)"
else
    log "!! 되돌린 버전도 시작 확인 실패 — journalctl -u sodam -n 100 확인 필요"
fi
exit 1
