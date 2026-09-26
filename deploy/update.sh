#!/usr/bin/env bash
# 서버 갱신: git pull → 의존성 설치 → 전체 테스트 통과해야 → 재시작. 어느 단계든 실패하면 중단(재시작 안 함).
# 사용: sudo -u sodam deploy/update.sh   (재시작 단계만 sudo 필요: sodam 사용자에게 'systemctl restart sodam' sudoers 허용)
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
PY="${PY:-/opt/sodam/venv/bin/python}"
SERVICE="${SERVICE:-sodam}"
SYSTEMCTL="${SYSTEMCTL:-sudo systemctl}"

cd "$APP_DIR"
echo "== git pull"
git pull --ff-only
echo "== pip install"
"$PY" -m pip install -q -r requirements.txt
echo "== tests"
if ! "$PY" tests/run_all.py; then
    echo "!! 테스트 실패 → 재시작하지 않음 (봇은 이전 코드로 계속 돔)" >&2
    exit 1
fi
echo "== restart $SERVICE"
$SYSTEMCTL restart "$SERVICE"
sleep 5
$SYSTEMCTL is-active --quiet "$SERVICE" || { echo "!! $SERVICE 가 안 떠 있음: journalctl -u $SERVICE -n 50" >&2; exit 1; }
echo "== done ($(git rev-parse --short HEAD))"
