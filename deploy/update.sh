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
UNIT_DIR=${UNIT_DIR:-/etc/systemd/system}
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
# 결과 한 줄 → data/*.status (봇이 5분마다 읽어 실패면 오너 1:1 로 — sodam/diag.py report_server_status)
report() {   # report 파일이름 내용…
    local f="$APP_DIR/data/$1"; shift
    mkdir -p "$APP_DIR/data" 2>/dev/null || return 0
    printf '%s %s\n' "$(date '+%F %T')" "$*" > "$f" 2>/dev/null || return 0
    [ -z "$RUN_AS" ] || chown "$RUN_AS:" "$f" 2>/dev/null || true
}
log() { echo "$(date '+%F %T') [update] $*"; }
g() { git -C "$APP_DIR" "$@"; }

exec 9>"$LOCK"
flock -n 9 || { say "다른 갱신이 진행 중"; exit 0; }

units() {   # 켜 둔 봇들 (딜러는 .env.dealer 가 있고 켜 둔 경우만)
    echo sodam
    if [ -f "$APP_DIR/.env.dealer" ] && $SYSTEMCTL is-enabled -q sodam-dealer 2>/dev/null; then echo sodam-dealer; fi
}

# 📞 음성 담당(sodam-voice): 본체가 뜬 뒤 따로 — 패키지·서비스 파일 설치·재시작. 실패해도 본체는 그대로 (되돌리지 않음).
voice_setup() {
    [ -n "$RUN_AS" ] && [ -f "$APP_DIR/deploy/sodam-voice.service" ] || return 0
    # 음성 관련 파일이 안 바뀌었고 이미 돌고 있으면 그대로 (재시작 = 진행 중 통화가 끊김)
    if $SYSTEMCTL is-active -q sodam-voice 2>/dev/null && [ -n "${PREV:-}" ] \
        && g diff --quiet "$PREV" HEAD -- sodam/voice sodam/mtproto.py sodam/db.py sodam/config.py \
               requirements-voice.txt deploy/sodam-voice.service; then
        log "voice: 바뀐 것 없음 → 통화 유지"; return 0
    fi
    "$PY" -m pip install -q --disable-pip-version-check -r "$APP_DIR/requirements-voice.txt" \
        || { log "voice: 패키지 설치 실패 (본체는 정상)"; return 0; }
    if ! cmp -s "$APP_DIR/deploy/sodam-voice.service" "$UNIT_DIR/sodam-voice.service"; then
        cp "$APP_DIR/deploy/sodam-voice.service" "$UNIT_DIR/" && $SYSTEMCTL daemon-reload
    fi
    $SYSTEMCTL is-enabled -q sodam-voice 2>/dev/null || $SYSTEMCTL enable -q sodam-voice
    $SYSTEMCTL restart sodam-voice && log "voice: 음성 담당 재시작" || log "voice: 재시작 실패 (journalctl -u sodam-voice)"
}

# 🔌 원격 점검 창구(sodam-diag) + Caddy(https://<공인IP>.sslip.io, 인증서 자동) + 방화벽 80·443. 실패해도 본체는 그대로.
# 창구는 읽기 전용·토큰 필수 (sodam/diag.py). 토큰은 봇이 오너 1:1 로만 보냄.
DIAG_PORT=${DIAG_PORT:-8787}
diag_setup() {
    [ -n "$RUN_AS" ] && [ -f "$APP_DIR/deploy/sodam-diag.service" ] || return 0
    local apt=(env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 -y -q)
    if ! command -v caddy >/dev/null 2>&1; then   # 자동 보안 업데이트가 잠금을 잡고 있으면 기다림 · 목록이 오래됐으면 update 뒤 한 번 더
        "${apt[@]}" install caddy >/dev/null 2>&1 || { "${apt[@]}" update >/dev/null 2>&1 && "${apt[@]}" install caddy >/dev/null 2>&1; } \
            || { log "diag: caddy 설치 실패 (본체는 정상)"; report diag_setup.status "diag_fail caddy 설치 실패"; return 0; }
    fi
    local ip host conf
    ip=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')
    [ -n "$ip" ] || { log "diag: 공인 IP 를 모름"; report diag_setup.status "diag_fail 공인 IP 모름"; return 0; }
    host="${ip//./-}.sslip.io"
    conf=$(mktemp)
    printf '%s {\n\tencode gzip\n\treverse_proxy 127.0.0.1:%s\n}\n' "$host" "$DIAG_PORT" > "$conf"
    if ! cmp -s "$conf" /etc/caddy/Caddyfile; then
        cp "$conf" /etc/caddy/Caddyfile && $SYSTEMCTL reload-or-restart caddy && log "diag: Caddy → https://$host"
    fi
    rm -f "$conf"
    $SYSTEMCTL is-enabled -q caddy 2>/dev/null || $SYSTEMCTL enable -q caddy
    if command -v ufw >/dev/null 2>&1; then ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; fi
    if ! cmp -s "$APP_DIR/deploy/sodam-diag.service" "$UNIT_DIR/sodam-diag.service"; then
        cp "$APP_DIR/deploy/sodam-diag.service" "$UNIT_DIR/" && $SYSTEMCTL daemon-reload
    fi
    $SYSTEMCTL is-enabled -q sodam-diag 2>/dev/null || $SYSTEMCTL enable -q sodam-diag
    if $SYSTEMCTL restart sodam-diag; then
        log "diag: 점검 창구 재시작"; report diag_setup.status "diag_ok https://$host"
    else
        log "diag: 재시작 실패 (journalctl -u sodam-diag)"; report diag_setup.status "diag_fail sodam-diag 시작 실패"
    fi
}

# 자동 갱신 유닛(시간 제한 등)이 바뀌었으면 설치 — 지금 도는 갱신은 옛 설정 그대로 끝나고 다음부터 적용
unit_setup() {
    [ -n "$RUN_AS" ] || return 0
    local u
    for u in sodam-autoupdate.service sodam-autoupdate.timer; do
        [ -f "$APP_DIR/deploy/$u" ] || continue
        cmp -s "$APP_DIR/deploy/$u" "$UNIT_DIR/$u" && continue
        cp "$APP_DIR/deploy/$u" "$UNIT_DIR/" && $SYSTEMCTL daemon-reload && log "unit: $u 갱신"
    done
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
    # 점검 창구가 안 떠 있으면 10분마다 다시 설치 시도 (apt 잠금 같은 일시 실패 회복)
    if [ -n "$RUN_AS" ] && [ -f "$APP_DIR/deploy/sodam-diag.service" ] && ! $SYSTEMCTL is-active -q sodam-diag 2>/dev/null; then
        diag_setup
    fi
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
    { grep -B1 -A15 -E '^FAIL' "$TMP/tests.log" | head -80; } || true   # 어떤 테스트가 왜 (끝 40줄엔 안 남는 경우가 많았음)
    tail -n 40 "$TMP/tests.log"
    report update.status "tests_failed $(g rev-parse --short "$NEW"): $(grep -E '^FAIL' "$TMP/tests.log" | head -5 | tr '\n' ' ')"
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
    report update.status "ok $VER"
    voice_setup
    diag_setup
    unit_setup
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
    report update.status "rollback $VER (새 버전 시작 확인 실패 → 되돌림)"
    log "되돌림 완료: 버전 $VER 실행 중 (실패한 커밋은 GitHub 에서 고친 뒤 다시 sodam-update)"
else
    log "!! 되돌린 버전도 시작 확인 실패 — journalctl -u sodam -n 100 확인 필요"
fi
exit 1
