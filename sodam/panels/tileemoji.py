"""🧩 소담이 말로 분할 커스텀 이모지 자동 제작 (오너 전용, 2026-10-22 '챗지피티처럼·자율모드').

'소담아 로얄클럽 금색으로 이모지 만들어' → banner + stylefx 로 글자 한 개씩 타일 렌더 → 봇이 오너 팩에 올림 →
t.me/addemoji 링크. 하드코딩 아님: text·style·frame·font 전부 값. IM 없으면 PIL 폴백(금색은 그대로).
"""
from __future__ import annotations

import io
import re

from ..permissions import Role
from ..stickerforge import banner, layout, stylefx
from ..tools import Tool, ToolCtx, register_tool

STYLES = ("gold", "chrome", "fire", "neon", "glass")
MAX_TILES = 24
FIRST_MAX = 50                      # createNewStickerSet 한 번에 올리는 최대

# 프레임(타일 배지) 테마 — ChatGPT 로얄클럽 역설계 토큰
FRAMES = {
    "gold": {"fill": [17, 18, 20], "border": 0.05, "border_color": [172, 143, 47], "radius": 0.11, "inset": [213, 173, 19], "gap": 0.08},
    "silver": {"fill": [18, 20, 24], "border": 0.05, "border_color": [150, 165, 190], "radius": 0.11, "inset": [210, 225, 245], "gap": 0.08},
    "neon": {"fill": [10, 12, 22], "border": 0.045, "border_color": [90, 220, 255], "radius": 0.14, "inset": [150, 240, 255], "gap": 0.08},
    "none": None,
}
_HANGUL = re.compile(r"[가-힣㄰-㆏]")


def _font_for(ch: str, style: str) -> str:
    """글자에 맞는 글꼴: 한글=굵은 제목체, 영문=스타일에 맞춰(금=로열세리프·나머지=블록)."""
    if _HANGUL.search(ch):
        return "bold"
    if style == "gold":
        return "royal"                  # 영문 럭셔리 = Cinzel 로열 세리프
    return "heavy"                       # 네온·불 등 임팩트 = Anton


