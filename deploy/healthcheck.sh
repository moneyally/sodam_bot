#!/usr/bin/env bash
# 하트비트 검사 (sodam-health.timer 가 1분마다, root). tools/supervise.sh 와 같은 규칙:
# 봇이 30초마다 data/heartbeat(딜러는 heartbeat-dealer)를 갱신 → STALE 초 넘게 안 바뀌면 그 봇만 재시작.
# 켜진 지 STALE 초가 안 된 봇은 건드리지 않음 (시작 중).
set -u
APP_DIR=${APP_DIR:-/opt/sodam}
STALE=${STALE:-180}
SYSTEMCTL=${SYSTEMCTL:-systemctl}

now=$(date +%s)
uptime_s=$(awk '{print int($1)}' "${PROC_UPTIME:-/proc/uptime}")   # 테스트는 가짜 파일
rc=0
for pair in "sodam:heartbeat" "sodam-dealer:heartbeat-dealer"; do
    unit=${pair%%:*}
    file="$APP_DIR/data/${pair#*:}"
    $SYSTEMCTL is-active -q "$unit" || continue
    started_us=$($SYSTEMCTL show -p ActiveEnterTimestampMonotonic --value "$unit" 2>/dev/null)
    running=$(( uptime_s - ${started_us:-0} / 1000000 ))
    [ "$running" -lt "$STALE" ] && continue
    mtime=$(stat -c %Y "$file" 2>/dev/null || echo 0)
    age=$(( now - mtime ))
    if [ "$age" -gt "$STALE" ]; then
        echo "$unit: heartbeat ${age}s old (> ${STALE}s) -> restart"
        $SYSTEMCTL restart "$unit" || rc=1
    fi
done
exit $rc
