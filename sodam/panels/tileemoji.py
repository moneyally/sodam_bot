"""🧩 소담이 말로 분할 커스텀 이모지 자동 제작 (오너 전용, 2026-10-22 '챗지피티처럼·자율모드').

'소담아 로얄클럽 금색으로 이모지 만들어' → banner + stylefx 로 글자 한 개씩 타일 렌더 → 봇이 오너 팩에 올림 →
t.me/addemoji 링크. 하드코딩 아님: text·style·frame·font 전부 값. IM 없으면 PIL 폴백(금색은 그대로).
"""
from __future__ import annotations

import asyncio
import io
import re
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from .. import emojipack
from ..permissions import Role
from ..settings import register_setting
from ..stickerforge import banner, emoji as emojifx, layout, stylefx
from ..tools import Tool, ToolCtx, register_tool

STYLES = ("gold", "chrome", "fire", "neon", "glass")
register_setting("emoji_daily", 5, "하루 커스텀 이모지 만들기(개)", range_=(0, 30))   # 0 = 끔. 글자는 비용 거의 0, 사진은 하루 한도만
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
    r = await asyncio.to_thread(banner.build_tiles, spec)   # ImageMagick·PIL 렌더가 무거워 메인 루프를 막지 않게
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


# ── 일반 make_emoji (멤버도·방/1:1·글자 또는 사진) ─────────────────────────────
async def _demo_inline(bot, chat_id: int, ids: list) -> bool:
    """봇 주인 프리미엄이면 방에 실제 커스텀 이모지를 인라인으로 시연 (Bot API 9.4). 안 되면 조용히 False."""
    if chat_id >= 0 or not ids:                 # 채널·1:1 은 인라인 커스텀 이모지 불가 (9.4: 그룹·슈퍼그룹만)
        return False
    tag = "".join(f'<tg-emoji emoji-id="{i}">⭐</tg-emoji>' for i in ids[:5])
    try:
        await bot.send_message(chat_id, f"{tag} ← 이렇게 채팅 옆에 붙여 써요 (쓰는 사람은 프리미엄 필요)", parse_mode="HTML")
        return True
    except TelegramError:
        return False                            # 봇 주인이 프리미엄 아님 → 링크만


async def t_make_emoji(ctx: ToolCtx, a: dict) -> str:
    uid, db, chat_id = ctx.caller.id, ctx.svc.db, ctx.chat_id
    source = str(a.get("source") or ("photo" if a.get("photo_of") else "text")).strip().lower()
    style = a.get("style") if a.get("style") in STYLES else "gold"
    frame = a.get("frame") if a.get("frame") in FRAMES else ("none" if source == "photo" else "gold")
    emoji = (str(a.get("emoji") or "⭐").strip() or "⭐")[:8]

    # 하루 한도 (글자는 거의 공짜지만 팩 도배 방지, 사진은 특히) — 오너는 면제
    cap = int(ctx.settings.get("emoji_daily", 5) or 0)
    if cap <= 0 and ctx.role < Role.OWNER:
        return "이 방은 커스텀 이모지 만들기가 꺼져 있어요 (emoji_daily 0). 방 관리자·오너만 켤 수 있어요."
    day = datetime.now(ctx.svc.cfg.tz).strftime("%Y-%m-%d")
    if ctx.role < Role.OWNER and await db.counter(day, 0, f"emo:{uid}") >= cap:
        return f"커스텀 이모지는 한 사람 하루 {cap}개까지예요. 내일 다시 된다고 안내."

    # 렌더: 사진 모드 = 배경 빼고 100×100 한 장 · 글자 모드 = 글자당 타일
    try:
        if source == "photo":
            from .avatar import source_photo
            src, err = await source_photo(ctx, a)
            if not src:
                return err or "원본 사진이 없어요."
            img = await asyncio.to_thread(emojifx.image_to_emoji, src)
            webps, preview, count = [emojifx.to_webp(img)], img, 1
            title = str(a.get("title") or "소담 이모지")
        else:
            text = str(a.get("text", "")).strip()
            if not text:
                return "만들 글자(text)나 사진(source='photo')이 필요해요. 예: text='제휴업체' / '🔥HOT' / '?'"
            font = a.get("font") if a.get("font") in {**layout.FONTS, **layout.DISPLAY_FONTS} else None
            spec = build_spec(text, style, frame, font)
            if not spec["layers"]:
                return "만들 글자가 없어요."
            r = await asyncio.to_thread(banner.build_tiles, spec)   # IM·PIL 렌더가 무거워 메인 루프 안 막게
            webps = [_webp(t) for t in r["tiles"]]
            preview, count = r["preview"], r["count"]
            title = str(a.get("title") or text)
    except Exception as e:                       # noqa: BLE001 — 렌더 실패는 사용자에게 한 줄로
        return f"이모지 그리기 실패: {str(e)[:120]}. 글자를 줄이거나 다른 사진으로 다시."

    # 미리보기 먼저 (방에 보이게 — 작으면 커 보이게)
    try:
        pv = preview.convert("RGBA")
        if max(pv.size) < 160:
            pv = pv.resize((pv.width * 3, pv.height * 3))
        await ctx.bot.send_photo(chat_id, photo=_png(pv),
                                 caption=f"🧩 커스텀 이모지 {count}개 만드는 중… ({'사진' if source == 'photo' else style})")
    except TelegramError:
        pass

    # 커스텀 이모지 팩에 올리기
    try:
        name, total, ids = await emojipack.add_emojis(ctx.bot, db, uid, title, webps, emoji)
    except emojipack.NeedsDM:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ 1:1 열고 다시 만들기", url=f"https://t.me/{ctx.bot.username}")]])
        try:
            await ctx.bot.send_message(chat_id, "먼저 소담 1:1 을 한 번 열어야 이모지 팩을 만들 수 있어요 (텔레그램 규칙). "
                                                "아래 버튼으로 열고 다시 '이모지 만들어줘' 해 주세요.", reply_markup=kb)
        except TelegramError:
            pass
        ctx.quiet = True
        return "이 사람이 소담 1:1 을 안 열어 팩을 못 만들었어요 (1:1 열기 버튼을 방에 보냈음). 짧게 안내만."
    except Exception as e:                       # noqa: BLE001
        return f"이모지 팩에 못 올렸어요: {str(e)[:140]}. 다른 name·사진으로 다시 하자고 안내."

    await db.bump(day, 0, f"emo:{uid}")
    link = emojipack.add_url(name)
    try:
        await ctx.bot.send_message(chat_id, f"✅ 커스텀 이모지 {count}개 준비됐어요 (내 팩 {total}개)\n{link}\n"
                                   "추가하면 채팅 글자 옆에 붙여 쓸 수 있어요 (쓰는 사람은 텔레그램 프리미엄 필요).")
    except TelegramError:
        pass
    await _demo_inline(ctx.bot, chat_id, ids)
    ctx.quiet = True
    return (f"커스텀 이모지 {count}개를 만들어 팩 링크를 방에 보냈어요: {link}. "
            + ("글자당 한 칸씩이라 이모지 칸에서 순서대로 이어 치면 한 줄이 돼요. " if source == "text" and count > 1 else "")
            + "한마디만 짧게.")


