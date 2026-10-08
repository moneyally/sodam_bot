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

# 📞 진행 중 통화가 있나 (DB 읽기 전용). 20분 넘은 '안 끝난' 줄은 유령(통화 최대 15분)이라 무시.
VOICE_POSTPONE_MAX=${VOICE_POSTPONE_MAX:-3600}   # 통화 때문에 미루는 최대 시간(초) — 업데이트가 영영 막히지 않게
db_file() {
    local rel=""
    [ -f "$APP_DIR/.env" ] && rel=$(sed -n 's/^DB_PATH=//p' "$APP_DIR/.env" | tail -1 | tr -d "\"'\r ")
    rel=${rel:-data/sodam.db}
    case $rel in /*) echo "$rel" ;; *) echo "$APP_DIR/$rel" ;; esac
}
voice_busy() {
    local db n
    db=$(db_file)
    [ -f "$db" ] || return 1
    n=$("$PY" - "$db" 2>/dev/null <<'PYEOF'
import sqlite3, sys, time
try:
    c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=5)
    n = c.execute("SELECT COUNT(*) FROM voice_calls WHERE end_ts IS NULL AND start_ts>?",
                  (int(time.time()) - 1200,)).fetchone()[0]
    try:   # 🎵 노래 트는 중도 (테스트가 CPU 를 다 써서 '노래가 자꾸 멈춰요' — 2026-10-08 실제 신고). 지금 곡이 playing 인 방
        n += c.execute("SELECT COUNT(*) FROM music_sessions s WHERE s.end_ts IS NULL AND EXISTS "
                       "(SELECT 1 FROM music_queue q WHERE q.chat_id=s.chat_id AND q.state='playing')").fetchone()[0]
    except sqlite3.Error:
        pass
    print(n)
except sqlite3.Error:
    print(0)
PYEOF
) || return 1
    [ "${n:-0}" -gt 0 ] 2>/dev/null
}
# 통화 중이면 0 (= 미룸). 처음 미룬 시각을 data/update.postponed 에 두고 VOICE_POSTPONE_MAX 가 지나면 그냥 진행.
postpone_for_call() {
    local f="$APP_DIR/data/update.postponed" now first
    if [ "$FORCE" -eq 1 ] || ! voice_busy; then rm -f "$f"; return 1; fi
    now=$(date +%s)
    mkdir -p "$APP_DIR/data" 2>/dev/null || true
    [ -f "$f" ] || echo "$now" > "$f"
    first=$(cat "$f" 2>/dev/null); first=${first:-$now}
    if [ $((now - first)) -ge "$VOICE_POSTPONE_MAX" ]; then
        log "voice: 통화 때문에 $(( (now - first) / 60 ))분 미뤘음 → 더 안 미루고 진행"
        rm -f "$f"; return 1
    fi
    return 0
}

units() {   # 켜 둔 봇들 (딜러는 .env.dealer 가 있고 켜 둔 경우만)
    echo sodam
    if [ -f "$APP_DIR/.env.dealer" ] && $SYSTEMCTL is-enabled -q sodam-dealer 2>/dev/null; then echo sodam-dealer; fi
}

# 📞 음성 담당(sodam-voice): 본체가 뜬 뒤 따로 — 패키지·서비스 파일 설치·재시작. 실패해도 본체는 그대로 (되돌리지 않음).
# VOICE_SETUP=auto(기본: root 설치일 때만 — RUN_AS 있음) / 1(항상, 테스트용) / 0(안 함)
VOICE_SETUP=${VOICE_SETUP:-auto}
RETRY_STAMP="$APP_DIR/data/update.retry"   # 새 커밋 없을 때 설치 재시도 간격 (10분)
REQ_STAMP="$APP_DIR/data/requirements.installed"   # 마지막으로 설치 성공한 requirements.txt 의 sha256 — 같으면 pip 건너뜀
VOICE_PENDING="$APP_DIR/data/voice.restart_pending"   # 통화 중이라 못 한 음성 재시작 → 다음 타이머(새 커밋 없어도)에서
# 🎧 음질 높인 ntgcalls (deploy/ntgcalls/patch_hq.py 를 GitHub Actions 'ntgcalls HQ wheel' 로 빌드, 서버 /opt/sodam-hq 에 둠).
# 해시가 맞을 때만, 설치된 게 원래 부품이면 덮어씀 (pip 가 같은 3.0.0 이라 그냥 두지만 venv 를 새로 만들면 원래 것으로 돌아가서).
HQ_DIR=${HQ_DIR:-/opt/sodam-hq}
HQ_SHA=5ef208e14f9485dbb6b0c19e8d389dc21a3354489324e154ee7913e0cac24bd0
hq_wheel() {
    local w so
    w=$(ls "$HQ_DIR"/ntgcalls-3.0.0-cp312-*.whl 2>/dev/null | head -n1 || true)
    [ -n "$w" ] || return 0
    [ "$(sha256sum "$w" | cut -d' ' -f1)" = "$HQ_SHA" ] || { log "voice: 음질 부품 해시가 다름 → 안 깖"; return 0; }
    so=$("$PY" -c 'import importlib.util as u; print(u.find_spec("ntgcalls").origin)' 2>/dev/null) || return 0
    grep -q NTG_OPUS_BITRATE "$so" 2>/dev/null && return 0
    "$PY" -m pip install -q --disable-pip-version-check --force-reinstall --no-deps "$w" \
        && log "voice: 음질 높인 ntgcalls 설치" || log "voice: 음질 부품 설치 실패 (원래 것으로 계속)"
}

voice_setup() {
    case $VOICE_SETUP in
        0) return 0 ;;
        1) ;;
        *) [ -n "$RUN_AS" ] || return 0 ;;
    esac
    [ -f "$APP_DIR/deploy/sodam-voice.service" ] || return 0
    # 음성 관련 파일이 안 바뀌었고 이미 돌고 있으면 그대로 (재시작 = 진행 중 통화가 끊김). 미뤄 둔 재시작이 있으면 비교 안 함.
    if [ ! -f "$VOICE_PENDING" ] && $SYSTEMCTL is-active -q sodam-voice 2>/dev/null && [ -n "${PREV:-}" ] \
        && g diff --quiet "$PREV" HEAD -- sodam/voice sodam/mtproto.py sodam/db.py sodam/config.py \
               requirements-voice.txt deploy/sodam-voice.service; then
        log "voice: 바뀐 것 없음 → 통화 유지"; return 0
    fi
    # 테스트(약 20분) 도중 통화가 시작됐을 수 있음 → 재시작 바로 전에 다시 확인, 통화 중이면 이번엔 건너뛰고 표시만
    if voice_busy; then
        mkdir -p "$APP_DIR/data" 2>/dev/null || true
        date +%s > "$VOICE_PENDING" 2>/dev/null || true
        log "voice: 통화 중 → 음성 담당 재시작은 통화 끝난 뒤 다음 타이머로 미룸"
        return 0
    fi
    rm -f "$VOICE_PENDING"
    "$PY" -m pip install -q --disable-pip-version-check -r "$APP_DIR/requirements-voice.txt" \
        || { log "voice: 패키지 설치 실패 (본체는 정상)"; return 0; }
    hq_wheel
    if ! cmp -s "$APP_DIR/deploy/sodam-voice.service" "$UNIT_DIR/sodam-voice.service"; then
        cp "$APP_DIR/deploy/sodam-voice.service" "$UNIT_DIR/" && $SYSTEMCTL daemon-reload
    fi
    $SYSTEMCTL is-enabled -q sodam-voice 2>/dev/null || $SYSTEMCTL enable -q sodam-voice
    $SYSTEMCTL restart sodam-voice && log "voice: 음성 담당 재시작" || log "voice: 재시작 실패 (journalctl -u sodam-voice)"
}

# 🔌 원격 점검 창구(sodam-diag) + Caddy(https://<공인IP>.sslip.io, 인증서 자동) + 방화벽 80·443. 실패해도 본체는 그대로.
# 창구는 읽기 전용·토큰 필수 (sodam/diag.py). 토큰은 봇이 오너 1:1 로만 보냄.
DIAG_PORT=${DIAG_PORT:-8787}
SSHWS_PORT=${SSHWS_PORT:-8023}
SSHD_CONF=${SSHD_CONF:-/etc/ssh/sshd_config}
ROOT_KEYS=${ROOT_KEYS:-/root/.ssh/authorized_keys}
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
    if [ -f "$APP_DIR/deploy/sodam-sshws.service" ]; then   # 🔐 /sshws = SSH 웹소켓 다리 (sodam/sshws.py), 나머지 = 점검 창구
        printf '%s {\n\thandle /sshws {\n\t\treverse_proxy 127.0.0.1:%s\n\t}\n\thandle {\n\t\tencode gzip\n\t\treverse_proxy 127.0.0.1:%s\n\t}\n}\n' \
            "$host" "$SSHWS_PORT" "$DIAG_PORT" > "$conf"
    else
        printf '%s {\n\tencode gzip\n\treverse_proxy 127.0.0.1:%s\n}\n' "$host" "$DIAG_PORT" > "$conf"
    fi
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

# 🔐 SSH 웹소켓 다리 (sodam/sshws.py, 오너 결정 2026-10-01): 클로드 작업 환경은 HTTPS 만 나가서 22번이 막힘 → Caddy /sshws → 127.0.0.1:22.
# 지키는 것: 다리 = 점검 토큰 필요 · sshd = 127.0.0.1 에서 온 접속은 비밀번호 로그인 금지(키만) · 클로드 키 = from="127.0.0.1,::1" 로만.
# 끄기: systemctl disable --now sodam-sshws (다음 배포 때 다시 켜지지 않게 하려면 deploy/sodam-sshws.service 를 지우고 배포)
# 🎵 노래 받기 우회 길 (Cloudflare WARP → wireproxy SOCKS5 127.0.0.1:40000). 프로그램은 고정 버전·해시 확인, WARP 무료 계정은 처음 한 번 등록.
WARP_DIR=${WARP_DIR:-/opt/sodam-warp}
WGCF_URL=https://github.com/ViRb3/wgcf/releases/download/v2.2.29/wgcf_2.2.29_linux_amd64
WIREPROXY_URL=https://github.com/whyvl/wireproxy/releases/download/v1.0.9/wireproxy_linux_amd64.tar.gz
warp_setup() {
    [ -n "$RUN_AS" ] && [ -f "$APP_DIR/deploy/sodam-warp.service" ] || return 0
    mkdir -p "$WARP_DIR" && chmod 755 "$WARP_DIR"
    if [ ! -x "$WARP_DIR/wireproxy" ]; then
        curl -fsSL --max-time 120 "$WIREPROXY_URL" | tar xz -C "$WARP_DIR" wireproxy \
            || { log "warp: wireproxy 받기 실패 (노래는 예전 방식)"; report warp_setup.status "warp_fail wireproxy"; return 0; }
    fi
    if [ ! -s "$WARP_DIR/wgcf-profile.conf" ]; then
        [ -x "$WARP_DIR/wgcf" ] || { curl -fsSL --max-time 120 -o "$WARP_DIR/wgcf" "$WGCF_URL" && chmod +x "$WARP_DIR/wgcf"; } \
            || { log "warp: wgcf 받기 실패"; report warp_setup.status "warp_fail wgcf"; return 0; }
        (cd "$WARP_DIR" && { [ -s wgcf-account.toml ] || ./wgcf register --accept-tos >/dev/null 2>&1; } && ./wgcf generate >/dev/null 2>&1) \
            || { log "warp: WARP 등록 실패"; report warp_setup.status "warp_fail register"; return 0; }
    fi
    printf 'WGConfig = %s/wgcf-profile.conf\n\n[Socks5]\nBindAddress = 127.0.0.1:40000\n' "$WARP_DIR" > "$WARP_DIR/wireproxy.conf"
    chown -R "$RUN_AS:" "$WARP_DIR"; chmod 600 "$WARP_DIR"/wgcf-*.* 2>/dev/null || true
    if ! cmp -s "$APP_DIR/deploy/sodam-warp.service" "$UNIT_DIR/sodam-warp.service"; then
        cp "$APP_DIR/deploy/sodam-warp.service" "$UNIT_DIR/" && $SYSTEMCTL daemon-reload
    fi
    $SYSTEMCTL is-enabled -q sodam-warp 2>/dev/null || $SYSTEMCTL enable -q sodam-warp
    $SYSTEMCTL is-active -q sodam-warp 2>/dev/null || $SYSTEMCTL restart sodam-warp
    sleep 3
    if curl -s --max-time 15 -x socks5h://127.0.0.1:40000 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null | grep -q '^warp=on'; then
        report warp_setup.status "warp_ok"
    else
        log "warp: 길이 안 열림 (노래는 예전 방식으로 계속)"; report warp_setup.status "warp_fail 연결"
    fi
}

sshws_setup() {
    [ -n "$RUN_AS" ] && [ -f "$APP_DIR/deploy/sodam-sshws.service" ] && [ -f "$APP_DIR/deploy/claude_ssh.pub" ] || return 0
    local pub line mark_b="# >>> sodam-sshws" mark_e="# <<< sodam-sshws" tmp
    pub=$(head -n1 "$APP_DIR/deploy/claude_ssh.pub")
    case "$pub" in ssh-ed25519\ *) ;; *) log "sshws: 공개키 모양이 이상함 → 건너뜀"; report sshws_setup.status "sshws_fail 공개키"; return 0;; esac
    # 1) sshd: 다리(127.0.0.1)로 온 접속은 키만 + root 는 키로만 (맨 끝 Match 블록, 표시 사이만 바꿈 · sshd -t 통과해야 적용)
    if [ -f "$SSHD_CONF" ]; then
        tmp=$(mktemp)
        awk -v b="$mark_b" -v e="$mark_e" '$0==b{skip=1;next} $0==e{skip=0;next} !skip' "$SSHD_CONF" > "$tmp"
        printf '%s\nMatch Address 127.0.0.1,::1\n\tPasswordAuthentication no\n\tKbdInteractiveAuthentication no\n\tPermitRootLogin prohibit-password\n%s\n' \
            "$mark_b" "$mark_e" >> "$tmp"
        if ! cmp -s "$tmp" "$SSHD_CONF"; then
            if sshd -t -f "$tmp" 2>/dev/null; then
                cp "$SSHD_CONF" "$SSHD_CONF.sodam-bak" && cat "$tmp" > "$SSHD_CONF" \
                    && { $SYSTEMCTL reload ssh 2>/dev/null || $SYSTEMCTL reload sshd 2>/dev/null || true; } && log "sshws: sshd 다리 규칙 적용"
            else
                log "sshws: sshd -t 실패 → sshd 설정 안 바꿈, 다리도 안 켬"; report sshws_setup.status "sshws_fail sshd -t"; rm -f "$tmp"; return 0
            fi
        fi
        rm -f "$tmp"
    fi
    # 2) 클로드 키: from=127.0.0.1 (다리로만) · 포워딩 없음
    line="from=\"127.0.0.1,::1\",no-agent-forwarding,no-X11-forwarding,no-port-forwarding $pub"
    mkdir -p "$(dirname "$ROOT_KEYS")" && chmod 700 "$(dirname "$ROOT_KEYS")"
    touch "$ROOT_KEYS" && chmod 600 "$ROOT_KEYS"
    if ! grep -qF "$pub" "$ROOT_KEYS"; then echo "$line" >> "$ROOT_KEYS" && log "sshws: 클로드 키 등록 (127.0.0.1 전용)"; fi
    # 3) 다리 유닛
    if ! cmp -s "$APP_DIR/deploy/sodam-sshws.service" "$UNIT_DIR/sodam-sshws.service"; then
        cp "$APP_DIR/deploy/sodam-sshws.service" "$UNIT_DIR/" && $SYSTEMCTL daemon-reload
    fi
    $SYSTEMCTL is-enabled -q sodam-sshws 2>/dev/null || $SYSTEMCTL enable -q sodam-sshws
    if $SYSTEMCTL restart sodam-sshws; then
        log "sshws: 다리 재시작"; report sshws_setup.status "sshws_ok"
    else
        log "sshws: 다리 시작 실패 (journalctl -u sodam-sshws)"; report sshws_setup.status "sshws_fail 시작 실패"
    fi
}

# 🧪 작업실(sodam-workshop): 격리 코드 실행 서버 (sodam/workshop/server.py). 실패해도 본체는 그대로.
# venv 는 --copies (진짜 실행 파일이어야 AppArmor 프로필이 붙음 → user namespace 허용). 패키지는 목록이 바뀔 때만.
WORKSHOP_DIR=${WORKSHOP_DIR:-/opt/sodam-sandbox}
APPARMOR_DIR=${APPARMOR_DIR:-/etc/apparmor.d}
workshop_check() {   # 작업실에 실제로 일을 시켜 봄: 계산·인터넷 막힘·봇 폴더 안 보임
    "$WORKSHOP_DIR/venv/bin/python3" -I - "$APP_DIR" <<'EOF'
import json, socket, sys
def ask(req):
    s = socket.socket(socket.AF_UNIX); s.settimeout(60); s.connect("/run/sodam-workshop/sock")
    s.sendall(json.dumps(req).encode() + b"\n"); data = b""
    while not data.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk: break
        data += chunk
    return json.loads(data)
run = lambda code: ask({"op": "run", "session": "_check", "code": code, "files": {}, "limits": {}})
bad = []
r = run("print(6*7)")
if r.get("output", "").strip() != "42": bad.append("calc:" + r.get("status", "?"))
r = run("import socket\nsocket.create_connection(('1.1.1.1', 53), timeout=3)\nprint('NET_OPEN')")
if "NET_OPEN" in r.get("output", ""): bad.append("net_open")
r = run(f"import os\nprint('APP_SEEN' if os.listdir({sys.argv[1]!r}) else 'APP_EMPTY')")
if "APP_SEEN" in r.get("output", ""): bad.append("app_dir_visible")
print("ok" if not bad else "fail " + ",".join(bad))
EOF
}
workshop_setup() {
    [ -n "$RUN_AS" ] && [ -f "$APP_DIR/deploy/sodam-workshop.service" ] && [ -f "$APP_DIR/sodam/workshop/server.py" ] || return 0
    local vpy="$WORKSHOP_DIR/venv/bin/python3" req="$APP_DIR/deploy/sandbox-requirements.txt" changed=0 res
    mkdir -p "$WORKSHOP_DIR" && chmod 755 "$WORKSHOP_DIR"
    if [ ! -x "$vpy" ] || [ -L "$vpy" ]; then
        log "workshop: venv 새로 (--copies)"
        rm -rf "$WORKSHOP_DIR/venv"
        python3 -m venv --copies "$WORKSHOP_DIR/venv" \
            || { log "workshop: venv 실패"; report workshop_setup.status "workshop_fail venv"; return 0; }
        rm -f "$WORKSHOP_DIR/requirements.installed"; changed=1
    fi
    if ! cmp -s "$req" "$WORKSHOP_DIR/requirements.installed"; then
        if "$vpy" -m pip install -q --disable-pip-version-check --no-cache-dir -r "$req"; then
            cp "$req" "$WORKSHOP_DIR/requirements.installed"; changed=1; log "workshop: 패키지 설치"
        else
            log "workshop: 패키지 설치 실패"; report workshop_setup.status "workshop_fail pip"; return 0
        fi
    fi
    if ! cmp -s "$APP_DIR/sodam/workshop/server.py" "$WORKSHOP_DIR/server.py"; then
        install -m 644 "$APP_DIR/sodam/workshop/server.py" "$WORKSHOP_DIR/server.py"; changed=1
    fi
    if [ -f "$APP_DIR/deploy/apparmor-sodam-workshop" ] && command -v apparmor_parser >/dev/null 2>&1; then
        if ! cmp -s "$APP_DIR/deploy/apparmor-sodam-workshop" "$APPARMOR_DIR/sodam-workshop"; then
            install -m 644 "$APP_DIR/deploy/apparmor-sodam-workshop" "$APPARMOR_DIR/sodam-workshop"; changed=1
        fi
        apparmor_parser -r "$APPARMOR_DIR/sodam-workshop" 2>/dev/null \
            || { log "workshop: AppArmor 프로필 적용 실패"; report workshop_setup.status "workshop_fail apparmor"; return 0; }
    fi
    if ! cmp -s "$APP_DIR/deploy/sodam-workshop.service" "$UNIT_DIR/sodam-workshop.service"; then
        cp "$APP_DIR/deploy/sodam-workshop.service" "$UNIT_DIR/" && $SYSTEMCTL daemon-reload; changed=1
    fi
    $SYSTEMCTL is-enabled -q sodam-workshop 2>/dev/null || $SYSTEMCTL enable -q sodam-workshop
    if [ "$changed" -eq 1 ] || ! $SYSTEMCTL is-active -q sodam-workshop 2>/dev/null; then
        $SYSTEMCTL restart sodam-workshop || { log "workshop: 시작 실패 (journalctl -u sodam-workshop)"; report workshop_setup.status "workshop_fail 시작"; return 0; }
        sleep 3
    fi
    res=$(workshop_check 2>&1 | tail -n 1) || true   # 점검 실패로 갱신 스크립트가 멈추면 안 됨 (set -e·pipefail)
    log "workshop: 점검 $res"; report workshop_setup.status "workshop_$res"
}

# 자동 갱신 유닛(시간 제한 등)이 바뀌었으면 설치 — 지금 도는 갱신은 옛 설정 그대로 끝나고 다음부터 적용
unit_setup() {
    [ -n "$RUN_AS" ] || return 0
    local u
    for u in sodam-autoupdate.service sodam-autoupdate.timer; do
        [ -f "$APP_DIR/deploy/$u" ] || continue
        cmp -s "$APP_DIR/deploy/$u" "$UNIT_DIR/$u" && continue
        cp "$APP_DIR/deploy/$u" "$UNIT_DIR/" && $SYSTEMCTL daemon-reload && log "unit: $u 갱신"
        if [ "$u" = sodam-autoupdate.timer ]; then $SYSTEMCTL restart "$u" || true; fi   # 새 간격 바로 (도는 중인 이 스크립트는 안 끊김)
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
    # 지난번에 통화 중이라 미룬 음성 담당 재시작 → 이제 통화가 없으면 함 (있으면 또 미룸)
    if [ -f "$VOICE_PENDING" ]; then
        voice_setup
    fi
    # 아래 설치 재시도는 10분에 한 번만 (타이머는 2분마다 — 실패가 계속되면 2분마다 apt·재시작을 되풀이하지 않게)
    if [ -f "$RETRY_STAMP" ] && [ -z "$(find "$RETRY_STAMP" -mmin +9 2>/dev/null)" ]; then exit 0; fi
    touch "$RETRY_STAMP" 2>/dev/null || true
    # 점검 창구가 안 떠 있으면 다시 설치 시도 (apt 잠금 같은 일시 실패 회복)
    if [ -n "$RUN_AS" ] && [ -f "$APP_DIR/deploy/sodam-diag.service" ] && ! $SYSTEMCTL is-active -q sodam-diag 2>/dev/null; then
        diag_setup
    fi
    # SSH 다리가 안 떠 있으면 다시 설치 시도 (첫 설치는 새 update.sh 가 도는 다음 타이머부터)
    if [ -n "$RUN_AS" ] && [ -f "$APP_DIR/deploy/sodam-sshws.service" ] && ! $SYSTEMCTL is-active -q sodam-sshws 2>/dev/null; then
        diag_setup
        sshws_setup
    fi
    # 노래 우회 길이 안 떠 있으면 다시 (첫 설치는 새 update.sh 가 도는 다음 타이머부터)
    if [ -n "$RUN_AS" ] && [ -f "$APP_DIR/deploy/sodam-warp.service" ] && ! $SYSTEMCTL is-active -q sodam-warp 2>/dev/null; then
        warp_setup
    fi
    # 작업실이 안 떠 있거나 마지막 점검이 실패면 다시 (첫 설치는 새 update.sh 가 도는 다음 타이머부터)
    if [ -n "$RUN_AS" ] && [ -f "$APP_DIR/deploy/sodam-workshop.service" ] \
        && { ! $SYSTEMCTL is-active -q sodam-workshop 2>/dev/null || ! grep -q ' workshop_ok' "$APP_DIR/data/workshop_setup.status" 2>/dev/null; }; then
        workshop_setup
    fi
    exit 0
fi
if ! g merge-base --is-ancestor "$PREV" "$NEW"; then
    log "!! origin/$BRANCH 가 지금 커밋에서 이어지지 않음 (강제 푸시?) → 중단. 확인 후 수동: git -C $APP_DIR reset --hard $NEW"
    exit 1
fi
log "$(g rev-parse --short "$PREV") → $(g rev-parse --short "$NEW") ($BRANCH)"

# 0) 음성 통화 중이면 무거운 테스트(약 10분·1.5코어)를 다음 타이머(2분 뒤 다시 확인)로 — 통화 소리가 끊기지 않게 (최대 60분, --force 는 바로)
if postpone_for_call; then
    log "voice: 통화 중 → 테스트·재시작을 다음 타이머로 미룸 (바로 하려면 sodam-update --force)"
    exit 0
fi

# 1) 새 코드를 임시 폴더에 풀어 테스트 (실제 폴더·data·.env 는 안 건드림, 네트워크·AI 호출 없음)
TMP=$(mktemp -d "${TMPDIR:-/var/tmp}/sodam-test.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
g archive "$NEW" | tar -x -C "$TMP"
# 패키지 설치는 requirements.txt 가 지난 설치 성공 때와 다를 때만 (매번 pip 확인 ~수십 초). 실패하면 표시를 지워 다음에 다시
REQ_SUM=$(sha256sum "$TMP/requirements.txt" | cut -d' ' -f1)
if [ "$(cat "$REQ_STAMP" 2>/dev/null)" != "$REQ_SUM" ]; then
    rm -f "$REQ_STAMP"
    "$PY" -m pip install -q --disable-pip-version-check -r "$TMP/requirements.txt"
    echo "$REQ_SUM" > "$REQ_STAMP" 2>/dev/null || true
fi
as_user=()
if [ -n "$RUN_AS" ]; then chown -R "$RUN_AS:" "$TMP"; as_user=(runuser -u "$RUN_AS" --); fi
log "tests…"
if ! (cd "$TMP" && "${as_user[@]}" env HOME="$TMP" TMPDIR="$TMP" "$PY" tests/run_all.py --jobs 2 > "$TMP/tests.log" 2>&1); then
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
    sshws_setup
    warp_setup
    workshop_setup
    unit_setup
    exit 0
fi

# 3) 시작 실패 → 되돌리기
log "!! 버전 $VER 시작 확인 실패 → 이전 커밋으로 되돌림"
$JOURNALCTL -u sodam -n 30 -o cat --no-pager 2>/dev/null || true
g reset -q --hard "$PREV"
rm -f "$REQ_STAMP"   # 되돌린 코드의 requirements 로 다시 설치 → 다음 배포는 새로 확인
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
