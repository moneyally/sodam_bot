# 서버 배포 (systemd)

## 0. 옛 서버 봇부터 끄기 (필수)
- 같은 봇 토큰으로 polling 을 두 곳에서 하면 텔레그램이 `409 Conflict` 를 내고 둘 다 업데이트를 놓친다.
  **새 서버를 켜기 전에 옛 서버(PC·클라우드 세션 포함) 봇을 먼저 끈다.**
- SQLite(`data/sodam.db`)는 네트워크 공유(NFS·SMB·동기화 폴더)로 두 서버가 같이 쓸 수 없다(잠금 깨짐 → DB 손상).
  옮길 땐: 옛 봇 정지 → 옛 서버에서 `.백업` 또는 `data/backups/` 최신 `.db.gz` 를 복사 → 새 서버에서 복원(아래) → 새 봇 시작.

## 1. 설치 (Ubuntu 예시)
```bash
sudo useradd -r -m -d /opt/sodam sodam                     # root 가 아닌 전용 사용자
sudo -u sodam git clone https://github.com/<계정>/sodam_bot /opt/sodam/sodam_bot
sudo -u sodam python3 -m venv /opt/sodam/venv
sudo -u sodam /opt/sodam/venv/bin/pip install -r /opt/sodam/sodam_bot/requirements.txt
sudo -u sodam cp .env /opt/sodam/sodam_bot/.env && sudo chmod 600 /opt/sodam/sodam_bot/.env
# (DB 옮기는 경우) 복원: README.md '7. DB 백업' 의 복구 절차 → python tools/restore_check.py 로 먼저 검증
sudo cp /opt/sodam/sodam_bot/deploy/sodam.service /opt/sodam/sodam_bot/deploy/sodam-health.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sodam sodam-health.timer
journalctl -u sodam -f                                     # 로그 (journald)
```
경로·사용자를 바꿨으면 `sodam.service` 의 `User`·`WorkingDirectory`·`ExecStart`, `sodam-health.service` 의 heartbeat 경로를 맞춘다.

## 2. 갱신
```bash
sudo -u sodam /opt/sodam/sodam_bot/deploy/update.sh
```
`git pull --ff-only` → `pip install -r` → `tests/run_all.py` **통과해야만** `systemctl restart sodam`. 어느 단계든 실패하면 재시작 안 함.
sodam 사용자가 재시작할 수 있게 sudoers 한 줄: `sodam ALL=(root) NOPASSWD: /usr/bin/systemctl restart sodam, /usr/bin/systemctl is-active --quiet sodam`

## 3. 살아 있는지 확인 (헬스체크)
- 프로세스가 죽으면: `Restart=always` + `RestartSec=5` 로 5초 뒤 자동 재시작.
- 프로세스는 살아 있는데 멈춘(행) 경우: 봇이 30초마다 `data/heartbeat`(유닉스 시각) 를 갱신한다.
  `sodam-health.timer` 가 1분마다 파일 수정 시각을 보고 **180초 넘게 안 바뀌면 `systemctl restart sodam`**.
  수동 확인: `echo $(( $(date +%s) - $(stat -c %Y /opt/sodam/sodam_bot/data/heartbeat) ))초 전`
- 대안: systemd `WatchdogSec=` 는 프로세스가 `sd_notify(WATCHDOG=1)` 를 보내야 해서 추가 코드(systemd 패키지)가 필요 → 지금은 timer 방식.
- 봇 시작·정상 종료 시 오너(관리자 보고 대상)에게 `▶️ 소담 시작 (버전 abc1234)` / `⏹ 소담 정상 종료` 가 온다.
  시작 알림만 오고 종료 알림 없이 다시 시작 알림이 오면 = 비정상 종료(죽음·강제 kill) 후 재시작.
- 예상 못 한 예외는 `[오류] 예외종류: 내용` 으로 오너에게 (같은 종류 10분에 1번, 네트워크 일시 오류 제외).
- TronGrid 조회가 3번 연속 실패하면 `[결제 확인 장애]`, 복구되면 `[결제 확인 복구]` 한 번씩.
