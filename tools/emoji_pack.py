"""🧩 만든 타일 조각(100×100)을 오너 이름의 프리미엄 이모지 팩으로 올리기 — 서버에서 클로드가 돌림 (sodam/emojilab.py 짝).

    cd /opt/sodam && .venv/bin/python tools/emoji_pack.py --dir data/emoji_lab/out/jehyu \\
        --name jehyu --title "제휴업체" --user <오너ID> [--emoji ⭐] [--add] [--dry]

- 조각 = 폴더의 01.png·02.webp·03.webm … (이름 순서 = 팩 순서 = 이어 칠 순서). png 는 webp 로 바꿔 올림.
- 정지: 100×100 PNG/WEBP · 영상: 100×100 VP9 WebM 3초↓ 64KB↓ (텔레그램 규격, 넘으면 올리기 전에 거절).
- 팩 이름 = <name>_by_<봇아이디>. 이미 있으면 --add 로 뒤에 덧붙임. 끝나면 t.me/addemoji/<팩> 을 찍고 오너 1:1 에도 보냄.
- 봇 토큰은 서버 .env 에서 (화면에 안 찍음). 봇이 돌고 있어도 됨 (getUpdates 를 안 부름).
"""
from __future__ import annotations

import argparse
import asyncio
import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SIZE = 100
VIDEO_MAX = 64 * 1024
FIRST_MAX = 50          # createNewStickerSet 한 번에 넣을 수 있는 수


def tiles(folder: Path) -> list[Path]:
    fs = [p for p in folder.iterdir() if p.suffix.lower() in (".png", ".webp", ".webm") and re.match(r"^\d+", p.name)]
    return sorted(fs, key=lambda p: (int(re.match(r"^\d+", p.name).group()), p.name))


def load(p: Path) -> tuple[bytes, str, str]:
    """(올릴 바이트, 형식 static|video, 파일 이름). 규격이 틀리면 ValueError."""
    if p.suffix.lower() == ".webm":
        data = p.read_bytes()
        if len(data) > VIDEO_MAX:
            raise ValueError(f"{p.name}: {len(data)}B > 64KB")
        return data, "video", p.name
    from PIL import Image
    im = Image.open(p).convert("RGBA")
    if im.size != (SIZE, SIZE):
        raise ValueError(f"{p.name}: {im.size} (100×100 이어야 함)")
    buf = io.BytesIO()
    im.save(buf, "WEBP", lossless=True)
    return buf.getvalue(), "static", p.with_suffix(".webp").name


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--name", required=True, help="영문·숫자·_ (팩 주소 앞부분)")
    ap.add_argument("--title", required=True)
    ap.add_argument("--user", type=int, required=True, help="팩 주인 (오너 텔레그램 ID)")
    ap.add_argument("--emoji", default="⭐", help="조각마다 붙일 보통 이모지 (검색용)")
    ap.add_argument("--add", action="store_true", help="이미 있는 팩 뒤에 덧붙이기")
    ap.add_argument("--dry", action="store_true", help="검사만 하고 안 올림")
    a = ap.parse_args()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,30}", a.name):
        print("name 은 영문으로 시작, 영문·숫자·_ 만")
        return 2
    items = [load(p) for p in tiles(Path(a.dir))]
    if not items:
        print("조각 없음 (01.png 처럼 숫자로 시작하는 파일)")
        return 2
    print(f"조각 {len(items)}개: " + ", ".join(f"{n}({fmt})" for _, fmt, n in items))
    if a.dry:
        return 0

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from telegram import Bot, InputFile, InputSticker
    from sodam.config import load_config
    cfg = load_config()
    async with Bot(cfg.telegram_token) as bot:
        me = await bot.get_me()
        set_name = f"{a.name}_by_{me.username}"
        stickers = [InputSticker(InputFile(data, filename=fn), [a.emoji], fmt) for data, fmt, fn in items]
        if a.add:
            for s in stickers:
                await bot.add_sticker_to_set(user_id=a.user, name=set_name, sticker=s)
        else:
            await bot.create_new_sticker_set(user_id=a.user, name=set_name, title=a.title[:64],
                                             stickers=stickers[:FIRST_MAX], sticker_type="custom_emoji")
            for s in stickers[FIRST_MAX:]:
                await bot.add_sticker_to_set(user_id=a.user, name=set_name, sticker=s)
        st = await bot.get_sticker_set(set_name)
        link = f"https://t.me/addemoji/{set_name}"
        print(f"✅ {link} · {len(st.stickers)}개")
        await bot.send_message(a.user, f"🧩 이모지 팩 '{a.title}' 준비됐어요 ({len(st.stickers)}개)\n{link}\n"
                                       "추가한 뒤 이모지 칸에서 순서대로 이어 치면 한 줄 배너가 돼요.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