_EDESC = ("커스텀 이모지(채팅 글자 옆에 붙여 쓰는 100×100 프리미엄 이모지)를 만들어 팩 링크를 방에 보낸다 — "
          "'제휴업체 이모지 만들어'·'🔥HOT 금색 이모지'·'내 프사로 이모지'. source='text'(기본)면 text 를 한 글자씩 타일로 "
          "style(gold 금속·chrome·fire·neon·glass)·frame(gold 금테·silver·neon·none)로 꾸밈. source='photo'면 "
          "photo_of(이 방 누구 프사)·붙은·답장한 사진의 배경을 빼서 한 장. 누구나 쓸 수 있고 하루 한도가 있다(emoji_daily). "
          "쓰는 사람은 텔레그램 프리미엄 필요. 여러 글자는 이모지 칸에서 이어 치면 한 줄 배너가 됨. 스티커(512)와 다름(이모지는 글 속에 붙음).")
EMOJI_TOOL = Tool("make_emoji", _EDESC,
                  {"text": {"type": "string", "description": "글자 모드: 타일로 만들 글자/단어 (한 글자당 한 타일, 최대 24자). 기호·?·!·HOT 도 됨"},
                   "source": {"type": "string", "enum": ["text", "photo"], "description": "text(기본, 글자 꾸미기) 또는 photo(사진 배경 빼서 이모지)"},
                   "photo_of": {"type": "string", "description": "photo 모드: 이 방 멤버 프사로 — 이름·@아이디·ID (없으면 붙은·답장한 사진 > 요청자 프사)"},
                   "style": {"type": "string", "enum": list(STYLES), "description": "글자 스타일 (기본 gold)"},
                   "frame": {"type": "string", "enum": list(FRAMES), "description": "타일 테두리 배지 (기본 글자=gold·사진=none)"},
                   "font": {"type": "string", "description": "글꼴 (안 주면 한글=굵게·영문=스타일에 맞춰 자동)"},
                   "emoji": {"type": "string", "description": "이 이모지의 뜻(검색용 기준 이모지 1개, 기본 ⭐)"},
                   "title": {"type": "string", "description": "팩 제목 (안 주면 글자/‘소담 이모지’)"}},
                  [], t_make_emoji, Role.MEMBER)   # setting 으로 숨기지 않음(캐시 일관성) — 0 한도는 핸들러가 안내
register_tool(EMOJI_TOOL, read_only=False)


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
