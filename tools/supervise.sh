#!/usr/bin/env bash
# 소담 감시 실행 (systemd 없는 곳용 — 클라우드 컨테이너 등. VPS 는 deploy/ 의 systemd 가 같은 일을 한다).
# 봇이 죽으면 5초 뒤 다시 켜고, 살아 있는데 멈추면(data/heartbeat 가 STALE 초 넘게 안 바뀜) 끄고 다시 켠다.
# 감시는 하나만 (flock). 시작할 때 감시 없이 떠 있던 봇은 정리한다 (두 개가 같은 토큰으로 받으면 Conflict).
# 사용: nohup tools/supervise.sh <실행 폴더> <python 경로> >> bot.log 2>&1 &
# 코드 갱신 후 재시작: 봇 프로세스만 끄면 됨 (pkill -f "python -m sodam$") → 감시가 새 코드로 다시 켬.
set -u
DIR=${1:-.}; PY=${2:-python}
STALE=${STALE:-180}; GRACE=${GRACE:-60}; CHECK=${CHECK:-30}
cd "$DIR" || exit 1
mkdir -p data
exec 9>data/.supervise.lock
flock -n 9 || { echo "$(date '+%F %T') 감시가 이미 돌고 있음"; exit 0; }
log() { echo "$(date '+%F %T') [감시] $*"; }
pkill -f "$PY -m sodam$" && { log "감시 없이 떠 있던 봇 정리"; sleep 3; }
while true; do
  "$PY" -m sodam 9>&- &
  pid=$!
  log "소담 시작 (pid $pid)"
  sleep "$GRACE"
  while kill -0 "$pid" 2>/dev/null; do
    age=$(( $(date +%s) - $(stat -c %Y data/heartbeat 2>/dev/null || echo 0) ))
    if [ "$age" -gt "$STALE" ]; then
      log "하트비트 ${age}초 멈춤 → 재시작"
      kill "$pid"; sleep 10; kill -9 "$pid" 2>/dev/null
      break
    fi
    sleep "$CHECK"
  done
  wait "$pid"; code=$?
  log "소담 종료 (코드 $code) → 5초 뒤 재시작"
  sleep 5
done
