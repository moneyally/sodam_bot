# 소담 이사: 클라우드 컨테이너 → VPS (약 15분)

지금 봇은 Claude 클라우드 컨테이너 안에서 돌아서, 컨테이너가 회수되면 최대 1시간 꺼져 있어요.
VPS(내 서버)로 옮기면 24시간 계속 돌고, 죽거나 멈추면 5초~3분 안에 알아서 다시 켜져요.

## 0. 먼저 알아둘 것: 봇은 딱 한 곳에서만
텔레그램은 **한 봇 토큰에 한 곳만** 메시지를 받게 해요(getUpdates). 옛 봇과 새 봇이 동시에 켜지면
`409 Conflict: terminated by other getUpdates request` 오류가 나면서 둘이 서로 끊고, 메시지가 이쪽저쪽으로 흩어져요.
게다가 DB 가 두 개로 갈라져서 포인트·구독·설정이 서로 달라져요.
→ 순서가 중요해요: **① 새 서버 준비 → ② 옛 봇 끄기 → ③ 데이터 옮기기 → ④ 새 봇 켜기.**
봇이 꺼진 몇 분 동안 온 메시지는 텔레그램이 보관했다가 새 봇이 켜지면 전달해요(놓치지 않음).

## 1. VPS 사기 (5분)
- 아무 회사나 OK (Vultr·DigitalOcean·AWS Lightsail·Linode 등). 지역은 서울 또는 도쿄.
- **운영체제: Ubuntu 24.04 LTS**
- 사양: **최소 1 vCPU · 1GB RAM · 20GB SSD** (월 $5~6). 여유 있게는 2GB RAM.
  실측: 봇 메모리 약 90MB, 데이터 6MB. 1GB 서버면 설치 스크립트가 스왑 2GB 를 자동으로 만들어요.
- 만들 때 **root 비밀번호**를 정하거나(또는 회사가 메일로 줌) 적어 두세요. 만들고 나면 **IP 주소**(예: `1.2.3.4`)가 보여요.

## 2. 윈도우에서 서버 접속 (PC방 OK, 설치 필요 없음)
1. 시작 버튼 → `PowerShell` 입력 → 실행
2. 입력 (1.2.3.4 대신 내 IP):
   ```powershell
   ssh root@1.2.3.4
   ```
3. 처음엔 `Are you sure you want to continue connecting (yes/no)?` → `yes` 입력
4. 비밀번호: 복사 후 **PowerShell 창에 마우스 오른쪽 클릭**하면 붙여넣어져요. 화면엔 아무것도 안 보이는 게 정상 → Enter
5. `root@...:~#` 이 보이면 접속 성공. (나올 땐 `exit`)
   - AWS Lightsail 처럼 `ubuntu@1.2.3.4` 로 접속하는 곳은 접속 뒤 `sudo -i` 를 한 번 입력해서 root 로 바꾸세요.

## 3. 새 서버 준비 (봇은 아직 안 켬, 3~5분)
서버 창에 그대로 붙여넣기:
```bash
curl -fsSL https://raw.githubusercontent.com/moneyally/sodam_bot/main/deploy/install.sh -o install.sh
bash install.sh
```
시간대(한국), 패키지, 방화벽(ssh 만 허용), 보안 자동 업데이트, 스왑, 로그 크기 제한, 봇 코드(`/opt/sodam`)를 설치해요.
마지막에 `준비 끝 (봇은 아직 안 켬)` 이 나오면 OK. 이 동안 옛 봇은 계속 돌아도 괜찮아요.

## 4. 옛 봇 끄고 데이터 꾸러미 만들기 (Claude 에게 부탁)
클라우드 컨테이너는 밖에서 접속이 안 돼서(컨테이너 → 서버 ssh 도 막힘) Claude 가 **봇을 통해 텔레그램으로** 보내줘요.
Claude 세션(소담 저장소)에 이렇게 보내세요:

> 소담 VPS 로 이사해. 순서대로: ① Routine '소담 봇 생존 확인' 꺼 ② ~/.claude/settings.json 세션 시작 훅에서 supervise.sh 켜는 부분 빼
> ③ 감시(supervise.sh)는 PID 로 끄고 봇도 꺼, `pgrep -af "m sodam$"` 비었는지 확인
> ④ `deploy/export_bundle.sh /home/user/sodam_bot --telegram` 실행하고 비밀번호 알려줘

①②를 먼저 하는 이유: 안 끄면 1시간 뒤 Routine 이 세션을 깨워 옛 봇이 다시 켜져요 → 409 Conflict.
끝나면 **텔레그램 소담 봇 1:1 에 `sodam-migrate-날짜.tar.gz.enc` 파일**이 오고, Claude 가 **비밀번호**를 알려줘요.
(파일은 암호화돼 있어서 비밀번호 없이는 못 열어요. 안에 .env(봇 토큰·API 키)와 DB 가 들어 있어요.)

