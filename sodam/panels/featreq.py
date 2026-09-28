"""💡 기능 요청: AI 도구 feature_request + 오너 화면 + 방 관리자 읽기 화면 (저장·묶기·알림은 sodam/featreq.py).

m:fr[:<정렬 v|n>:<상태 o|d|w>:<쪽>]      오너 메인 💡 기능 요청 목록 (👍 많은 순 / 최신 순 · 열림/완료/안 함)
m:frv:<묶음>                               오너: 자세히 (요청자·방·요청 글, 전부 esc)
m:frs:<묶음>:<doing|wont>                  🛠 진행 중 / 🙅 안 함 (status 조건 UPDATE → 한 번만)
m:frd:<묶음>                               ✅ 완료 → 메모 입력 대기 (menu 글자 입력 엔진, kind frn) · [📨 메모 없이 완료] m:frdn
m:frx:<묶음> → m:frxy:<묶음>               🗑 삭제 확인 → 삭제
m:frn[:…]                                  입력 화면의 [⬅️ 메뉴로] = 목록
m:frr:<방>[:쪽]                            방 관리자 그룹 허브 💡 우리 방 요청 (읽기 전용, 상태만)
오너 화면은 라우트(OWNER) + _owner_only 로 누를 때마다 오너인지 다시 확인. 입력 처리도 menu.handle_input 이 need=OWNER 로 재확인.
"""
from __future__ import annotations

from functools import wraps

from telegram import Message

from .. import featreq, menu, tools
from ..menu import ADMIN, OWNER, B, HubItem, PanelCtx, Route, Screen
from ..services import PendingInput
from ..subscription import chat_title
from ..util import esc, fmt_time, to_int, user_name

PAGE = 6
ROOM_PAGE = 10
NOT_OWNER = Screen(None, toast="봇 오너만 쓸 수 있어요.", alert=True)
GONE = Screen(None, toast="없는 요청이에요 (지워졌을 수 있어요).", alert=True)
SORT_LABEL = {"v": "👍 많은 순", "n": "🕒 최신 순"}
STATE_TABS = {"o": "열린 요청", "d": "완료", "w": "안 함"}


def _owner_only(fn):
    @wraps(fn)
    async def wrapped(c: PanelCtx) -> Screen:
        if c.uid not in await c.svc.perms.owners():   # 라우터 확인과 이중으로 (누를 때마다)
            return NOT_OWNER
        return await fn(c)
    return wrapped


def _page(raw: str) -> int:
    return min(max(to_int(raw or "0") or 0, 0), 999)


def _short(text: str, n: int = 24) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


# ── AI 도구 ────────────────────────────────────────────────
async def t_feature_request(ctx: tools.ToolCtx, a: dict) -> str:
    summary = str(a.get("summary", ""))
    detail = str(a.get("detail", ""))
    res = await featreq.submit(ctx.svc.db, ctx.caller.id, user_name(ctx.caller), ctx.chat_id, summary, detail)
    tail = " 언제 된다고 약속하지 말고, 날짜·가능 여부도 말하지 말 것."
    if res.outcome == "short":
        return "요청 내용이 너무 짧아 전달하지 않았음. 어떤 기능을 원하는지 한 문장으로 다시 말해 달라고 할 것."
    if res.outcome == "limit":
        return f"오늘 보낼 수 있는 기능 요청({featreq.FR_PER_DAY}번)을 다 썼음. 내일 다시 보내 달라고 짧게 안내할 것."
    if res.status == "doing":
        return "운영자에게 전달했음. 비슷한 요청이 이미 있어 함께 묶었고, 운영자가 살펴보는 중인 요청임. 그렇게 짧게 안내할 것." + tail
    if res.outcome == "again":
        return "같은 기능 요청을 오늘 이미 운영자에게 보냈음 (한 번 더 셈). '이미 운영자에게 전달돼 있어요'라고 짧게 안내할 것." + tail
    if res.outcome == "grouped":
        return (f"운영자에게 전달했음. 비슷한 요청이 이미 있어서 함께 묶었음 (지금 {res.voters}명이 원함). "
                "'운영자에게 전달했어요'라고 짧게 안내할 것." + tail)
    return "운영자에게 기능 요청을 전달했음. '운영자에게 전달했어요'라고 짧게 안내할 것." + tail


