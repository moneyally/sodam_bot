# 서버 배포 (Ubuntu 24.04 + systemd)

처음 옮기는 거라면 → **[migrate_from_container.md](migrate_from_container.md)** (단계별, 약 15분).

## 구성
| 경로 | 내용 | 소유 |
|---|---|---|
| `/opt/sodam` | git 저장소 (코드), `.venv`, `.env`(640 root:sodam), `VERSION` | root (봇은 읽기만) |
| `/opt/sodam/data` | `sodam.db`(WAL)·`heartbeat`·`heartbeat-dealer`·봇 자체 백업 `backups/` | sodam (700) |
| `/opt/sodam/backups` | 매일 04:30 외부 백업 `sodam-*.db.gz`, 14일 | sodam (700) |

| 파일 | 하는 일 |
|---|---|
| `install.sh` | 서버 준비 + 설치/갱신 + `.env`·DB·이사 꾸러미 가져오기 + 유닛 등록·시작 (여러 번 실행해도 안전) |
| `sodam.service` / `sodam-dealer.service` | 메인 봇 / 딜러 봇(`.env.dealer` 있을 때만). `Restart=always`·`RestartSec=5`·SIGTERM 후 60초 정상 종료·샌드박스(`ProtectSystem=strict`, 쓰기는 data 만) |
| `sodam-health.timer` → `healthcheck.sh` | 1분마다: 켜진 지 3분 넘은 봇의 하트비트가 180초 넘게 멈췄으면 그 봇만 재시작 (`tools/supervise.sh` 와 같은 규칙) |
| `sodam-backup.timer` → `backup.sh` | `data/*.db` 온라인 백업(sqlite 백업 API, WAL 안전) + integrity_check + gzip, 14일 지난 것 삭제. rclone 예시 주석 |
| `update.sh` (`sodam-update`) | fetch → 새 커밋을 임시 폴더에서 `tests/run_all.py` → 통과해야 ff 적용·`VERSION`·재시작 → 90초 안에 `시작! (버전 <커밋>` 없으면 이전 커밋으로 되돌림 |
| `sodam-autoupdate.timer` | (선택, `install.sh --auto-update`) 10분마다 `update.sh --quiet` |
| `export_bundle.sh` | 옛 서버에서: 봇 끈 뒤 `.env`+DB 를 암호화 꾸러미로 (`--telegram` 이면 오너 1:1 로 전송) |

## 자주 쓰는 것
```bash
journalctl -u sodam -f            # 로그
systemctl status sodam            # 상태
sodam-update                      # 갱신 (테스트 통과해야 재시작, 실패하면 되돌림)
systemctl start sodam-backup      # 지금 백업
systemctl list-timers 'sodam*'    # 타이머 다음 실행 시각
```

## 주의
- 같은 봇 토큰은 **한 곳에서만** 켠다 (둘이면 `409 Conflict`, DB 도 갈라짐). 옮길 땐 옛 봇 먼저 끄기.
- SQLite 는 네트워크 공유 폴더(NFS·SMB·동기화 폴더)에 두지 않는다 (잠금 깨짐 → DB 손상).
- `.env` 의 `DB_PATH`·`BACKUP_DIR` 는 `data/` 아래 상대 경로여야 함 (서비스는 data 에만 쓸 수 있음). install.sh 가 검사.
- 서버에서 코드를 직접 고치지 않는다 (갱신은 ff-only → 막힘). 고칠 건 GitHub 로.
- 시작·정상 종료 때 오너에게 `▶️ 소담 시작 (버전 …)` / `⏹ 소담 정상 종료` 가 온다. 종료 알림 없이 시작 알림만 또 오면 = 비정상 종료 후 재시작.
