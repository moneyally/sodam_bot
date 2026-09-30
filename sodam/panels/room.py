"""방 관리 보조 화면들.

- 📜 규칙 (m:rules): 보기 · ✏️ 글자 입력으로 바꾸기(2000자) · 🗑 삭제(확인 토큰)
- ✏️ 숫자 직접 입력 (m:num[:묶음] → m:numi:<키>): 버튼 프리셋에 없는 값도 정하기. 허용 키만, settings.coerce 로 범위 검사
  보안(m:sec)·경고 단계(m:wl) 화면 맨 아래에 버튼을 붙인다 (AI 화면은 ai.py 가 직접 넣음)
- 🛠️ 봇 관리자 (m:badm): 텔레그램 관리자·오너만. 목록 · ➕ @username/숫자 ID · 🗑 빼기(확인 토큰)
  봇 관리자는 다른 봇 관리자를 추가·삭제할 수 없다 (TG_ADMIN).
"""
from __future__ import annotations

from telegram import Message
from telegram.error import TelegramError

from .. import menu
from ..menu import TG_ADMIN, B, HubItem, PanelCtx, Route, Screen
from ..services import PendingInput
from ..settings import LABELS, RANGES, coerce, over_cap
from ..util import display_name, esc, josa, to_int

MAX_RULES = 2000
MAX_BOT_ADMINS = 20


# ── 📜 규칙 ───────────────────────────────────────────────
async def s_rules(c: PanelCtx) -> Screen:
    rules = (await c.svc.db.get_settings(c.cid))["rules"]
    lines = ["📜 <b>방 규칙</b>", "멤버가 방에서 <code>.규칙</code> 을 치면 보여줘요. 예약공지의 {규칙} 자리에도 들어가요.", ""]
    lines.append(esc(rules) if rules else "(아직 없어요)")
    rows = [[B("✏️ 새로 쓰기" if rules else "✏️ 규칙 쓰기", f"m:in:{c.cid}:rules")]]
    if rules:
        rows[0].append(B("🗑 삭제", f"m:rulesx:{c.cid}"))
    rows.append(menu._back(c.cid))
    return Screen("\n".join(lines), menu._kb(rows))


async def s_rules_ask_delete(c: PanelCtx) -> Screen:
    if not (await c.svc.db.get_settings(c.cid))["rules"]:
        return await s_rules(c)
    tok = menu.token(c.svc, c.uid, c.cid, "del_rules", None)
    return Screen("📜 방 규칙을 삭제할까요?", menu._kb([[B("🗑 삭제", f"m:k:{tok}"), B("취소", f"m:rules:{c.cid}")]]))


async def _save_rules(c: PanelCtx, text: str) -> None:
    await c.svc.db.set_setting(c.cid, "rules", text)
    await c.svc.db.log_mod(c.cid, c.uid, None, "rules", f"수정 ({len(text)}자)" if text else "삭제")


async def t_del_rules(c: PanelCtx, _arg) -> Screen:
    await _save_rules(c, "")
    screen = await s_rules(c)
    screen.toast = "규칙을 지웠어요."
    return screen


