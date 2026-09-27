"""🔔 알림 규칙 화면 (방 관리자 1:1). 동작은 sodam/rules.py.

m:rl:<방>                       목록 + ➕ 🔑 낱말 / 👤 사람 / 🚪 입장 / 💤 조용함
m:rla:<방>                      🚪 입장 규칙 바로 만들기
m:rli:<방>:<번호>               항목: 켜기/끄기 · 알림 방식(dm/call) · 쿨다운 · 삭제
m:rlt / m:rlx / m:rlc:<방>:<번호>:<값>   켜기/끄기(목표값) · 방식 · 쿨다운(분)
m:rld:<방>:<번호>[:1]           삭제 확인 → 삭제
AI 에게 말로('누가 입금 얘기하면 알려줘') 만들면 방에 확인 카드 → 토큰 rule_save / rule_no.
번호로 찾을 땐 항상 그 방 규칙인지 확인 (다른 방 번호를 버튼에 넣어도 못 건드림).
"""
from __future__ import annotations

from .. import menu, rules
from ..menu import B, HubItem, PanelCtx, Route, Screen
from ..util import esc, to_int

COOLDOWNS = (1, 10, 60)


async def _rule(c: PanelCtx):
    rid = to_int(c.arg(0))
    return await c.svc.db._one("SELECT * FROM alert_rules WHERE id=? AND chat_id=?", (rid, c.cid)) if rid else None


async def s_list(c: PanelCtx) -> Screen:
    rows = await rules.room_rules(c.svc.db, c.cid)
    lines = [f"🔔 <b>알림 규칙</b> ({len(rows)}/{rules.MAX_RULES})",
             "정한 일이 생기면 알려드려요. 규칙마다 쿨다운이 있고 하루 최대 "
             f"{rules.DAILY_CAP}번까지만 울려요. 만든 분이 관리자에서 빠지면 규칙이 꺼져요."]
    if not await c.svc.paid_features(c.cid):
        lines.append("⚠️ 이용 기간(구독·체험) 중인 방에서만 동작해요.")
    lines.append("\n방에서 말로도 돼요: <i>소담아 누가 입금 얘기하면 나한테 알려줘</i>")
    btns = [[B(f"{'🟢' if r['enabled'] else '⏸'} #{r['id']} {(await rules.describe(c.svc, r))[:40]}",
               f"m:rli:{c.cid}:{r['id']}")] for r in rows]
    btns += [[B("🔑 낱말", f"m:in:{c.cid}:rlk"), B("👤 사람", f"m:in:{c.cid}:rlu")],
             [B("🚪 누가 들어오면", f"m:rla:{c.cid}"), B("💤 조용하면", f"m:in:{c.cid}:rlq")], menu._back(c.cid)]
    return Screen("\n".join(lines), menu._kb(btns))


async def s_item(c: PanelCtx) -> Screen:
    r = await _rule(c)
    if r is None:
        screen = await s_list(c)
        screen.toast = "없는 규칙이에요."
        return screen
    cid, rid = c.cid, r["id"]
    lines = [f"🔔 <b>규칙 #{rid}</b> · {'🟢 켜짐' if r['enabled'] else '⏸ 꺼짐'}", esc(await rules.describe(c.svc, r)),
             f"쿨다운 {r['cooldown']}분 · 오늘 {r['fired'] if r['day'] else 0}번 울림",
             f"만든 사람: {esc(await c.svc.db.first_name(r['created_by']) or str(r['created_by']))}"]
    toggle = B("⏸ 끄기", f"m:rlt:{cid}:{rid}:0") if r["enabled"] else B("▶️ 켜기", f"m:rlt:{cid}:{rid}:1")
    how = [B(("● " if r["action"] == a else "") + rules.ACTIONS[a], f"m:rlx:{cid}:{rid}:{a}") for a in ("dm", "call")]
    cool = [B(("● " if r["cooldown"] == m else "") + f"{m}분", f"m:rlc:{cid}:{rid}:{m}") for m in COOLDOWNS]
    rows = ([how] if r["action"] != "post" else []) + [cool, [toggle, B("🗑 삭제", f"m:rld:{cid}:{rid}")],
                                                          [B("⬅️ 목록", f"m:rl:{cid}")]]
    return Screen("\n".join(lines), menu._kb(rows))


async def _update(c: PanelCtx, sql: str, value) -> Screen:
    r = await _rule(c)
    if r is not None:
        await c.svc.db._write(f"UPDATE alert_rules SET {sql}=? WHERE id=? AND chat_id=?", (value, r["id"], c.cid))
        rules.forget(c.svc.db, c.cid)
        await c.svc.db.log_mod(c.cid, c.uid, None, "setting", f"알림 규칙 #{r['id']} {sql}={value}")
    screen = await s_item(c)
    screen.toast = "✅ 바꿨어요" if r is not None else screen.toast
    return screen


