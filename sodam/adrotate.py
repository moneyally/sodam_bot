"""📢 채널 광고 자동순환 (2026-11-24 오너 — 누룽지 '채널 공지 광고들 30분마다 자동으로 홍보').

연결된 채널(channels.room_id)에 올라온 글들을 방에 **간격마다 하나(또는 몇 개)씩 돌아가며** forward 로 자동 재전송한다.
'채널에 올리기만 하면 자동' — 소담이 그 채널 관리자로 **들어온 뒤** 올라온 글만 돈다(텔레그램 한계: 봇은 채널 과거 글 못 읽음).
seq = 지난 글 다음 차례부터 · random = 무작위. del_prev = 새로 올릴 때 지난번 올린 걸 지움(도배 방지).

설정·상태는 전용 표 ad_rotate 한 곳(방 설정 JSON·change_setting 아님 — 틱이 켠 방만 훑게, 캐시 안 깸).
예약공지(announce)는 '직접 쓴 글 반복', 이건 '채널 글을 자동으로 순환' — 역할이 다름. forward 라 원본(버튼·서식·미디어) 그대로.
"""
from __future__ import annotations

import json
import logging
import random
import time

from telegram.error import TelegramError

from . import channel, hooks
from .db import register_schema

log = logging.getLogger(__name__)

DEFAULT_MIN = 30
MIN_MIN, MAX_MIN = 5, 1440      # 간격 5분 ~ 24시간
MAX_PER = 5                     # 한 번에 올릴 광고 최대
ROTATE_MAX = 200               # 한 방이 순환할 채널 글 최대 (오래된 것부터)

_COLS = {"enabled", "interval_min", "per_tick", "order_mode", "del_prev",
         "last_sent", "last_ad", "last_msgs"}

register_schema(
    "CREATE TABLE IF NOT EXISTS ad_rotate ("
    " chat_id INTEGER PRIMARY KEY,"
    " enabled INTEGER NOT NULL DEFAULT 0,"
    " interval_min INTEGER NOT NULL DEFAULT 30,"
    " per_tick INTEGER NOT NULL DEFAULT 1,"
    " order_mode TEXT NOT NULL DEFAULT 'seq',"     # seq | random
    " del_prev INTEGER NOT NULL DEFAULT 0,"
    " last_sent INTEGER NOT NULL DEFAULT 0,"
    " last_ad INTEGER,"                            # 마지막으로 보낸 채널 글 msg_id (seq 다음 차례 기준)
    " last_msgs TEXT)",                            # del_prev 용: 지난번 방에 올린 메시지 id JSON
    migrate={"ad_rotate": "single"})


async def get(svc, chat_id: int):
    return await svc.db._one("SELECT * FROM ad_rotate WHERE chat_id=?", (chat_id,))


async def save(svc, chat_id: int, **fields) -> None:
    """바뀌는 칸만 UPSERT (한 문장 — 틱·패널·도구 공용)."""
    f = {k: v for k, v in fields.items() if k in _COLS}
    cols = ["chat_id", *f]
    ph = ",".join("?" * len(cols))
    upd = ",".join(f"{k}=excluded.{k}" for k in f) or "chat_id=excluded.chat_id"
    await svc.db._write(
        f"INSERT INTO ad_rotate({','.join(cols)}) VALUES({ph}) ON CONFLICT(chat_id) DO UPDATE SET {upd}",
        (chat_id, *f.values()))


async def enabled_rooms(svc):
    return await svc.db._all("SELECT * FROM ad_rotate WHERE enabled=1")


async def linked_channel(svc, room_id: int):
    """이 방에 연결된 채널 (channels.room_id) 또는 None."""
    return await svc.db._one("SELECT * FROM channels WHERE room_id=?", (room_id,))


async def ad_ids(svc, channel_chat_id: int, limit: int = ROTATE_MAX) -> list[int]:
    """순환할 채널 글 msg_id 목록 (올라온 순서 = msg_id 오름차순). 글자 없는 사진 광고도 포함(forward 는 msg_id 면 됨)."""
    rows = await svc.db._all(
        "SELECT msg_id FROM channel_msgs WHERE chat_id=? ORDER BY msg_id ASC LIMIT ?",
        (channel_chat_id, max(1, limit)))
    return [r["msg_id"] for r in rows]


def pick(ads: list[int], last_ad, per: int, order: str) -> list[int]:
    """다음에 보낼 광고 msg_id 들 (순수 함수).
    seq = 지난 광고 다음 차례부터 per 개(끝나면 처음으로 순환) · random = 지난 것 빼고 무작위 per 개."""
    if not ads:
        return []
    per = max(1, min(int(per or 1), len(ads)))
    if order == "random":
        pool = [a for a in ads if a != last_ad] or list(ads)
        return random.sample(pool, min(per, len(pool)))
    try:
        i = ads.index(last_ad) + 1
    except ValueError:
        i = 0
    return [ads[(i + k) % len(ads)] for k in range(per)]


def _ids(js) -> list[int]:
    try:
        v = json.loads(js or "[]")
        return [int(x) for x in v] if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


async def tick(svc, bot) -> None:
    """30초 틱: 켜진 방 중 간격이 찬 방에 다음 광고(들)를 forward. 한 방이 터져도 나머지는 계속."""
    now = int(time.time())
    for r in await enabled_rooms(svc):
        try:
            room = r["chat_id"]
            if now - (r["last_sent"] or 0) < max(1, r["interval_min"]) * 60:
                continue                                  # 아직 간격 안 참
            ch = await linked_channel(svc, room)
            if not ch or not await channel.covered(svc, ch):
                continue                                  # 연결 채널 없음·이용 기간 아님 → 건너뜀(끄진 않음)
            ids = await ad_ids(svc, ch["chat_id"])
            if not ids:
                await save(svc, room, last_sent=now)      # 광고 없으면 간격만큼 쉼 (매 틱 안 훑게)
                continue
            batch = pick(ids, r["last_ad"], r["per_tick"], r["order_mode"])
            if r["del_prev"]:
                for mid in _ids(r["last_msgs"]):
                    try:
                        await bot.delete_message(room, mid)
                    except TelegramError:
                        pass
            posted = []
            for ad in batch:
                try:
                    m = await bot.forward_message(chat_id=room, from_chat_id=ch["chat_id"], message_id=ad)
                    posted.append(m.message_id)
                except TelegramError as e:
                    log.warning("ad_rotate forward %s<-%s/%s: %s", room, ch["chat_id"], ad, e)
            await save(svc, room, last_sent=now,
                       last_ad=(batch[-1] if batch else r["last_ad"]),
                       last_msgs=json.dumps(posted))
        except Exception:
            log.exception("ad_rotate tick failed in %s", r["chat_id"])


hooks.add_tick_hook(tick)
