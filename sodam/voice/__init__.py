"""📞 소담 음성채팅 — 그룹 음성채팅에서 소담이 실시간으로 말한다 (설계·운영: CLAUDE.md '📞 음성채팅', .claude/skills/telegram-voice-assistant/SKILL.md, GPT-Live 전환 설계 docs/VOICE_LIVE.md).

텔레그램은 봇 계정에 통화(phone.*)를 주지 않는다 → 음악봇들(오픈소스 YukkiMusicBot 등)처럼 **어시스턴트 사람 계정** 하나가
음성채팅에 들어가고, 소담 봇(관리자)이 그 계정을 1회용 초대링크로 방에 넣고 '음성채팅 관리' 권한을 준다. 구조만 참고, 코드는 새로.

- audio.py   48 kHz(텔레그램) ↔ 24 kHz(OpenAI Realtime) PCM16 변환·10 ms 조각
- bridge.py  OpenAI Realtime(WebSocket) ↔ 통화: 듣기·말하기·말 끊기(truncate)·시간/침묵 한도
- store.py   봇 ↔ 통화 담당 프로세스가 DB 로 주고받는 일(voice_jobs)·통화 기록(voice_calls)·로그인 상태
- worker.py  통화 담당 프로세스 (python -m sodam.voice.worker, systemd sodam-voice) — py-tgcalls·ntgcalls 는 여기서만 import
             (ntgcalls 네이티브 크래시가 소담 본체를 죽이지 않게 따로)
- panels/voice.py  봇 쪽: AI 도구 voice_call · 어시스턴트 자동 입장 · 오너 🎙 로그인 화면 · 끝난 통화 안내
"""