## 5. 꾸러미를 서버에 올리고 봇 켜기 (3분)
1. 텔레그램(데스크톱 또는 web.telegram.org)에서 파일 **다운로드**
2. **새 PowerShell 창**(서버 접속 창 말고)에서 — 파일 이름은 실제 이름으로, `Tab` 키로 자동완성돼요:
   ```powershell
   scp "$HOME\Downloads\sodam-migrate-20260927-1612.tar.gz.enc" root@1.2.3.4:/root/
   ```
   (텔레그램 데스크톱은 보통 `$HOME\Downloads\Telegram Desktop\` 폴더에 저장해요)
3. 서버 창에서:
   ```bash
   bash /opt/sodam/deploy/install.sh --bundle /root/sodam-migrate-20260927-1612.tar.gz.enc
   ```
   - 비밀번호를 물으면 Claude 가 준 비밀번호를 **오른쪽 클릭으로 붙여넣고** Enter (안 보이는 게 정상)
   - `옛 봇(클라우드 컨테이너)을 끄셨나요? [y/N]` → 4단계 끝났으면 `y`
4. 초록색 `✅ 소담 (@sodam_ai_bot) 시작! (버전 abc1234) 모델=…` 이 나오면 성공.

## 6. 확인
- 텔레그램에 오너 알림 `▶️ 소담 시작 (버전 abc1234)` 이 와요.
- 봇 1:1 에서 `/start` → 메뉴가 뜨면 OK. 방에서 `소담아 안녕` 도 해보기.
- 충돌 없는지: `journalctl -u sodam --since "10 min ago" | grep -i conflict` → **아무것도 안 나와야** 정상.
  나오면 옛 봇이 아직 어딘가 켜져 있는 거예요 → Claude 에게 4단계 ③ 다시.
- 뒷정리: 텔레그램 1:1 의 꾸러미 메시지 **삭제**, 서버에서 `rm /root/sodam-migrate-*.enc /root/install.sh`.
- Claude 앱 → Routines 에서 '소담 봇 생존 확인'이 꺼졌는지 한 번 더 확인. 옛 클라우드 세션은 보관(archive)해도 돼요.

## 매일 쓰는 명령 (서버 창에서)
| 하고 싶은 것 | 명령 |
|---|---|
| 실시간 로그 보기 (나가기 `Ctrl+C`) | `journalctl -u sodam -f` |
| 최근 로그 100줄 | `journalctl -u sodam -n 100` |
| 켜져 있나? | `systemctl status sodam` |
| 재시작 / 끄기 / 켜기 | `systemctl restart sodam` / `systemctl stop sodam` / `systemctl start sodam` |
| 새 코드로 갱신 | `sodam-update` |
| 지금 백업 | `systemctl start sodam-backup && ls -lh /opt/sodam/backups` |
| 하트비트(몇 초 전?) | `echo $(( $(date +%s) - $(stat -c %Y /opt/sodam/data/heartbeat) ))초 전` |
| .env 고치기 | `nano /opt/sodam/.env` → `Ctrl+O` Enter `Ctrl+X` → `systemctl restart sodam` |

자동으로 되는 것: 죽으면 5초 뒤 재시작 · 멈추면(하트비트 3분) 재시작(`sodam-health.timer`) · 서버 재부팅해도 자동 시작 ·
매일 04:30 DB 백업 14일 보관(`sodam-backup.timer`) + 봇 자체 백업 05:00(`data/backups`) · 보안 업데이트.

## 코드 갱신 (Claude 가 고친 걸 서버에 반영)
Claude 클라우드 세션은 서버에 직접 접속할 수 없어요. 그래서: **Claude 가 GitHub `main` 에 푸시 → 서버가 가져감.**
- 손으로: PowerShell 에서 `ssh root@1.2.3.4 sodam-update`
- 자동으로(추천): 서버에서 한 번만 `bash /opt/sodam/deploy/install.sh --auto-update` → 10분마다 새 커밋 확인.
  확인 기록: `journalctl -u sodam-autoupdate -n 50`

`sodam-update` 가 하는 일: 새 코드를 임시 폴더에서 **전체 오프라인 테스트** → 통과해야만 적용·재시작 →
로그에 `시작! (버전 새커밋` 이 90초 안에 안 뜨면 **이전 커밋으로 자동 되돌림**. 테스트 실패면 봇은 옛 코드로 계속 돌아요.

## 백업·복원
- 백업 파일: `/opt/sodam/backups/sodam-YYYYmmdd-HHMMSS.db.gz` (매일, 14일치)
- 복원 (예: 어제 걸로):
  ```bash
  systemctl stop sodam sodam-dealer
  sqlite3 /opt/sodam/data/sodam.db ".backup /opt/sodam/backups/before-restore.db"   # 지금 것도 혹시 몰라 보관
  gunzip -c /opt/sodam/backups/sodam-20260927-043012.db.gz > /opt/sodam/data/sodam.db
  rm -f /opt/sodam/data/sodam.db-wal /opt/sodam/data/sodam.db-shm
  chown sodam:sodam /opt/sodam/data/sodam.db
  systemctl start sodam
  ```
- 서버 밖(구글드라이브 등)에도 두려면 `deploy/backup.sh` 맨 아래 rclone 예시 참고.

## 딜러 봇 (따로 쓰는 경우만)
컨테이너에 `.env.dealer` 가 있었으면 꾸러미에 같이 들어가서 `sodam-dealer` 서비스로 자동으로 켜져요.
로그 `journalctl -u sodam-dealer -f`. 나중에 추가하려면 `.env.dealer` 를 올리고
`bash /opt/sodam/deploy/install.sh --env-dealer /root/.env.dealer`.

## 막히면
- `ssh: connect ... timed out` → IP 확인, VPS 회사 방화벽(보안 그룹)에서 22번 포트 허용.
- 설치 중 빨간 `!!` 줄 → 그 화면을 복사해서 Claude 에게 보여주세요.
- 비밀번호 틀림 → 꾸러미 비밀번호는 Claude 가 준 것 (서버 root 비밀번호 아님).
