"""📤 전달 글 그대로 올리기 — python tests/run_all.py repost

완성된 글(사진+글+버튼)을 소담 1:1 에 전달하면 copy_message 로 고른 방에 그대로 재전송.
(2026-11-12 오너 '채널전달하면 인식돼서 그 글 바뀌는 거 없나, 존나 어렵게 돼있노')
"""
import asyncio
import sys
from types import SimpleNamespace

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from fakes import FakeBot, FakeQuery, fake_user, make_db, make_svc, runner

from sodam.panels import repost as R

test, run_all = runner()
CHAT, CHAT2 = -1001000000111, -1001000000222
ADMIN, OWNER, OUTSIDER = 901, 900, 999


def fwd_photo(user, *, forwarded=True, button=True):
    mk = InlineKeyboardMarkup([[InlineKeyboardButton("🔵 사이트", url="https://t.me/x")]]) if button else None
    msg = SimpleNamespace(text=None, caption="광고글", entities=(), caption_entities=(),
                          photo=[SimpleNamespace(file_id="p1")], video=None, animation=None,
                          document=None, sticker=None, audio=None, voice=None,
                          from_user=user, chat_id=user.id, message_id=7, reply_markup=mk)
    msg.forward_origin = SimpleNamespace(type="channel") if forwarded else None
    return msg


def fwd_text(user):
    msg = SimpleNamespace(text="그냥 글", caption=None, entities=(), caption_entities=(),
                          photo=None, video=None, animation=None, document=None, sticker=None,
                          audio=None, voice=None, from_user=user, chat_id=user.id, message_id=8, reply_markup=None)
    msg.forward_origin = SimpleNamespace(type="channel")
    return msg


async def setup():
    db = await make_db()
    svc = await make_svc(db, admins={ADMIN})
    svc.perms.owner_ids = {OWNER}
    await db.ensure_chat(CHAT, "와일드")
    await db.ensure_chat(CHAT2, "둘째방")
    return db, svc, FakeBot()


@test
async def forwarded_media_offers_picker_and_copies_with_buttons():
    db, svc, bot = await setup()
    u = fake_user(ADMIN, "관리")
    assert await R.maybe_offer(svc, bot, fwd_photo(u), u) is True
    assert any("어느 방에 올릴까" in c[2] for c in bot.named("send_message")), bot.named("send_message")
    did = (await db._one("SELECT id FROM repost_drafts", ()))["id"]
    q = FakeQuery(ADMIN, fake_user(ADMIN), f"fwd:go:{did}:{CHAT}")
    await R.on_cb(svc, bot, q, ["go", str(did), str(CHAT)])
    cps = [c for c in bot.calls if c[0] == "copy"]
    assert len(cps) == 1, cps
    assert cps[0][1] == CHAT and cps[0][2] == u.id and cps[0][3] == 7   # 그 방에 · 1:1 원본 · msg_id 7
    assert cps[0][4].get("reply_markup") is not None                    # 버튼 그대로 (뮤테이션: reply_markup=markup 지우면 None)
    assert await db._one("SELECT id FROM repost_drafts", ()) is None    # 올린 뒤 초안 삭제


@test
async def copies_without_buttons_when_none():
    db, svc, bot = await setup()
    u = fake_user(ADMIN, "관리")
    assert await R.maybe_offer(svc, bot, fwd_photo(u, button=False), u) is True
    did = (await db._one("SELECT id FROM repost_drafts", ()))["id"]
    q = FakeQuery(ADMIN, fake_user(ADMIN), f"fwd:go:{did}:{CHAT2}")
    await R.on_cb(svc, bot, q, ["go", str(did), str(CHAT2)])
    cps = [c for c in bot.calls if c[0] == "copy"]
    assert len(cps) == 1 and cps[0][1] == CHAT2 and cps[0][4].get("reply_markup") is None


@test
async def text_only_forward_is_not_repost():
    db, svc, bot = await setup()
    u = fake_user(ADMIN, "관리")
    assert await R.maybe_offer(svc, bot, fwd_text(u), u) is False   # 텍스트만 = 이름조회로 넘김 (가로채지 않음)


@test
async def not_forwarded_or_outsider_declined():
    db, svc, bot = await setup()
    u = fake_user(ADMIN, "관리")
    assert await R.maybe_offer(svc, bot, fwd_photo(u, forwarded=False), u) is False   # 전달 아님
    out = fake_user(OUTSIDER, "외부")
    assert await R.maybe_offer(svc, bot, fwd_photo(out), out) is False   # 관리 방 없음·오너 아님


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
