"""📓 소담이 일기 화면 (오너 1:1 메인). 쓰기·올리기·자동 시각은 sodam/diary.py.

m:dy                 설정 화면 (채널 · 켜기 방식 · 시각 · 번호 · 초안 있음)
m:dyc:<채널>         올릴 채널 고르기 (등록된 채널 중 글쓰기 권한 있는 것)
m:dym:<off|auto|preview> · m:dyt:<시각 2330 꼴>
m:dyw                지금 써보기 → 1:1 에 초안 + [올리기][다시 쓰기][안 올림] (AI 1번)
m:dyg / m:dyr / m:dyx  초안 올리기 / 다시 쓰기 / 버리기
오너만 (라우트 OWNER + 누를 때마다 다시 확인).
"""
from __future__ import annotations

from datetime import datetime
from functools import wraps

from telegram.error import TelegramError

from .. import diary, menu
from ..menu import OWNER, B, PanelCtx, Route, Screen
from ..security import NO_PREVIEW
from ..util import esc, to_int

NOT_OWNER = Screen(None, toast="봇 오너만 쓸 수 있어요.", alert=True)


def _owner_only(fn):
    @wraps(fn)
    async def wrapped(c: PanelCtx) -> Screen:
        if c.uid not in await c.svc.perms.owners():
            return NOT_OWNER
        return await fn(c)
    return wrapped


def draft_kb():
    return menu._kb([[B("✅ 채널에 올리기", "m:dyg"), B("🔄 다시 쓰기", "m:dyr")], [B("🙅 오늘은 안 올림", "m:dyx")]])


async def _channels(db) -> list:
    return await db._all("SELECT chat_id, title FROM channels WHERE active=1 AND can_post=1 ORDER BY title")


@_owner_only
async def s_diary(c: PanelCtx) -> Screen:
    st = await diary.settings(c.svc.db)
    chans = await _channels(c.svc.db)
    cur = next((ch for ch in chans if ch["chat_id"] == st["diary_channel"]), None)
    lines = ["📓 <b>소담이 일기</b>",
             "매일 밤 정한 시각에 소담이가 오늘 있었던 일을 짧은 일기로 써서 채널에 올려요.",
             "숫자만 보고 써요 — 방 이름·사람 이름·대화 내용은 안 들어가요 (링크·주소도 코드가 한 번 더 지움). AI 비용 하루 몇 원.", "",
             f"채널: <b>{esc(cur['title']) if cur else '안 정함'}</b>",
             f"방식: <b>{diary.MODES.get(st['diary_mode'], st['diary_mode'])}</b> · 시각: <b>{st['diary_time']}</b> · "
             f"다음 번호: #{int(st['diary_no'] or 1) + 1}"]
    if st["diary_draft"]:
        lines.append("✏️ 안 올린 초안이 있어요.")
    if not chans:
        lines.append("\n⚠️ 글을 쓸 수 있는 채널이 없어요 — 채널에 소담을 관리자로 넣고 '메시지 보내기' 권한을 켜 주세요.")
    rows = [[B(("✅ " if ch["chat_id"] == st["diary_channel"] else "") + ch["title"][:24], f"m:dyc:{ch['chat_id']}")]
            for ch in chans[:6]]
    rows.append([B(("● " if st["diary_mode"] == k else "") + v, f"m:dym:{k}") for k, v in diary.MODES.items()])
    rows.append([B(("● " if st["diary_time"] == t else "") + t, f"m:dyt:{t.replace(':', '')}") for t in diary.TIMES])
    rows.append([B("✍️ 지금 써보기 (나한테만)", "m:dyw")] + ([B("✅ 초안 올리기", "m:dyg")] if st["diary_draft"] else []))
    rows.append([B("⬅️ 처음으로", "m:home")])
    return Screen("\n".join(lines), menu._kb(rows))


async def _redraw(c: PanelCtx, toast: str) -> Screen:
    screen = await s_diary(c)
    screen.toast = toast
    return screen


@_owner_only
async def r_channel(c: PanelCtx) -> Screen:
    cid = to_int(c.arg(0).lstrip("-"))
    cid = -cid if cid and c.arg(0).startswith("-") else cid
    if not any(ch["chat_id"] == cid for ch in await _channels(c.svc.db)):
        return Screen(None, toast="글을 쓸 수 있는 채널이 아니에요.", alert=True)
    await c.svc.db.set_state(0, "diary_channel", cid)
    return await _redraw(c, "채널을 정했어요.")


@_owner_only
async def r_mode(c: PanelCtx) -> Screen:
    if c.arg(0) not in diary.MODES:
        return Screen(None)
    await c.svc.db.set_state(0, "diary_mode", c.arg(0))
    return await _redraw(c, diary.MODES[c.arg(0)])


@_owner_only
async def r_time(c: PanelCtx) -> Screen:
    t = next((t for t in diary.TIMES if t.replace(":", "") == c.arg(0)), None)   # 콜백은 ':' 로 나뉘어서 2330 꼴
    if not t:
        return Screen(None)
    await c.svc.db.set_state(0, "diary_time", t)
    return await _redraw(c, f"매일 {t} 에 써요.")


@_owner_only
async def r_write(c: PanelCtx) -> Screen:
    try:
        text = await diary.write(c.svc, datetime.now(c.svc.cfg.tz))
    except Exception as e:
        return Screen(None, toast=f"지금은 못 썼어요 ({type(e).__name__}). 잠시 뒤 다시.", alert=True)
    await c.svc.db.set_state(0, "diary_draft", text)
    await c.bot.send_message(c.uid, f"📓 일기 초안 (아직 안 올림)\n\n{text}", reply_markup=draft_kb(),
                             link_preview_options=NO_PREVIEW)
    return Screen(None, toast="1:1 로 초안을 보냈어요 👇")


@_owner_only
async def r_post(c: PanelCtx) -> Screen:
    st = await diary.settings(c.svc.db)
    if not st["diary_draft"]:
        return Screen(None, toast="올릴 초안이 없어요 (이미 올렸거나 버렸어요).", alert=True)
    if not st["diary_channel"]:
        return Screen(None, toast="먼저 📓 화면에서 채널을 골라 주세요.", alert=True)
    if not await diary.persist.claim(c.svc.db, f"diary_post:{hash(st['diary_draft'])}", 600):   # 연타 1번
        return Screen(None, toast="올리는 중이에요.")
    try:
        no = await diary.post(c.svc, c.bot, st["diary_draft"])
    except TelegramError as e:
        return Screen(None, toast=f"채널에 못 올렸어요: {e.message}", alert=True)
    return Screen(None, toast=f"채널에 올렸어요 (#{no}) ✅", alert=True)


@_owner_only
async def r_discard(c: PanelCtx) -> Screen:
    await c.svc.db.set_state(0, "diary_draft", None)
    return Screen(None, toast="초안을 버렸어요. 오늘은 안 올려요.")


menu.register_main(92, "dy", "📓 소담이 일기", OWNER)
for _code, _fn in (("dy", s_diary), ("dyc", r_channel), ("dym", r_mode), ("dyt", r_time), ("dyw", r_write),
                   ("dyr", r_write), ("dyg", r_post), ("dyx", r_discard)):
    menu.register_route(_code, Route(_fn, OWNER, scoped=False))