async def r_toggle(c: PanelCtx) -> Screen:
    return await _update(c, "enabled", 1 if c.arg(1) == "1" else 0)


async def r_action(c: PanelCtx) -> Screen:
    return await _update(c, "action", c.arg(1)) if c.arg(1) in ("dm", "call") else await s_item(c)


async def r_cooldown(c: PanelCtx) -> Screen:
    m = to_int(c.arg(1))
    return await _update(c, "cooldown", m) if m in COOLDOWNS else await s_item(c)


async def r_delete(c: PanelCtx) -> Screen:
    r = await _rule(c)
    if r is None:
        return await s_list(c)
    if c.arg(1) != "1":
        return Screen(f"🗑 규칙 #{r['id']} 을 삭제할까요?\n{esc(await rules.describe(c.svc, r))}",
                      menu._kb([[B("🗑 삭제", f"m:rld:{c.cid}:{r['id']}:1"), B("취소", f"m:rli:{c.cid}:{r['id']}")]]))
    await c.svc.db._write("DELETE FROM alert_rules WHERE id=? AND chat_id=?", (r["id"], c.cid))
    rules.forget(c.svc.db, c.cid)
    await c.svc.db.log_mod(c.cid, c.uid, None, "setting", f"알림 규칙 #{r['id']} 삭제")
    screen = await s_list(c)
    screen.toast = "삭제했어요."
    return screen


async def _create(c: PanelCtx, spec: dict) -> tuple[bool, str]:
    err = rules.validate(spec["trig"], str(spec.get("arg", "")), spec["action"], spec.get("text", ""))
    if err:
        return False, err
    if len(await rules.room_rules(c.svc.db, c.cid)) >= rules.MAX_RULES:
        return False, f"규칙은 방마다 {rules.MAX_RULES}개까지예요."
    rid = await rules.add(c.svc, c.cid, c.uid, spec)
    return True, f"✅ 규칙 #{rid} 을 만들었어요 (알림은 1:1 로 와요. 방식은 목록에서 바꿀 수 있어요)"


async def r_join(c: PanelCtx) -> Screen:
    ok, text = await _create(c, {"trig": "join", "action": "dm"})
    screen = await s_list(c)
    screen.toast, screen.alert = text, not ok
    return screen


async def i_keyword(c: PanelCtx, msg) -> tuple[bool, str]:
    word, _, flag = (msg.text or "").partition("|")
    return await _create(c, {"trig": "keyword", "arg": word.strip(), "action": "dm",
                             "who": "newbie" if "신규" in flag else "all"})


async def i_user(c: PanelCtx, msg) -> tuple[bool, str]:
    found = await c.svc.db.find_members(c.cid, (msg.text or "").strip())
    if len(found) != 1:
        return False, "이 방 멤버 한 명을 @아이디·정확한 이름·ID 로 보내주세요." + (" (여러 명이에요)" if found else "")
    return await _create(c, {"trig": "user", "arg": str(found[0]["user_id"]), "action": "dm"})


async def i_quiet(c: PanelCtx, msg) -> tuple[bool, str]:
    return await _create(c, {"trig": "quiet", "arg": (msg.text or "").strip().removesuffix("시간").strip(), "action": "dm"})


async def t_save(c: PanelCtx, spec) -> Screen:
    """말로 만든 규칙의 확인 카드 [✅ 만들기] (토큰 = 요청한 관리자만)."""
    ok, text = await _create(c, spec)
    return Screen(text + ("\n끄기·삭제는 관리자 1:1 메뉴 🔔 알림 규칙에서" if ok else ""), None,
                  toast="만들었어요" if ok else text, alert=not ok)


async def t_no(c: PanelCtx, _) -> Screen:
    return Screen("규칙을 만들지 않았어요.", None)


menu.register_hub(HubItem(38, "rl", "🔔 알림 규칙"))
menu.register_screen("rl", s_list)
menu.register_screen("rli", s_item)
for _code, _fn in (("rla", r_join), ("rlt", r_toggle), ("rlx", r_action), ("rlc", r_cooldown), ("rld", r_delete)):
    menu.register_route(_code, Route(_fn))
menu.register_token_action("rule_save", t_save, fresh=True)
menu.register_token_action("rule_no", t_no)
for _kind, _prompt, _fn in (
        ("rlk", "🔑 알려드릴 <b>낱말</b>을 보내주세요. 예: <code>입금</code>\n신규 입장자만: <code>입금 | 신규</code>", i_keyword),
        ("rlu", "👤 지켜볼 <b>사람</b>을 보내주세요 (@아이디·정확한 이름·ID). 그 사람이 말하면 알려드려요.", i_user),
        ("rlq", "💤 몇 시간 조용하면 알려드릴까요? (1~72) 예: <code>3</code>", i_quiet)):
    menu.register_input(_kind, _prompt, "rl", _fn, s_list)
    menu.register_route(_kind, Route(s_list))       # 입력 화면의 [⬅️ 메뉴로]