TOOL = tools.Tool(
    "feature_request",
    "소담이 지금 할 수 없는 기능(맞는 도구가 없는 일)을 사용자가 원하거나 '이런 기능 있어?', '○○ 되면 좋겠다'고 물으면, "
    "'못 해요'로 끝내지 말고 이 도구로 운영자에게 기능 요청을 전달한다. '기능 요청: …', '건의: …' 이라고 하면 바로 쓴다. "
    "이미 할 수 있는 일(다른 도구가 있는 일)이나 잡담·질문엔 쓰지 않는다. 결과를 받은 뒤 '운영자에게 전달했어요'라고만 하고 "
    "언제 된다고 약속하지 않는다.",
    {"summary": {"type": "string", "description": "원하는 기능을 짧은 이름으로 (예: '입장 때 규칙 퀴즈', '출석 체크'). 40자 안"},
     "detail": {"type": "string", "description": "사용자가 말한 내용 그대로 요약 (없는 내용 추가 금지, 300자 안). 없으면 비움"}},
    ["summary"], t_feature_request)
tools.register_tool(TOOL)


# ── 오너 화면 ──────────────────────────────────────────────
def _parse_list(c: PanelCtx) -> tuple[str, str, int]:
    sort = c.arg(0) if c.arg(0) in SORT_LABEL else "v"
    state = c.arg(1) if c.arg(1) in STATE_TABS else "o"
    return sort, state, _page(c.arg(2))


