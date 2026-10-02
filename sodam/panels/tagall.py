"""📣 전체 태그 — '소담아 방 전체 태그해줘' (AI 도구 mention_all) · `.전체태그 [할 말]` (관리자).

실제 요청 2026-10-02 일루왕방: "방전체인원 태그해줘 새로들어온사람말고" 를 4번 → 전체 태그 기능이 없어서 AI 가 greet_members
(오늘 들어온 사람만 돌려줌)로 대신함 → 새 멤버 4명만 태그. 그래서 전용 기능.

- 관리자만. 바로 하지 않고 확인 카드 [📣 태그 시작][❌ 취소] (요청한 관리자만 누름, 누를 때 관리자 다시 확인).
- 명령 `.전체태그` 는 commands.py 에 (panels ↔ commands 순환 import 때문, 뉴스와 같음).
- 대상: 이 방 멤버(members) − 나간 사람(member_left) − 봇 − 요청자. Bot API 는 전체 명단을 안 줌 → 서버에 MTProto 헬퍼(봇 세션)가
  있으면 카드 전에 텔레그램 전체 참가자를 받아 DB(users·members·member_left)에 채움(sync_members), 없으면 '소담이 본 사람'만
  → 카드에 '텔레그램 전체 M명 중 N명' 으로 밝힘.
- 텔레그램: 한 메시지의 멘션 알림은 앞 5명까지만 → 5명씩 나눠 보냄 · 그룹에 분당 20개 → 3.5초 간격 (RetryAfter 는 기다렸다 계속).
- 방마다 6시간에 한 번 (도배 방지, persist.claim — 재시작해도 유지). 도는 중 [⏹ 멈추기] (요청한 관리자).
- 태그 메시지는 10분 뒤 자동 삭제 (알림은 이미 감, persist.remember_delete — 재시작해도 지움). 첫 줄(할 말)은 남김.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from telegram.error import RetryAfter, TelegramError

from .. import cards, menu, persist, tools
from ..menu import PanelCtx, Screen
from ..permissions import Role
from ..tools import Tool, ToolCtx
from ..util import esc, mention, user_name

log = logging.getLogger(__name__)

PER_MSG = 5                 # 텔레그램: 한 메시지에서 알림이 가는 멘션은 앞 5명까지
SEND_GAP = 3.5              # 그룹 분당 20개 제한 → 17개/분
COOLDOWN = 6 * 3600         # 방마다 6시간에 한 번
CLEANUP = 600               # 태그 메시지 10분 뒤 지움
MAX_TEXT = 300
_running: dict[int, dict] = {}      # 방 → {"stop": bool, "sent": int, "total": int}
_tasks: set[asyncio.Task] = set()


SYNC_TIMEOUT = 30           # 전체 명단 받기 상한 (200명씩, 1만 명 방이면 50쪽)


async def sync_members(svc, bot, chat_id: int) -> int | None:
    """MTProto 헬퍼(봇 세션, svc.mtproto)로 텔레그램 전체 참가자를 받아 DB 에 채움 → 받은 사람 수 (헬퍼 없거나 실패면 None).
    users = 이름 갱신 (min 계정은 이름이 불완전할 수 있어 새로만) · members = 없으면 추가 (last_seen·joined_at 은 안 건드림 —
    '마지막 말한 때'·신규 입장 판단이 틀어지지 않게) · 명단에 있으면 member_left 지움 · 명단이 텔레그램 인원 수만큼 다 왔으면
    DB 에만 있던 사람은 나간 사람으로 (member_left). 탈퇴 계정(deleted)은 넣지 않음."""
    mt = getattr(svc, "mtproto", None)
    if mt is None or not getattr(mt, "bot_ready", False):
        return None
    try:
        async with asyncio.timeout(SYNC_TIMEOUT):
            users = await mt.participants(chat_id)
    except Exception as e:      # 헬퍼 실패는 DB 명단으로 계속
        log.info("전체 명단 받기 실패 %s: %r", chat_id, e)
        return None
    if not users:
        return None
    people = [u for u in users if isinstance(u, dict) and not u.get("deleted")]
    total = await _tg_total(bot, chat_id)
    complete = total is not None and len(users) >= total
    now = int(time.time())
    ids = [int(u["id"]) for u in people]

    def run(c):
        for u in people:
            row = (int(u["id"]), u.get("username") or None, u.get("first_name") or "", u.get("last_name") or None,
                   int(bool(u.get("is_bot"))), now)
            if u.get("min"):
                c.execute("INSERT OR IGNORE INTO users(user_id, username, first_name, last_name, is_bot, updated_at) "
                          "VALUES(?,?,?,?,?,?)", row)
            else:
                c.execute("INSERT INTO users(user_id, username, first_name, last_name, is_bot, updated_at) VALUES(?,?,?,?,?,?) "
                          "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, "
                          "last_name=excluded.last_name, updated_at=excluded.updated_at", row)
            c.execute("INSERT OR IGNORE INTO members(chat_id, user_id) VALUES(?,?)", (chat_id, int(u["id"])))
        c.executemany("DELETE FROM member_left WHERE chat_id=? AND user_id=?", [(chat_id, i) for i in ids])
        if complete:
            known = {r[0] for r in c.execute("SELECT user_id FROM members WHERE chat_id=?", (chat_id,))}
            c.executemany("INSERT INTO member_left(chat_id, user_id, ts) VALUES(?,?,?) "
                          "ON CONFLICT(chat_id, user_id) DO NOTHING", [(chat_id, i, now) for i in known - set(ids)])
    await svc.db.atomic(run)
    return len(people)


async def targets(db, chat_id: int, exclude: int | None = None) -> list[tuple[int, str]]:
    rows = await db._all(
        "SELECT m.user_id, u.first_name, u.last_name, u.username FROM members m JOIN users u ON u.user_id=m.user_id "
        "WHERE m.chat_id=? AND u.is_bot=0 AND m.user_id NOT IN (SELECT user_id FROM member_left WHERE chat_id=?) "
        "ORDER BY m.last_seen DESC", (chat_id, chat_id))
    return [(r["user_id"], user_name(r) or str(r["user_id"])) for r in rows if r["user_id"] != exclude]


def batches(people: list[tuple[int, str]]) -> list[list[tuple[int, str]]]:
    return [people[i:i + PER_MSG] for i in range(0, len(people), PER_MSG)]


def eta_text(n_msgs: int) -> str:
    sec = int(n_msgs * SEND_GAP)
    return f"약 {sec // 60}분 {sec % 60}초" if sec >= 60 else f"약 {sec}초"


async def _tg_total(bot, chat_id: int) -> int | None:
    try:
        return await bot.get_chat_member_count(chat_id)
    except Exception:          # 숫자는 안내용 — 못 받아도 카드는 띄움
        return None


async def offer(svc, bot, chat_id: int, caller, text: str) -> str:
    """확인 카드를 방에 올림. 돌려주는 글은 AI·명령이 요청자에게 할 짧은 안내."""
    text = (text or "").strip()[:MAX_TEXT]
    if chat_id in _running:
        return "지금 전체 태그를 보내는 중이라 새로 못 함 (끝나거나 멈춘 뒤에)."
    synced = await sync_members(svc, bot, chat_id)     # 헬퍼가 있으면 텔레그램 전체 명단 → DB
    people = await targets(svc.db, chat_id, caller.id)
    if not people:
        return "태그할 멤버를 아직 몰라요 (소담은 말하거나 들어온 적 있는 사람만 알아요)."
    total = await _tg_total(bot, chat_id)
    n_msgs = len(batches(people))
    spec = {"text": text, "n": len(people)}
    kb = await cards.card(svc, caller.id, chat_id, "mention_all", "tagall_go", "tagall_no", spec, ok_label="📣 태그 시작")
    known = f"텔레그램 전체 {total}명 중 " if total else ""
    who = "멤버" if synced else "소담이 아는 멤버"
    await bot.send_message(
        chat_id,
        f"📣 <b>전체 태그</b>를 할까요?\n{known}{who} <b>{len(people)}명</b> → {PER_MSG}명씩 {n_msgs}개 메시지 "
        f"({eta_text(n_msgs)})\n"
        + (f"할 말: {esc(text)}\n" if text else "")
        + f"· 태그 메시지는 {CLEANUP // 60}분 뒤 자동으로 지워져요 (알림은 그대로) · 방마다 {COOLDOWN // 3600}시간에 한 번\n"
        f"(요청한 {esc(caller.first_name or '')}님만 누를 수 있어요)",
        parse_mode="HTML", reply_markup=kb)
    return "확인 카드를 보냈음. 요청한 관리자가 [📣 태그 시작]을 눌러야 시작된다고 짧게 안내. 아직 태그한 게 아니니 했다고 말하지 말 것."


async def _send(bot, chat_id: int, text: str):
    while True:
        try:
            return await bot.send_message(chat_id, text, parse_mode="HTML")
        except RetryAfter as e:
            wait = e.retry_after.total_seconds() if isinstance(e.retry_after, timedelta) else float(e.retry_after)
            await asyncio.sleep(min(wait, 60) + 0.5)


async def run(svc, bot, chat_id: int, by: int, text: str, sleep=asyncio.sleep) -> int:
    """5명씩 보냄. 보낸 사람 수를 돌려줌 (멈추면 그때까지)."""
    people = await targets(svc.db, chat_id, by)
    state = _running.setdefault(chat_id, {"stop": False, "sent": 0, "total": len(people)})
    state["total"] = len(people)
    try:
        if text:
            await _send(bot, chat_id, f"📣 {esc(text)}")
        for i, group in enumerate(batches(people)):
            if state["stop"]:
                break
            if i:
                await sleep(SEND_GAP)
                if state["stop"]:
                    break
            try:
                msg = await _send(bot, chat_id, " ".join(mention(uid, name) for uid, name in group))
            except TelegramError as e:
                log.warning("전체 태그 전송 실패 %s: %s", chat_id, e)
                break
            state["sent"] += len(group)
            await persist.remember_delete(svc.db, bot, chat_id, msg.message_id, CLEANUP)
        await svc.db.log_mod(chat_id, by, None, "tagall",
                             f"{state['sent']}/{len(people)}명" + (" (멈춤)" if state["stop"] else ""))
        return state["sent"]
    finally:
        _running.pop(chat_id, None)


async def _is_admin(c: PanelCtx) -> bool:
    return await c.svc.perms.role(c.bot, c.cid, c.uid) >= Role.ADMIN


async def t_go(c: PanelCtx, spec) -> Screen:
    if not await _is_admin(c):
        return Screen(None, toast="관리자만 할 수 있어요.", alert=True)
    if c.cid in _running:
        return Screen(None, toast="지금 보내는 중이에요.", alert=True)
    if not await cards.claim(c.svc, spec, "ok"):
        return Screen(None, toast=cards.ALREADY)
    if not await persist.claim(c.svc.db, f"tagall:{c.cid}", COOLDOWN):
        return Screen(f"전체 태그는 방마다 {COOLDOWN // 3600}시간에 한 번이에요. 조금 뒤에 다시 해 주세요.", None)
    _running[c.cid] = {"stop": False, "sent": 0, "total": int(spec.get("n") or 0)}
    task = asyncio.create_task(run(c.svc, c.bot, c.cid, c.uid, str(spec.get("text") or "")))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    await cards.pressed(c.svc, c.cid, c.uid, "mention_all", spec, f"📣 전체 태그 시작 ({spec.get('n')}명)", done=True)
    stop = await menu.lasting_token(c.svc, c.uid, c.cid, "tagall_stop", {}, 3600)
    return Screen(f"📣 전체 태그 시작 — {spec.get('n')}명, {PER_MSG}명씩 보내는 중이에요.",
                  menu._kb([[menu.B("⏹ 멈추기", f"m:k:{stop}")]]))


async def t_no(c: PanelCtx, spec) -> Screen:
    if not await cards.claim(c.svc, spec, "no"):
        return Screen(None, toast=cards.ALREADY)
    await cards.pressed(c.svc, c.cid, c.uid, "mention_all", spec, "❌ 전체 태그 취소", done=False)
    return Screen("전체 태그를 하지 않았어요.", None)


async def t_stop(c: PanelCtx, spec) -> Screen:
    if c.cid not in _running:
        return Screen("이미 끝났어요.", None)
    return Screen(stop(c.cid), None)


menu.register_token_action("tagall_go", t_go, fresh=True)
menu.register_token_action("tagall_no", t_no)
menu.register_token_action("tagall_stop", t_stop)


def stop(chat_id: int) -> str:
    """도는 중인 전체 태그 멈춤 (말로 '소담아 멈춰' · 버튼 둘 다). 결과 글은 AI·버튼이 그대로 전함."""
    st = _running.get(chat_id)
    if not st:
        return "지금 보내는 중인 전체 태그 없음 (이미 끝났거나 시작 안 함). 멈췄다고 말하지 말 것."
    st["stop"] = True
    return f"⏹ 전체 태그 멈춤 ({st['sent']}/{st['total']}명까지 보냄)."


async def t_stop_tag_all(ctx: ToolCtx, a: dict) -> str:
    """'소담아 멈춰' — **누구나** (오너 결정 2026-10-03: 태그 폭탄을 맞는 쪽이 멤버). 멈추게 한 사람은 관리 기록에."""
    out = stop(ctx.chat_id)
    if out.startswith("⏹"):
        await ctx.svc.db.log_mod(ctx.chat_id, ctx.caller.id, None, "tagall_stop", out[2:60])
    return out


async def t_mention_all(ctx: ToolCtx, a: dict) -> str:
    return await offer(ctx.svc, ctx.bot, ctx.chat_id, ctx.caller, str(a.get("text") or ""))


tools.register_tool(Tool(
    "mention_all",
    "방 전체 멤버를 태그(멘션)해서 부름 — '전체 태그해줘', '모두 불러줘', '다 태그해'. 확인 카드를 보냄 (관리자만). "
    "멈추기는 stop_tag_all. 새로 들어온 사람만 부르는 인사는 greet_members. text = 태그와 함께 올릴 할 말(없으면 비움).",
    {"text": {"type": "string", "description": "함께 올릴 할 말 (선택)"}}, [], t_mention_all,
    min_role=Role.ADMIN, where="room"))
tools.register_tool(Tool(
    "stop_tag_all",
    "보내는 중인 전체 태그를 멈춤 — '소담아 멈춰·그만·중지·태그 그만' (누구나). 안 도는 중이면 그렇다고만.",
    {}, [], t_stop_tag_all, where="room"))
