#!/usr/bin/env bash
# data/clones/*.env 를 보고 sodam@<slug> 를 켜고(enable --now), .env 가 사라진 클론은 끈다(disable --now).
# sodam-clone-apply.path 가 디렉터리 변화 때 root 로 호출. 봇은 .env 파일만 쓴다(systemctl 권한 없음).
set -euo pipefail
CLONES=/opt/sodam/data/clones
[ -d "$CLONES" ] || exit 0

# ① .env 가 있는 클론은 켠다 (이미 켜져 있으면 무해)
shopt -s nullglob
for f in "$CLONES"/*.env; do
    slug=$(basename "$f" .env)
    [[ "$slug" =~ ^[a-z0-9]{1,24}$ ]] || continue          # 안전: slug 모양만
    systemctl enable --now "sodam@${slug}.service" 2>/dev/null || true
done

# ② .env 가 사라졌는데 아직 켜져 있는 클론은 끈다
while IFS= read -r unit; do
    [ -n "$unit" ] || continue
    slug=$(sed -E 's/^sodam@(.+)\.service$/\1/' <<<"$unit")
    if [ ! -f "$CLONES/${slug}.env" ]; then
        systemctl disable --now "$unit" 2>/dev/null || true
    fi
done < <(systemctl list-units --all --plain --no-legend 'sodam@*.service' 2>/dev/null | awk '{print $1}')