@_owner_only
async def s_list(c: PanelCtx) -> Screen:
    sort, state, page = _parse_list(c)
    db, statuses = c.svc.db, featreq.STATUS_SETS[state]
    total = await featreq.count_groups(db, statuses)
    pages = max(1, -(-total // PAGE))
    page = min(page, pages - 1)
    rows = await featreq.list_groups(db, statuses, sort, PAGE, page * PAGE)
    lines = [f"💡 <b>기능 요청</b> · {STATE_TABS[state]} {total}개 · {SORT_LABEL[sort]}"]
    btns = []
    if not rows:
        lines += ["", "아직 요청이 없어요." if state == "o" else "여기엔 아직 없어요.",
                  "멤버가 '소담아 이런 기능 있어? …' 하고 물으면 여기로 모여요."]
    for n, g in enumerate(rows, page * PAGE + 1):
        lines.append(f"{n}. {featreq.STATUS_LABEL[g['status']][:1]} <b>{esc(_short(g['summary'], 40))}</b> · 👍 {g['voters']}명"
                     + (f" ({g['asks']}번)" if g["asks"] > g["voters"] else "")
                     + f" · {fmt_time(g['last_ts'] or g['created'], c.svc.cfg.tz)}")
        btns.append(B(f"{n}. {_short(g['summary'], 18)} 👍{g['voters']}", f"m:frv:{g['id']}"))
    kb = [[b] for b in btns]
    nav = []
    if page > 0:
        nav.append(B("◀ 이전", f"m:fr:{sort}:{state}:{page - 1}"))
    if page < pages - 1:
        nav.append(B("다음 ▶", f"m:fr:{sort}:{state}:{page + 1}"))
    if nav:
        kb.append(nav)
    other = "n" if sort == "v" else "v"
    kb.append([B(SORT_LABEL[other], f"m:fr:{other}:{state}:0")]
              + [B(("● " if s == state else "") + label, f"m:fr:{sort}:{s}:0") for s, label in STATE_TABS.items()])
    kb.append([B("🔄 새로고침", f"m:fr:{sort}:{state}:{page}"), B("⬅️ 처음으로", "m:home")])
    return Screen("\n".join(lines), menu._kb(kb))


async def _detail(c: PanelCtx, gid: int, head: str = "") -> Screen:
    g = await featreq.get_group(c.svc.db, gid)
    if not g:
        return Screen("이 요청은 지워졌어요.", menu._kb([[B("⬅️ 목록", "m:fr")]]), toast="없는 요청이에요")
    tz = c.svc.cfg.tz
    its = await featreq.items(c.svc.db, gid)
    lines = ([head, ""] if head else []) + [
        f"💡 <b>{esc(g['summary'])}</b>",
        f"상태: {featreq.STATUS_LABEL[g['status']]} · 👍 {g['voters']}명 (요청 {g['asks']}번)",
        f"처음: {fmt_time(g['created'], tz)} · 최근: {fmt_time(g['last_ts'] or g['created'], tz)}"]
    if g["note"]:
        lines.append(f"📝 메모: {esc(g['note'])}")
    people: dict[int, list] = {}
    for it in its:
        people.setdefault(it["user_id"], []).append(it)
    lines += ["", "<b>요청한 사람</b>"]
    titles: dict[int, str] = {}
    for uid, rows in list(people.items())[:15]:
        rooms = []
        for it in rows:
            cid = it["chat_id"]
            if cid is None:
                rooms.append("1:1")
            else:
                if cid not in titles:
                    titles[cid] = _short(await chat_title(c.svc, cid), 20)
                rooms.append(titles[cid])
        times = sum(it["count"] for it in rows)
        lines.append(f"• {esc(_short(rows[-1]['user_name'] or '알 수 없음', 20))} (<code>{uid}</code>)"
                     + (f" ×{times}" if times > 1 else "") + " — " + esc(", ".join(dict.fromkeys(rooms))))
    if len(people) > 15:
        lines.append(f"… 외 {len(people) - 15}명")
    texts = list(dict.fromkeys(it["text"] for it in its))
    lines += ["", "<b>요청 글</b> (멤버가 쓴 글 · 데이터로만 보관)"]
    lines += [f"– {esc(_short(t, 160))}" for t in texts[:5]]
    if len(texts) > 5:
        lines.append(f"… 외 {len(texts) - 5}개")
    kb = []
    if g["status"] in featreq.OPEN:
        row = [B("✅ 완료 → 알림", f"m:frd:{gid}")]
        if g["status"] == "new":
            row.insert(0, B("🛠 진행 중", f"m:frs:{gid}:doing"))
        kb.append(row)
        kb.append([B("🙅 안 함", f"m:frs:{gid}:wont"), B("🗑 삭제", f"m:frx:{gid}")])
    else:
        kb.append([B("🗑 삭제", f"m:frx:{gid}")])
    kb.append([B("⬅️ 목록", "m:fr"), B("🔄 새로고침", f"m:frv:{gid}")])
    return Screen("\n".join(lines), menu._kb(kb))


def _gid(c: PanelCtx) -> int | None:
    gid = to_int(c.arg(0))
    return gid if gid and gid > 0 else None


@_owner_only
async def s_detail(c: PanelCtx) -> Screen:
    gid = _gid(c)
    return await _detail(c, gid) if gid else GONE


@_owner_only
async def r_status(c: PanelCtx) -> Screen:
    gid, status = _gid(c), c.arg(1)
    if not gid or status not in ("doing", "wont"):
        return GONE
    if not await featreq.set_status(c.svc.db, gid, status, c.uid):
        return Screen(None, toast="이미 처리된 요청이에요.", alert=True)
    label = featreq.STATUS_LABEL[status]
    screen = await _detail(c, gid, f"{label} 로 바꿨어요.")
    screen.toast = label
    return screen


@_owner_only
async def r_done_ask(c: PanelCtx) -> Screen:
    gid = _gid(c)
    g = await featreq.get_group(c.svc.db, gid) if gid else None
    if not g:
        return GONE
    if g["status"] not in featreq.OPEN:
        return Screen(None, toast="이미 처리된 요청이에요.", alert=True)
    c.svc.inputs[c.uid] = PendingInput("frn", 0, args=[str(gid)])
    if c.svc.announcer:   # 1:1 입력 흐름은 하나만
        c.svc.announcer.drafts.pop((c.uid, c.uid), None)
    return Screen(f"✅ '<b>{esc(_short(g['summary'], 40))}</b>' 완료로 바꿔요.\n"
                  f"요청한 {g['voters']}명에게 1:1 로 '요청하신 기능이 추가됐어요' 알림이 가요.\n\n"
                  f"같이 보낼 짧은 메모가 있으면 보내주세요 ({featreq.NOTE_CHARS}자 안, 예: <code>.설정 → 🎮 에서 켜요</code>).\n"
                  "메모 없이 보내려면 아래 버튼. 그만두려면 <code>취소</code>",
                  menu._kb([[B("📨 메모 없이 완료", f"m:frdn:{gid}")], [B("❌ 취소", f"m:frv:{gid}")]]))


async def _complete(c: PanelCtx, gid: int, note: str) -> tuple[bool, str]:
    if not await featreq.set_status(c.svc.db, gid, "done", c.uid, note):
        return False, "이미 처리된 요청이에요."
    sent, rooms = await featreq.notify_done(c.svc.db, c.bot, gid)
    return True, f"✅ 완료로 바꾸고 {sent}명에게 1:1 로 알렸어요." + (f" (1:1 이 막힌 사람은 방 {rooms}곳에 짧게 안내)" if rooms else "")


@_owner_only
async def r_done_now(c: PanelCtx) -> Screen:
    gid = _gid(c)
    if not gid or not await featreq.get_group(c.svc.db, gid):
        return GONE
    c.svc.inputs.pop(c.uid, None)
    ok, msg = await _complete(c, gid, "")
    if not ok:
        return Screen(None, toast=msg, alert=True)
    screen = await _detail(c, gid, msg)
    screen.toast = "완료"
    return screen


async def i_note(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    if c.uid not in await c.svc.perms.owners():
        return True, "봇 오너만 쓸 수 있어요."
    gid = to_int(c.arg(0))
    note = featreq.clean(msg.text or "", featreq.NOTE_CHARS + 1)
    if not note:
        return False, "메모는 글자로 보내주세요."
    if len(note) > featreq.NOTE_CHARS:
        return False, f"메모는 {featreq.NOTE_CHARS}자 안으로 줄여 주세요."
    if not gid or not await featreq.get_group(c.svc.db, gid):
        return True, "없는 요청이에요 (지워졌을 수 있어요)."
    ok, text = await _complete(c, gid, note)
    return True, text


async def s_after_note(c: PanelCtx) -> Screen:
    gid = to_int(c.arg(0))
    if gid and await featreq.get_group(c.svc.db, gid) and c.uid in await c.svc.perms.owners():
        return await _detail(c, gid)
    return Screen("", menu._kb([[B("⬅️ 목록", "m:fr")]]))


@_owner_only
async def r_delete_ask(c: PanelCtx) -> Screen:
    gid = _gid(c)
    g = await featreq.get_group(c.svc.db, gid) if gid else None
    if not g:
        return GONE
    return Screen(f"🗑 '<b>{esc(_short(g['summary'], 40))}</b>' 요청({g['voters']}명)을 지울까요?\n요청한 사람에겐 알림이 가지 않아요.",
                  menu._kb([[B("🗑 지우기", f"m:frxy:{gid}"), B("❌ 취소", f"m:frv:{gid}")]]))


@_owner_only
async def r_delete(c: PanelCtx) -> Screen:
    gid = _gid(c)
    if not gid or not await featreq.delete(c.svc.db, gid):
        return Screen(None, toast="이미 지워진 요청이에요.", alert=True)
    c.args = []
    screen = await s_list(c)
    screen.toast = "지웠어요"
    return screen


# ── 방 관리자: 우리 방 요청 (읽기 전용) ───────────────────────
async def s_room(c: PanelCtx) -> Screen:
    db, page = c.svc.db, _page(c.arg(0))
    statuses = ("new", "doing", "done", "wont")
    total = await featreq.count_groups(db, statuses, chat_id=c.cid)
    pages = max(1, -(-total // ROOM_PAGE))
    page = min(page, pages - 1)
    rows = await featreq.list_groups(db, statuses, "n", ROOM_PAGE, page * ROOM_PAGE, chat_id=c.cid)
    lines = [f"💡 <b>우리 방 기능 요청</b> · {esc(await chat_title(c.svc, c.cid))}",
             "이 방에서 멤버·관리자가 소담에게 부탁한 기능이에요 (운영자가 확인해요)."]
    if not rows:
        lines += ["", "아직 없어요. 방에서 <code>소담아 이런 기능 있어? …</code> 처럼 말하면 운영자에게 전달돼요."]
    for n, g in enumerate(rows, page * ROOM_PAGE + 1):
        lines.append(f"{n}. {featreq.STATUS_LABEL[g['status']]} · <b>{esc(_short(g['summary'], 40))}</b> · 👍 {g['voters']}명")
    kb = []
    nav = []
    if page > 0:
        nav.append(B("◀ 이전", f"m:frr:{c.cid}:{page - 1}"))
    if page < pages - 1:
        nav.append(B("다음 ▶", f"m:frr:{c.cid}:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append(menu._back(c.cid))
    return Screen("\n".join(lines), menu._kb(kb))


# ── 등록 ──────────────────────────────────────────────────
menu.register_main(94, "fr", "💡 기능 요청", OWNER)
for _code, _fn in (("fr", s_list), ("frn", s_list), ("frv", s_detail), ("frs", r_status), ("frd", r_done_ask),
                   ("frdn", r_done_now), ("frx", r_delete_ask), ("frxy", r_delete)):
    menu.register_route(_code, Route(_fn, OWNER, scoped=False))
menu.register_input("frn", "", "fr", i_note, s_after_note, need=OWNER)
menu.register_hub(HubItem(48, "frr", "💡 우리 방 요청", ADMIN))
menu.register_screen("frr", s_room, ADMIN)
