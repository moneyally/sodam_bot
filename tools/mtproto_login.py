"""MTProto 사용자 세션 만들기 (오너가 서버 터미널에서 1번): python tools/mtproto_login.py

채널 조회수(messages.getMessagesViews — 사용자 계정만 가능)를 위한 **전용 텔레그램 계정**으로 로그인해서
data/mtproto_user.session (권한 0600) 에 저장한다. 봇을 재시작하면 🔧 헬퍼 화면에 ② 연결됨으로 보인다.
- 전화번호 → 텔레그램 앱으로 온 코드 → (있으면) 2단계 비밀번호를 **이 터미널에만** 입력.
  코드를 텔레그램 채팅(봇 포함)에 보내지 말 것 — 채팅에 올라온 로그인 코드는 텔레그램이 바로 무효화함.
- 그 계정은 조회수를 볼 채널의 멤버(또는 관리자)여야 함. 끄려면 세션 파일을 지우고 재시작.
- 세션 파일 = 그 계정의 열쇠. 절대 git·채팅에 올리지 말 것 (data/ 는 .gitignore).
"""
import asyncio
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from sodam.mtproto import read_session, write_session  # noqa: E402


async def main() -> int:
    load_dotenv(os.getenv("SODAM_ENV", ".env"))
    api_id, api_hash = os.getenv("MTPROTO_API_ID", "").strip(), os.getenv("MTPROTO_API_HASH", "").strip()
    if not (api_id.isdecimal() and api_hash):
        print(".env 에 MTPROTO_API_ID / MTPROTO_API_HASH 를 먼저 넣어주세요 (my.telegram.org → API development tools).")
        return 1
    path = Path(os.getenv("DB_PATH", "data/sodam.db")).resolve().parent / "mtproto_user.session"
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    client = TelegramClient(StringSession(read_session(path) or None), int(api_id), api_hash,
                            receive_updates=False, device_model="sodam-helper")
    print("⚠️ 봇 전용으로 만든 계정을 쓰세요. 로그인 코드는 이 터미널에만 입력하고, 채팅으로 보내지 마세요.")
    await client.start(phone=lambda: input("전화번호 (+82…): ").strip(),
                       code_callback=lambda: input("텔레그램 앱으로 온 코드: ").strip(),
                       password=lambda: getpass.getpass("2단계 비밀번호 (없으면 엔터): "))
    try:
        me = await client.get_me()
        if me.bot:
            print("봇 계정은 안 돼요 (조회수는 사용자 계정만 가능).")
            return 1
        write_session(path, client.session.save())
        print(f"저장됨: {path} (권한 0600) · 계정 {('@' + me.username) if me.username else me.id}")
        print("봇을 재시작하면 켜져요. 이 계정이 조회수를 볼 채널에 들어가 있어야 해요.")
        return 0
    finally:
        await client.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
