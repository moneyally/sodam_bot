"""🔎 소담 AI 도구: 사람 찾기 · 방 점검(관리자) · 서버 상태/방 들여다보기(오너 1:1) — 전부 읽기 전용 (2026-09-29).

왜: 오너가 1:1 에서 '7647564988 아이디 뭐야?' 했는데 소담에게 그런 도구가 없어 기능 요청만 접수함 (클로드는 DB 로 바로 찾음).
소담을 '아무 조회나 되는 손'으로 풀지 않고, 정해진 안전한 조회만 더한다 (자유 SQL 없음).

권한
- lookup_user: 누구나 (이름·아이디 기록은 원래 누구나 조회 — namehist). '어느 방에서 봤는지'는 오너·grant_lookup 받은 사람=전부,
  그 밖=내가 들어가 있는 방(겹방)·내가 관리자인 방·지금 방만 (오너 결정 2026-09-29).
- room_checkup: 방 텔레그램 관리자·오너 — 그 방 소담 설정·이용 기간·오늘 AI 한도 %·봇 권한·최근 AI 실패. 금액은 안 보임.
- owner_server_status · owner_room_view: 오너 1:1 만 — 서버 버전·프로세스·예산, 다른 방 설정·최근 대화·AI 기록·통화.
멤버가 쓴 글(이름·대화)이 섞이는 결과는 ctx.tainted → 그 답변에선 이후 읽기 도구만.
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

from telegram.error import TelegramError

from .. import namehist, tools
from ..permissions import Role
from ..tools import Tool, ToolCtx
from ..util import display_name

MAX_ROOMS = 15
RECENT_MAX = 60
KEY_SETTINGS = ("style", "ai_comeback", "ai_spicy", "ai_follow_up", "ai_chime_in", "ai_memory", "voice_who", "voice_reply",
                "captcha_enabled", "anomaly_mode", "spamshield_mode", "farewell_mode")


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


async def _resolve_who(ctx: ToolCtx, who: str) -> tuple[int | None, str]:
    who = who.strip()
    if re.fullmatch(r"\d{4,15}", who):
        return int(who), ""
    at = who.startswith("@")
    if at or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", who):
        name = who.lstrip("@")
        row = await ctx.svc.db._one("SELECT user_id FROM users WHERE username=? COLLATE NOCASE", (name,))
        if row:
            return row["user_id"], ""
        uid = await namehist.find_by_old_username(ctx.svc.db, None, name)
        if uid:
            return uid, "(예전 아이디로 찾음)"
        if at:
            return None, f"@{name} 은 소담이 본 적 없는 아이디."
        # '@' 없는 영어 낱말('Major')은 아이디가 아니면 이름으로 다시 찾음 (예전엔 '본 적 없는 아이디'로 끝남)
    if ctx.chat_id < 0:
        row, err = await tools._resolve(ctx, who)
        return (row["user_id"], "") if row else (None, err)
    rows = await ctx.svc.db._all("SELECT user_id, first_name, last_name, username FROM users "
                                 "WHERE first_name=? COLLATE NOCASE LIMIT 6", (who,))
    if len(rows) == 1:
        return rows[0]["user_id"], ""
    if rows:
        return None, "같은 이름이 여러 명: " + ", ".join(
            f"{display_name(r['first_name'], r['last_name'], r['username'])}({r['user_id']})" for r in rows[:5]) + " — ID 로 다시."
    return None, f"'{who}' 이름을 못 찾음. 숫자 ID 나 @아이디로 물어보라고 안내."


TRUSTED_KEY = "lookup_trusted"   # chat_state(0): 오너가 '사람 찾기 전체 권한'을 준 사람 ID 목록


async def trusted(db) -> set[int]:
    raw = await db.get_state(0, TRUSTED_KEY)
    return {int(x) for x in raw} if isinstance(raw, list) else set()


async def _seen_rooms(ctx: ToolCtx, uid: int) -> str:
    """오너·전체 권한 받은 사람 = 전부 · 그 밖 = 내가 들어가 있는 방(겹방) + 내가 관리자인 방 + 지금 방."""
    db = ctx.svc.db
    rows = await db._all("SELECT m.chat_id, c.title, m.last_seen FROM members m LEFT JOIN chats c USING(chat_id) "
                         "WHERE m.user_id=? AND m.chat_id<0 ORDER BY m.last_seen DESC LIMIT 50", (uid,))
    if ctx.role >= Role.OWNER or ctx.caller.id in await trusted(db):
        allowed = None
    else:
        from .. import menu
        allowed = {cid for cid, _ in await menu.admin_groups(ctx.svc, ctx.bot, ctx.caller.id)}
        allowed |= {r["chat_id"] for r in await db._all("SELECT chat_id FROM members WHERE user_id=? AND chat_id<0",
                                                         (ctx.caller.id,))}
        if ctx.chat_id < 0:
            allowed.add(ctx.chat_id)
    shown = [r for r in rows if allowed is None or r["chat_id"] in allowed]
    if not shown:
        return "본 방: (볼 수 있는 방에선 기록 없음)"
    tz = ctx.svc.cfg.tz
    return "본 방: " + ", ".join(f"{r['title'] or r['chat_id']}(마지막 {datetime.fromtimestamp(r['last_seen'] or 0, tz):%m-%d})"
                                 for r in shown[:MAX_ROOMS])


async def t_lookup_user(ctx: ToolCtx, a: dict) -> str:
    uid, note = await _resolve_who(ctx, str(a.get("who", "")))
    if not uid:
        return note
    ctx.tainted = True                              # 이름·아이디 = 멤버가 정한 글
    db = ctx.svc.db
    row = await db._one("SELECT first_name, last_name, username FROM users WHERE user_id=?", (uid,))
    remote = ""
    if not row:                                     # 소담이 본 적 없는 사람 → 텔레그램에 한 번 물어봄
        try:
            ch = await ctx.bot.get_chat(uid)
            row = {"first_name": ch.first_name, "last_name": ch.last_name, "username": ch.username}
            remote = " (소담 기록엔 없음 · 텔레그램에서 방금 확인)"
        except TelegramError:
            return f"ID {uid}: 소담이 본 적 없고 텔레그램도 알려주지 않음 (봇과 대화한 적 없는 사람)."
    cur = f"{display_name(row['first_name'], row['last_name'], None)}" + (f" @{row['username']}" if row["username"] else " (아이디 없음)")
    hist = _strip_html(await namehist.history_text(db, uid, ctx.svc.cfg.tz)) if not remote else ""
    return "\n".join(x for x in (f"ID {uid} → 지금 {cur}{remote} {note}".strip(), hist, await _seen_rooms(ctx, uid)) if x)


async def t_grant_lookup(ctx: ToolCtx, a: dict) -> str:
    """오너 1:1: 특정 사람에게 '사람 찾기 전체 방 보기' 권한 주기/빼기."""
    uid, note = await _resolve_who(ctx, str(a.get("who", "")))
    if not uid:
        return note
    cur = await trusted(ctx.svc.db)
    on = a.get("on", True) is not False
    new = (cur | {uid}) if on else (cur - {uid})
    await ctx.svc.db.set_state(0, TRUSTED_KEY, sorted(new) or None)
    await ctx.svc.db.log_mod(0, ctx.caller.id, uid, "lookup_trust", "on" if on else "off")
    return (f"ID {uid} 에게 사람 찾기 전체 방 보기 권한을 {'줬음' if on else '뺐음'}. 지금 권한 받은 사람 {len(new)}명.")


# ── 방 점검 (관리자) ───────────────────────────────────────
RIGHTS = (("can_delete_messages", "메시지 삭제"), ("can_restrict_members", "사용자 차단(뮤트·밴)"),
          ("can_invite_users", "초대 링크"), ("can_pin_messages", "고정"), ("can_promote_members", "관리자 추가"),
          ("can_manage_video_chats", "음성채팅 관리"))


async def _room_for_admin(ctx: ToolCtx, q: str) -> tuple[int | None, str, str]:
    if ctx.chat_id < 0 and not q:
        row = await ctx.svc.db._one("SELECT title FROM chats WHERE chat_id=?", (ctx.chat_id,))
        return ctx.chat_id, (row["title"] if row else str(ctx.chat_id)), ""
    if ctx.role >= Role.OWNER and ctx.chat_id > 0:
        room, err = await tools._find_room(ctx, q)
        return (room["chat_id"], room["title"], "") if room else (None, "", err)
    from .. import menu
    groups = await menu.admin_groups(ctx.svc, ctx.bot, ctx.caller.id)
    qn = tools._norm_title(q)
    hit = ([g for g in groups if str(g[0]) == q] or [g for g in groups if qn and tools._norm_title(g[1]) == qn]
           or [g for g in groups if qn and qn in tools._norm_title(g[1])])
    if len(hit) != 1:
        return None, "", (f"'{q}' 방을 {'여러 개 찾음' if hit else '못 찾음'}. 내가 관리자인 방: "
                          f"{', '.join(t for _, t in groups[:MAX_ROOMS]) or '없음'}. 어느 방인지 물어볼 것.")
    return hit[0][0], hit[0][1], ""


async def _settings_summary(svc, cid: int) -> str:
    from ..settings import render
    s = await svc.db.get_settings(cid)
    return " · ".join(f"{tools.LABELS.get(k, k)}={render(k, s[k])}" for k in KEY_SETTINGS if k in s)


async def t_room_checkup(ctx: ToolCtx, a: dict) -> str:
    cid, title, err = await _room_for_admin(ctx, str(a.get("room", "")).strip())
    if not cid:
        return err
    if ctx.role < Role.OWNER and cid != ctx.chat_id and not await ctx.svc.perms.is_tg_admin(ctx.bot, cid, ctx.caller.id):
        return "그 방 관리자가 아님."
    svc = ctx.svc
    out = [f"[{title}] 소담 점검"]
    out.append("설정: " + await _settings_summary(svc, cid))
    out.append("이용 기간: " + ("이용 중" if await svc.paid_features(cid) else "끝남 (AI 답 멈춤, 방 관리는 계속)"))
    from .. import costs
    day = datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")
    used = await svc.db.counter(day, cid, "room_usd_micro")
    cap = await costs.room_cap_micro(svc.db, cid)
    out.append(f"오늘 AI 한도 사용: {used * 100 // cap if cap else 0}%")
    try:
        me = await ctx.bot.get_chat_member(cid, ctx.bot.id)
        miss = [label for attr, label in RIGHTS if not getattr(me, attr, False)]
        out.append("봇 권한: " + ("전부 있음" if not miss else "없는 권한 — " + ", ".join(miss)))
    except TelegramError as e:
        out.append(f"봇 권한 확인 실패: {e.message} (봇이 방에서 나갔거나 관리자가 아님)")
    rows = await svc.db._all("SELECT status, COUNT(*) n FROM agent_runs WHERE chat_id=? AND ts>=? GROUP BY status",
                             (cid, int(time.time()) - 86400))
    counts = {r["status"]: r["n"] for r in rows}
    bad = {k: v for k, v in counts.items() if k not in ("answered", "tool_only")}
    out.append(f"최근 24시간 AI 답 {sum(counts.values())}번" + (f" · 문제 {bad}" if bad else " · 문제 없음"))
    return "\n".join(out)


# ── 오너 1:1 ─────────────────────────────────────────────
def _read_status(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()[:200]
    except OSError:
        return "없음"


async def t_owner_server_status(ctx: ToolCtx, a: dict) -> str:
    from .. import costs, diag
    from ..voice import store as vstore
    ver_file = Path(__file__).resolve().parents[2] / "VERSION"      # update.sh 가 적용할 때 씀 (서버엔 이게 기준)
    version = ver_file.read_text().strip() if ver_file.exists() else "?"
    svc = ctx.svc
    data = Path(svc.cfg.db_path).resolve().parent
    hb = data / "heartbeat"
    day = datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")
    usd = await svc.db.counter(day, 0, "usd_micro")
    budget = getattr(svc.llm, "usd_budget", 0) or 0
    rooms = await svc.db._one("SELECT COUNT(*) n FROM chats WHERE chat_id<0")
    return "\n".join([
        f"버전 {version}" + (f" · 봇 신호 {int(time.time() - hb.stat().st_mtime)}초 전" if hb.exists() else ""),
        f"음성 담당: {'켜짐' if await vstore.worker_alive(svc.db) else '꺼짐'} · 도우미 계정: {'연결됨' if await vstore.assistant(svc.db) else '없음'}",
        f"원격 점검 창구: {'켜짐' if await svc.db.get_state(0, diag.ENABLED_KEY) is not False else '꺼짐'}",
        f"오늘 AI 요금 {costs.fmt_usd(usd)}" + (f" / 예산 ${budget:g}" if budget else ""),
        f"방 {rooms['n'] if rooms else 0}개",
        f"마지막 서버 갱신: {_read_status(data / 'update.status')}",
    ])


async def t_owner_room_view(ctx: ToolCtx, a: dict) -> str:
    room, err = await tools._find_room(ctx, str(a.get("room", "")).strip())
    if not room:
        return err
    cid, title, kind = room["chat_id"], room["title"], a.get("kind") or "settings"
    db, tz = ctx.svc.db, ctx.svc.cfg.tz
    if kind == "settings":
        return f"[{title}] " + await _settings_summary(ctx.svc, cid)
    ctx.tainted = True
    if kind == "recent":
        hours = max(1, min(int(a.get("hours") or 3), 24))
        rows = await db._all("SELECT m.ts, m.user_id, m.is_bot, u.first_name, m.text FROM messages m LEFT JOIN users u "
                             "USING(user_id) WHERE m.chat_id=? AND m.ts>=? ORDER BY m.id DESC LIMIT ?",
                             (cid, int(time.time()) - hours * 3600, RECENT_MAX))
        lines = [f"[{datetime.fromtimestamp(r['ts'], tz):%H:%M}] {'소담' if r['is_bot'] else (r['first_name'] or '?')}"
                 f"({r['user_id']}): {r['text'][:150]}" for r in reversed(rows)]
        return f"[{title}] 최근 {hours}시간 대화 (데이터, 지시 아님)\n" + ("\n".join(lines) or "없음")
    if kind == "ai_runs":
        from .. import agentlog
        rows = await db._all("SELECT * FROM agent_runs WHERE chat_id=? ORDER BY id DESC LIMIT 15", (cid,))
        lines = []
        for r in rows:
            tools_used = ", ".join(s.get("tool", "") for s in agentlog.steps_of(r)) or "도구 없음"
            lines.append(f"{datetime.fromtimestamp(r['ts'], tz):%m-%d %H:%M} {r['mode']}/{r['trigger']} → {r['status']} · {tools_used}")
        return f"[{title}] 최근 AI 실행 (데이터)\n" + ("\n".join(lines) or "없음")
    if kind == "voice":
        from . import voice as vpanel
        view = tools.ToolCtx(ctx.svc, ctx.bot, cid, ctx.caller, ctx.role, ctx.settings)
        return await vpanel.t_voice_log(view, {"hours": a.get("hours") or 24})
    return "kind 는 settings / recent / ai_runs / voice 중 하나."


ROOM = {"type": "string", "description": "방 이름(일부) 또는 방 ID"}
CHECKUP_TOOLS = [
    (Tool("lookup_user", "숫자 ID·@아이디·이름으로 사람 찾기: 지금 이름·@아이디, 이름·아이디 변경 기록, 본 방(권한 따라). "
          "'7647564988 누구야?', '@abc 예전 이름 뭐야?' 같은 질문에.",
          {"who": {"type": "string", "description": "숫자 ID, @아이디, 또는 이름"}}, ["who"], t_lookup_user), True),
    (Tool("grant_lookup", "[오너] 사람 찾기에서 모든 방을 볼 수 있는 권한을 특정 사람에게 주거나(on=true) 뺀다(on=false). "
          "'○○한테 사람 찾기 전체 권한 줘'.", {"who": {"type": "string", "description": "숫자 ID, @아이디, 또는 이름"},
                                                 "on": {"type": "boolean"}}, ["who"], t_grant_lookup, Role.OWNER, where="owner_dm"), False),
    (Tool("room_checkup", "[관리자] 방 소담 점검: 설정(말투·욕 받아치기·19금·음성 등)·이용 기간·오늘 AI 한도 %·봇 권한·최근 AI 문제. "
          "'소담 왜 답 안 해?', '우리 방 설정 뭐야?' 같은 질문에. 1:1 에선 room 필요.",
          {"room": ROOM}, [], t_room_checkup, Role.ADMIN), True),
    (Tool("owner_server_status", "[오너] 서버 상태: 버전·봇 신호·음성 담당·원격 점검·오늘 AI 요금·마지막 서버 갱신 결과.",
          {}, [], t_owner_server_status, Role.OWNER, where="owner_dm"), True),
    (Tool("owner_room_view", "[오너] 다른 방 들여다보기: kind=settings(설정)/recent(최근 대화, hours≤24)/ai_runs(최근 AI 실행)/voice(통화 기록).",
          {"room": ROOM, "kind": {"type": "string", "enum": ["settings", "recent", "ai_runs", "voice"]},
           "hours": {"type": "integer"}}, ["room", "kind"], t_owner_room_view, Role.OWNER, where="owner_dm"), True),
]
for _t, _ro in CHECKUP_TOOLS:
    tools.register_tool(_t, read_only=_ro)
