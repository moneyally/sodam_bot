"""🧩 소담이 커스텀 이모지 팩에 바로 넣기 (Bot API createNewStickerSet sticker_type=custom_emoji).

스티커 팩(stickerpack.py)과 같은 꼴이지만 **커스텀 이모지**용:
- 규격: 정적 WEBP **정확히 100×100** (조사 2026-10-28), 셋당 **200개**(일반 스티커 120), 초기 50 + 나머지 addStickerToSet.
- 팩 주인 = 그 사람(user_id), 이름 끝 '_by_<봇>'. 쓰는 사람은 **텔레그램 프리미엄** 필요(없으면 네모로 보임).
- 봇이 메시지에 커스텀 이모지를 쓰려면 **봇 주인 계정 프리미엄**(Bot API 9.4, 2026-02) — 그때만 인라인 시연 가능, 아니면 링크만.
- 스티커 팩과 달리 file_id 가 아니라 **방금 만든 WEBP 바이트**를 바로 올림 (매번 새로 그림).

make_tile_emoji(오너 named 공유 팩)와 달리 이건 **부른 사람 개인 팩**(sodamemoji<ID>)에 쌓는다.
"""
from __future__ import annotations

import logging
import time

from telegram import InputFile, InputSticker
from telegram.error import BadRequest, Forbidden, TelegramError

from .db import register_schema

log = logging.getLogger(__name__)
PER_SET = 200                       # 커스텀 이모지 셋 한도 (일반 스티커는 120)
FIRST_MAX = 50                      # createNewStickerSet 한 번에 올리는 최대

register_schema("""
CREATE TABLE IF NOT EXISTS emoji_packs (
    user_id INTEGER NOT NULL,
    vol     INTEGER NOT NULL,
    name    TEXT NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    ts      INTEGER NOT NULL,
    PRIMARY KEY (user_id, vol)
);
""")


def pack_name(user_id: int, vol: int, bot_username: str) -> str:
    """영문·숫자·밑줄, 글자로 시작, '_by_<봇>' 로 끝. 커스텀 이모지라 'emoji' 를 붙여 스티커 팩과 안 겹치게."""
    return f"sodamemoji{user_id}" + (f"v{vol}" if vol > 1 else "") + f"_by_{bot_username}"


def add_url(name: str) -> str:
    return f"https://t.me/addemoji/{name}"


class NeedsDM(Exception):
    """이 사람이 봇과 1:1 을 안 열어서 팩을 못 만듦 (텔레그램 규칙)."""


def _err(e: Exception) -> str:
    return (getattr(e, "message", "") or str(e)).lower()


_NO_USER = ("peer_id_invalid", "user not found", "user_id_invalid", "bot was blocked", "chat not found", "initiate")


async def packs(db, user_id: int) -> list:
    return await db._all("SELECT * FROM emoji_packs WHERE user_id=? ORDER BY vol", (user_id,))


def _stickers(webps: list[bytes], emoji: str) -> list[InputSticker]:
    return [InputSticker(InputFile(w, filename=f"{i:02d}.webp"), [emoji or "⭐"], "static") for i, w in enumerate(webps)]


async def add_emojis(bot, db, user_id: int, title: str, webps: list[bytes], emoji: str = "⭐") -> tuple[str, int, list[str]]:
    """WEBP 여러 장을 user_id 의 커스텀 이모지 팩에 → (팩 이름, 그 팩 총 개수, 새로 넣은 custom_emoji_id 목록).
    1:1 안 열었으면 NeedsDM. 팩이 꽉 차면 다음 권으로."""
    if not webps:
        raise ValueError("빈 목록")
    username = bot.username or (await bot.get_me()).username
    have = await packs(db, user_id)
    cur = have[-1] if have else None
    vol = cur["vol"] if cur else 1
    exists = bool(cur)
    if cur and cur["count"] >= PER_SET:
        vol, exists = vol + 1, False
    for _ in range(4):                  # 팩 상태가 DB 와 다를 때(지워짐·꽉 참·이미 있음) 고쳐서 다시
        # 남은 자리에 맞춰 이번에 넣을 양 (셋당 200)
        room = PER_SET - (cur["count"] if (exists and cur and cur["vol"] == vol) else 0)
        batch = webps[:max(1, room)]
        name = pack_name(user_id, vol, username)
        sts = _stickers(batch, emoji)
        try:
            if exists:
                for s in sts:
                    await bot.add_sticker_to_set(user_id=user_id, name=name, sticker=s)
            else:
                t = f"{(title or '소담 이모지')[:48]}" + (f" {vol}" if vol > 1 else "")
                await bot.create_new_sticker_set(user_id=user_id, name=name, title=t,
                                                 stickers=sts[:FIRST_MAX], sticker_type="custom_emoji")
                for s in sts[FIRST_MAX:]:
                    await bot.add_sticker_to_set(user_id=user_id, name=name, sticker=s)
        except Forbidden as e:
            raise NeedsDM() from e
        except BadRequest as e:
            m = _err(e)
            if any(w in m for w in _NO_USER):
                raise NeedsDM() from e
            if "occupied" in m and not exists:          # 팩은 있는데 DB 에 없음 (DB 초기화 등) → 덧붙이기로
                exists = True
                continue
            if "stickerset_invalid" in m and exists:    # 사람이 팩을 지움 → 새로
                exists = False
                continue
            if "too_much" in m or "too much" in m or "stickers_too_much" in m:   # 꽉 참 → 다음 권
                vol, exists = vol + 1, False
                continue
            raise
        await _bump(db, user_id, vol, name, len(batch))
        got = await db._one("SELECT count FROM emoji_packs WHERE user_id=? AND vol=?", (user_id, vol))
        ids = await _read_ids(bot, name, len(batch))
        return name, (got["count"] if got else len(batch)), ids
    raise BadRequest("emoji pack state kept changing")


async def _read_ids(bot, name: str, last_n: int) -> list[str]:
    """방금 넣은 이모지의 custom_emoji_id (맨 뒤 last_n개) — 인라인 시연용. 실패하면 빈 목록."""
    try:
        s = await bot.get_sticker_set(name)
        got = [getattr(x, "custom_emoji_id", None) for x in (s.stickers or [])]
        return [g for g in got[-last_n:] if g]
    except TelegramError:
        return []


async def _bump(db, user_id: int, vol: int, name: str, n: int) -> None:
    await db._write("INSERT INTO emoji_packs (user_id, vol, name, count, ts) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(user_id, vol) DO UPDATE SET count=count+?, name=excluded.name, ts=excluded.ts",
                    (user_id, vol, name, n, int(time.time()), n))