async def _input_rules(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    text = (msg.text or msg.caption or "").strip()
    if len(text) > MAX_RULES:
        return False, f"규칙은 {MAX_RULES}자까지예요. 지금 {len(text):,}자라 줄여서 다시 보내주세요."
    await _save_rules(c, text)
    return True, "✅ 방 규칙을 저장했어요."


# ── ✏️ 숫자 직접 입력 ─────────────────────────────────────
NUM_GROUPS: dict[str, tuple[str, list[str]]] = {
    "sec": ("🛡️ 보안", ["flood_count", "flood_seconds", "flood_mute_minutes", "dup_limit", "newbie_link_hours"]),
    "wl": ("⚠️ 경고 단계", ["warn_mute_at", "warn_mute_minutes", "warn_ban_at"]),
    "ai": ("🤖 AI", ["reply_max_chars", "user_rate_per_min", "room_rate_per_min", "web_search_daily", "image_daily", "video_weekly", "video_seconds"]),
}
GROUP_OF = {k: g for g, (_, keys) in NUM_GROUPS.items() for k in keys}
NUM_KEYS = [k for k in GROUP_OF if k in RANGES]   # 범위가 정해진 정수 설정만


def _unit(key: str) -> str:
    if key.endswith("_minutes"):
        return "분"
    if key.endswith("_hours"):
        return "시간"
    if key.endswith("_seconds"):
        return "초"
    if key.endswith("_chars"):
        return "자"
    return "회" if key.endswith(("_at", "_per_min", "_daily", "_limit")) else "개"


async def s_num(c: PanelCtx) -> Screen:
    grp = c.arg(0) if c.arg(0) in NUM_GROUPS else ""
    s = await c.svc.db.get_settings(c.cid)
    groups = [grp] if grp else list(NUM_GROUPS)
    lines = ["✏️ <b>숫자 직접 입력</b>", "버튼에 없는 값도 정할 수 있어요. 바꿀 항목을 고르세요."]
    rows = []
    for g in groups:
        if not grp:
            lines.append(f"· {NUM_GROUPS[g][0]}")
        rows += menu._chunks([B(f"{LABELS[k]}: {s[k]}", f"m:numi:{c.cid}:{k}")
                              for k in NUM_GROUPS[g][1] if k in NUM_KEYS], 2)
    rows.append(menu._back(c.cid, grp or "g"))
    return Screen("\n".join(lines), menu._kb(rows))


async def r_num_ask(c: PanelCtx) -> Screen:
    key = c.arg(0)
    if key not in NUM_KEYS:
        return Screen(None)
    lo, hi = RANGES[key]
    cur = (await c.svc.db.get_settings(c.cid))[key]
    c.svc.inputs[c.uid] = PendingInput("num", c.cid, args=[key])
    if c.svc.announcer:  # 1:1 입력 흐름은 하나만 (예약공지 마법사와 서로 취소)
        c.svc.announcer.drafts.pop((c.uid, c.uid), None)
    return Screen(f"✏️ <b>{esc(LABELS[key])}</b>\n지금: {cur}{_unit(key)}\n\n"
                  f"{lo} ~ {hi} 사이의 숫자를 보내주세요.\n\n5분 안에 보내주세요. 그만두려면 <code>취소</code>",
                  menu._kb([[B("❌ 취소", f"m:num:{c.cid}:{GROUP_OF[key]}")]]))


async def _input_num(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    key = c.arg(0)
    if key not in NUM_KEYS:
        return True, "바꿀 수 없는 항목이에요."
    raw = (msg.text or "").strip().replace(",", "")
    try:
        value = coerce(key, raw)
    except ValueError as e:
        return False, f"❌ {esc(str(e))}"
    if (why := over_cap(key, value)) and c.uid not in await c.svc.perms.owners():
        return False, f"❌ {esc(why)}"
    await menu._set(c, key, value)
    return True, f"✅ {esc(LABELS[key])}: {value}{_unit(key)}"


async def s_after_num(c: PanelCtx) -> Screen:
    grp = GROUP_OF.get(c.arg(0), "")
    fn = menu.SCREENS.get(grp)
    return await fn(PanelCtx(c.svc, c.bot, c.uid, c.cid, [])) if fn else await s_num(c)


def _num_row(grp: str):
    async def rows(c: PanelCtx) -> list[list]:
        return [[B("✏️ 숫자 직접 입력", f"m:num:{c.cid}:{grp}")]]
    return rows


menu.register_screen_extra("sec", _num_row("sec"))
menu.register_screen_extra("wl", _num_row("wl"))


# ── 🛠️ 봇 관리자 ─────────────────────────────────────────
async def _bot_admins(c: PanelCtx) -> list[tuple[int, str]]:
    ids = sorted(await c.svc.db.bot_admin_ids(c.cid))
    out = []
    for uid in ids:
        u = await c.svc.db._one("SELECT first_name, last_name, username FROM users WHERE user_id=?", (uid,))
        out.append((uid, display_name(u["first_name"], u["last_name"], u["username"]) if u else f"ID {uid}"))
    return out


async def s_badm(c: PanelCtx) -> Screen:
    admins = await _bot_admins(c)
    lines = ["🛠️ <b>봇 관리자</b>",
             "텔레그램 관리자가 아니어도 이 방의 봇 설정·관리 명령(경고·뮤트 등)을 쓸 수 있는 사람이에요.",
             "봇 관리자는 다른 봇 관리자를 추가·삭제하거나 이용 기간 화면을 볼 수 없어요.", ""]
    lines += [f"• {esc(name)} (<code>{uid}</code>)" for uid, name in admins] or ["(없어요)"]
    if admins:
        lines.append("\n이름을 누르면 봇 관리자에서 뺄 수 있어요.")
    btns = [B(f"🗑 {name[:20]}", f"m:k:{menu.token(c.svc, c.uid, c.cid, 'ask_badm', uid, menu.LIST_TOKEN_TTL)}")
            for uid, name in admins]
    rows = menu._chunks(btns, 2)
    if len(admins) < MAX_BOT_ADMINS:
        rows.append([B("➕ 봇 관리자 추가", f"m:in:{c.cid}:badm")])
    rows.append(menu._back(c.cid))
    return Screen("\n".join(lines), menu._kb(rows))


async def t_ask_badm(c: PanelCtx, uid) -> Screen:
    name = dict(await _bot_admins(c)).get(uid)
    if name is None:
        screen = await s_badm(c)
        screen.toast = "이미 봇 관리자가 아니에요."
        return screen
    tok = menu.token(c.svc, c.uid, c.cid, "del_badm", uid)
    return Screen(f"🛠️ <b>{esc(name)}</b>님을 봇 관리자에서 뺄까요?",
                  menu._kb([[B("🗑 빼기", f"m:k:{tok}"), B("취소", f"m:badm:{c.cid}")]]))


async def t_del_badm(c: PanelCtx, uid) -> Screen:
    uid = int(uid)
    had = uid in await c.svc.db.bot_admin_ids(c.cid)
    if had:
        await c.svc.db.set_bot_admin(c.cid, uid, False)
        await c.svc.db.log_mod(c.cid, c.uid, uid, "bot_admin", "해제")
    screen = await s_badm(c)
    screen.toast = "봇 관리자에서 뺐어요." if had else "이미 봇 관리자가 아니에요."
    return screen


BADM_PROMPT = ("🛠️ 봇 관리자로 추가할 사람의 <b>@username</b> 또는 <b>숫자 ID</b>를 보내주세요.\n"
               "@username 은 이 방에서 봇이 본 적 있는 멤버만 찾을 수 있어요.")


async def _input_badm(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    raw = (msg.text or "").strip()
    if not raw or len(raw) > 64 or "\n" in raw:
        return False, "@username 이나 숫자 ID 하나만 보내주세요."
    uid = to_int(raw)
    if uid is not None:
        if uid <= 0:
            return False, "숫자 ID 는 양수예요. 상대가 봇에게 /start 후 🪪 내 ID 로 확인할 수 있어요."
        rows = await c.svc.db.find_members(c.cid, raw)
        name = display_name(rows[0]["first_name"], rows[0]["last_name"], rows[0]["username"]) if rows else f"ID {uid}"
    else:
        rows = await c.svc.db.find_members(c.cid, raw)
        if not rows:
            return False, f"이 방에서 '{esc(raw[:30])}' 멤버를 못 찾았어요. 숫자 ID 로 보내주세요."
        if len(rows) > 1:
            return False, "같은 이름이 여러 명이에요. @username 이나 숫자 ID 로 보내주세요."
        uid = rows[0]["user_id"]
        name = display_name(rows[0]["first_name"], rows[0]["last_name"], rows[0]["username"])
    if uid == c.bot.id:
        return False, "봇 자신은 추가할 수 없어요."
    current = await c.svc.db.bot_admin_ids(c.cid)
    if uid in current:
        return True, f"{esc(name)}님은 이미 봇 관리자예요."
    try:
        tg_admin = await c.svc.perms.is_tg_admin(c.bot, c.cid, uid)
    except TelegramError:
        tg_admin = False
    if tg_admin:
        return True, f"{esc(name)}님은 텔레그램 관리자라 이미 모든 설정을 할 수 있어요."
    if len(current) >= MAX_BOT_ADMINS:
        return True, f"봇 관리자는 방당 {MAX_BOT_ADMINS}명까지예요."
    await c.svc.db.set_bot_admin(c.cid, uid, True)
    await c.svc.db.log_mod(c.cid, c.uid, uid, "bot_admin", "추가")
    return True, f"✅ {esc(josa(name, '을를'))} 봇 관리자로 추가했어요."


# ── 등록 ──────────────────────────────────────────────────
menu.register_hub(HubItem(60, "rules", "📜 규칙"))
menu.register_screen("rules", s_rules)
menu.register_screen("rulesx", s_rules_ask_delete)
menu.register_input("rules", f"📜 새 <b>방 규칙</b>을 보내주세요 ({MAX_RULES}자까지).\n지금 규칙은 통째로 바뀌어요.",
                    "rules", _input_rules, s_rules)
menu.register_token_action("del_rules", t_del_rules, fresh=True)

menu.register_screen("num", s_num)
menu.register_route("numi", Route(r_num_ask))
menu.register_input("num", "✏️ 바꿀 항목을 먼저 골라주세요.", "num", _input_num, s_after_num)   # 시작은 m:numi (항목마다 안내가 달라서)

menu.register_hub(HubItem(75, "badm", "🛠️ 봇 관리자", TG_ADMIN))
menu.register_screen("badm", s_badm, need=TG_ADMIN)
menu.register_input("badm", BADM_PROMPT, "badm", _input_badm, s_badm, need=TG_ADMIN)
menu.register_token_action("ask_badm", t_ask_badm, need=TG_ADMIN)
menu.register_token_action("del_badm", t_del_badm, fresh=True, need=TG_ADMIN)
