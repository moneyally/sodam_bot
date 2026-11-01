#!/usr/bin/env bash
# 🌐 WARP 출구 IP 교체 (sodam 유저가 실행 — root 불필요).
# 유튜브가 고정 datacenter IP(WARP)를 학습해 '봇이냐?'로 막으면, 새 Cloudflare WARP 계정을 등록해 출구 IP 를 바꾼다.
# wgcf-account.toml 을 지우고 register 를 다시 하면 새 계정·새 IP → wgcf generate 로 새 WireGuard profile.
# wireproxy.conf 는 profile 경로만 가리키므로 안 바꿔도 됨 → pkill wireproxy → systemd Restart=always 가 새 profile 로 재시작.
# 실패하면(네트워크·Cloudflare rate limit) 기존 account/profile 을 되돌려 원래 IP 유지 (음악이 안 멈추게).
# 호출: sodam/voice/warprotate.py. 인자: WARP 디렉토리 (/opt/sodam-warp · /opt/sodam-warp2).
set -u
dir="${1:?usage: warp_rotate.sh <warp_dir>}"
cd "$dir" 2>/dev/null || { echo "no_dir"; exit 1; }
[ -x ./wgcf ] || { echo "no_wgcf"; exit 1; }

cp -f wgcf-account.toml wgcf-account.toml.bak 2>/dev/null || true   # 실패 시 되돌릴 백업
cp -f wgcf-profile.conf wgcf-profile.conf.bak 2>/dev/null || true
rm -f wgcf-account.toml                                             # 새 계정을 받도록 기존 계정 제거

if ./wgcf register --accept-tos >/dev/null 2>&1 && ./wgcf generate >/dev/null 2>&1; then
    chmod 600 wgcf-account.toml wgcf-profile.conf 2>/dev/null || true
    rm -f wgcf-account.toml.bak wgcf-profile.conf.bak
    pkill -f "wireproxy.* $dir/wireproxy.conf" 2>/dev/null \
        || pkill -f "wireproxy.*$dir" 2>/dev/null || true          # systemd Restart=always → 새 IP 로 재시작
    echo "rotated"
    exit 0
else
    mv -f wgcf-account.toml.bak wgcf-account.toml 2>/dev/null || true   # 등록 실패 → 원래 IP 유지
    mv -f wgcf-profile.conf.bak wgcf-profile.conf 2>/dev/null || true
    echo "register_failed"
    exit 1
fi