def build_spec(text: str, style: str = "gold", frame: str = "gold", font: str | None = None) -> dict:
    """글자들을 한 칸당 하나씩 타일로. cols≤10, 넘으면 다음 줄. 전부 값(요청마다 달라짐)."""
    chars = [c for c in text if not c.isspace()][:MAX_TILES]
    if not chars:
        return {"cols": 1, "rows": 1, "bg": "transparent", "layers": []}
    cols = min(10, len(chars))
    rows = (len(chars) + cols - 1) // cols
    h = 0.62 if rows == 1 else 0.6 / rows * 1.6
    layers = []
    for i, ch in enumerate(chars):
        at = [((i % cols) + 0.5) / cols, ((i // cols) + 0.5) / rows]
        layers.append({"type": "text", "text": ch, "style": style, "font": font or _font_for(ch, style),
                       "at": at, "height": h})
    return {"cols": cols, "rows": rows, "bg": "transparent", "frames": FRAMES.get(frame, FRAMES["gold"]), "layers": layers}


def _webp(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "WEBP", lossless=True)
    return buf.getvalue()


async def create_pack(bot, user_id: int, name: str, title: str, tiles: list, emoji: str = "⭐") -> str:
    """오너 계정에 커스텀 이모지 팩 생성 → t.me/addemoji 링크. (emoji_pack.py 와 같은 길, 봇 안에서 바로.)"""
    from telegram import InputFile, InputSticker
    me = await bot.get_me()
    set_name = f"{name}_by_{me.username}"
    stickers = [InputSticker(InputFile(_webp(t), filename=f"{i:02d}.webp"), [emoji], "static") for i, t in enumerate(tiles)]
    await bot.create_new_sticker_set(user_id=user_id, name=set_name, title=title[:64],
                                     stickers=stickers[:FIRST_MAX], sticker_type="custom_emoji")
    for s in stickers[FIRST_MAX:]:
        await bot.add_sticker_to_set(user_id=user_id, name=set_name, sticker=s)
    return f"https://t.me/addemoji/{set_name}"


async def t_make_tile_emoji(ctx: ToolCtx, a: dict) -> str:
    text = str(a.get("text", "")).strip()
    if not text:
        return "만들 글자(text)가 필요해요. 예: text='로얄클럽'"
    style = a.get("style") if a.get("style") in STYLES else "gold"
    frame = a.get("frame") if a.get("frame") in FRAMES else "gold"
    font = a.get("font") if a.get("font") in {**layout.FONTS, **layout.DISPLAY_FONTS} else None
    name = str(a.get("name", "")).strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,24}", name):
        return "name 은 영문으로 시작·영문/숫자/_ 만 (팩 주소). 예: name='royalclub'"
    spec = build_spec(text, style, frame, font)
    if not spec["layers"]:
        return "만들 글자가 없어요."
    r = banner.build_tiles(spec)
    # 미리보기 먼저
    try:
        await ctx.bot.send_photo(ctx.caller.id, photo=_png(r["preview"]),
                                 caption=f"🧩 미리보기 — '{text}' {style} ({r['count']}개 타일). 팩 올리는 중…")
    except Exception:
        pass
    try:
        link = await create_pack(ctx.bot, ctx.caller.id, name, str(a.get("title") or text), r["tiles"])
    except Exception as e:
        msg = str(e)
        if "occupied" in msg or "STICKERSET" in msg or "already" in msg.lower():
            return f"'{name}' 팩 이름이 이미 있어요. 다른 name 으로 다시 (또는 그 팩에 덧붙이기는 다음 작업)."
        if "PEER_ID_INVALID" in msg or "initiate" in msg.lower():
            return "먼저 소담 1:1 을 한 번 열어야 팩을 만들 수 있어요 (오너가 /start)."
        return f"팩 만들기 실패: {msg[:120]}"
    await ctx.bot.send_message(ctx.caller.id, f"✅ '{text}' 이모지 팩 준비됐어요 ({r['count']}개)\n{link}\n"
                               "추가한 뒤 이모지 칸에서 순서대로 이어 치면 한 줄 배너가 돼요. (쓰는 사람은 텔레그램 프리미엄 필요)")
    ctx.quiet = True
    return f"분할 이모지 팩 {r['count']}개를 만들어 오너 1:1 에 링크를 보냈어요: {link}"


def _png(img) -> bytes:
    buf = io.BytesIO()
    img.convert("RGBA").save(buf, "PNG")
    return buf.getvalue()


_DESC = ("[오너] 분할 커스텀 이모지 팩을 만든다 — '로얄클럽 금색 이모지 만들어'. text 를 한 글자씩 타일로 쪼개 "
         "style(gold 금속·chrome 크롬·fire 불·neon 네온·glass 유리)·frame(gold 금테·silver·neon·none)로 꾸며 "
         "오너 팩에 올리고 t.me/addemoji 링크를 1:1 로 보냄. name 은 팩 주소(영문). 전부 값이라 요청마다 달라짐.")
TOOL = Tool("make_tile_emoji", _DESC,
            {"text": {"type": "string", "description": "타일로 만들 글자/단어 (한 글자당 한 타일, 최대 24자)"},
             "name": {"type": "string", "description": "팩 주소 앞부분 (영문 시작·영문/숫자/_). 예: royalclub"},
             "style": {"type": "string", "enum": list(STYLES), "description": "글자 스타일 (기본 gold)"},
             "frame": {"type": "string", "enum": list(FRAMES), "description": "타일 테두리 배지 (기본 gold)"},
             "font": {"type": "string", "description": "글꼴 (안 주면 한글=굵게·영문=스타일에 맞춰 자동)"},
             "title": {"type": "string", "description": "팩 제목 (안 주면 text)"}},
            ["text", "name"], t_make_tile_emoji, Role.OWNER, where="owner_dm")
register_tool(TOOL, read_only=False)
