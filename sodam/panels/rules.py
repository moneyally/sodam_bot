"""🔔 알림 규칙 화면 (방 관리자 1:1). 동작은 sodam/rules.py.

m:rl:<방>                       목록 + ➕ 🔑 낱말 / 👤 사람 / 🚪 입장 / 💤 조용함
m:rla:<방>                      🚪 입장 규칙 바로 만들기
m:rli:<방>:<번호>               항목: 켜기/끄기 · 알림 방식(dm/call) · 쿨다운 · 삭제
m:rlt / m:rlx / m:rlc:<방>:<번호>:<값>   켜기/끄기(목표값) · 방식 · 쿨다운(분)
m:rld:<방>:<번호>[:1]           삭제 확인 → 삭제
m:rlv:<방>:<번호>               🔎 미리 보기 (지난 7일이면 몇 번 울렸을지, rules.replay)
m:rlp:<방>:<멈춤>:<r|d>         폭주로 멈춘 규칙 [▶️ 계속 실행][⏸ 오늘 중지] — 만든 사람만·지금 관리자(fresh)·한 번만
AI 에게 말로('누가 입금 얘기하면 알려줘') 만들면 방에 확인 카드 → 토큰 rule_save / rule_no.
번호로 찾을 땐 항상 그 방 규칙인지 확인 (다른 방 번호를 버튼에 넣어도 못 건드림).
"""
from __future__ import annotations

from .. import cards, menu, persist, rules
from ..menu import B, HubItem, PanelCtx, Route, Screen
from ..util import esc, fmt_time, to_int

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
    btns = [[B(f"{'🟢' if r['enabled'] and not await rules.active_pause(c.svc.db, r['id']) else '⏸'} #{r['id']} "
               f"{(await rules.describe(c.svc, r))[:40]}", f"m:rli:{c.cid}:{r['id']}")] for r in rows]
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
    pause = await rules.active_pause(c.svc.db, rid)
    if pause:
        lines.append(f"⏸ 너무 자주 걸려서 멈춤 (10분에 {pause['hits']}번) · "
                     f"<code>{fmt_time(pause['until'], c.svc.cfg.tz)}</code>까지"
                     + (" · 오늘 중지" if pause["status"] == "today" else ""))
    toggle = B("⏸ 끄기", f"m:rlt:{cid}:{rid}:0") if r["enabled"] else B("▶️ 켜기", f"m:rlt:{cid}:{rid}:1")
    how = [B(("● " if r["action"] == a else "") + rules.ACTIONS[a], f"m:rlx:{cid}:{rid}:{a}") for a in ("dm", "call")]
    cool = [B(("● " if r["cooldown"] == m else "") + f"{m}분", f"m:rlc:{cid}:{rid}:{m}") for m in COOLDOWNS]
    rows = ([rules.pause_buttons(cid, pause["id"])] if pause and pause["status"] is None else []) + \
        ([how] if r["action"] != "post" else []) + [cool, [toggle, B("🗑 삭제", f"m:rld:{cid}:{rid}")],
                                                    [B("🔎 미리 보기", f"m:rlv:{cid}:{rid}"), B("⬅️ 목록", f"m:rl:{cid}")]]
    return Screen("\n".join(lines), menu._kb(rows))


async def s_preview(c: PanelCtx) -> Screen:
    """🔎 지난 7일 기록에 이 규칙을 대 보면 (코드로 셈, AI 없음)."""
    r = await _rule(c)
    if r is None:
        return await s_list(c)
    text = await rules.preview_text(c.svc, c.cid, dict(r))
    return Screen(f"🔎 <b>규칙 #{r['id']} 미리 보기</b>\n{esc(await rules.describe(c.svc, r))}\n\n{text}\n"
                  f"(쿨다운 {r['cooldown']}분 · 하루 최대 {rules.DAILY_CAP}번 적용, 기록된 대화·입장 기준)",
                  menu._kb([[B("⬅️ 규칙", f"m:rli:{c.cid}:{r['id']}")]]))


async def r_pause(c: PanelCtx) -> Screen:
    """폭주 멈춤 알림의 [▶️ 계속 실행](r) · [⏸ 오늘 중지](d). 권한은 누를 때 다시(Route fresh), 만든 사람만, 한 번만."""
    pid, act = to_int(c.arg(0)), {"r": "resume", "d": "today"}.get(c.arg(1))
    pause = await c.svc.db._one("SELECT * FROM alert_rule_pauses WHERE id=? AND chat_id=?", (pid, c.cid)) if pid else None
    r = pause and await c.svc.db._one("SELECT * FROM alert_rules WHERE id=? AND chat_id=?", (pause["rule_id"], c.cid))
    if not r or not act:
        screen = await s_list(c)
        screen.toast = "없는 규칙이에요."
        return screen
    if c.uid != r["created_by"]:
        return Screen(None, toast="규칙을 만든 분만 누를 수 있어요.", alert=True)
    c.args = [str(r["id"])]
    if not await rules.resolve_pause(c.svc, pause["id"], act, c.uid):
        screen = await s_item(c)
        screen.toast = "이미 처리한 알림이에요."
        return screen
    await c.svc.db.log_mod(c.cid, c.uid, None, "setting",
                           f"알림 규칙 #{r['id']} " + ("계속 실행" if act == "resume" else "오늘 중지"))
    screen = await s_item(c)
    screen.toast = "▶️ 다시 울려요 (1시간은 안 멈춰요)" if act == "resume" else "⏸ 오늘은 멈춰 둘게요 (내일 0시에 다시)"
    return screen


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
    await rules.drop_stats(c.svc.db, r["id"])
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
    if not await persist.claim(c.svc.db, f"rla:{c.cid}:{c.uid}", 5):   # 두 번 눌러 같은 규칙 2개 (감사 B5)
        return Screen(None, toast="방금 만들었어요.")
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
    """말로 만든 규칙의 확인 카드 [✅ 만들기] / [✅ + 오늘은 확인 생략] (토큰 = 요청한 관리자만, 카드당 한 번)."""
    if not await cards.claim(c.svc, spec, "day" if spec.get("day") else "ok"):
        return Screen(None, toast=cards.ALREADY)
    ok, text = await _create(c, spec)
    await cards.pressed(c.svc, c.cid, c.uid, "alert_rule", spec, rule_line(ok, text), done=ok)
    return Screen(text + ("\n끄기·삭제는 관리자 1:1 메뉴 🔔 알림 규칙에서" + cards.day_note(spec) if ok else ""), None,
                  toast="만들었어요" if ok else text, alert=not ok)


def rule_line(ok: bool, text: str) -> str:
    """AI 맥락용 결과 한 줄."""
    return ("✅ 알림 규칙 " + text.removeprefix("✅ 규칙 ").split(" 을 ")[0] + " 만듦") if ok else f"⚠️ 알림 규칙 못 만듦: {text[:60]}"


async def t_no(c: PanelCtx, spec) -> Screen:
    if not await cards.claim(c.svc, spec, "no"):
        return Screen(None, toast=cards.ALREADY)
    await cards.pressed(c.svc, c.cid, c.uid, "alert_rule", spec, "❌ 알림 규칙 취소 (안 만듦)", done=False)
    return Screen("규칙을 만들지 않았어요.", None)


menu.register_hub(HubItem(38, "rl", "🔔 알림 규칙"))
menu.register_screen("rl", s_list)
menu.register_screen("rli", s_item)
menu.register_screen("rlv", s_preview)
menu.register_route("rlp", Route(r_pause, fresh=True))
for _code, _fn in (("rla", r_join), ("rlt", r_toggle), ("rlx", r_action), ("rlc", r_cooldown), ("rld", r_delete)):
    menu.register_route(_code, Route(_fn))
menu.register_token_action("rule_save", t_save, fresh=True)
menu.register_token_action("rule_save" + cards.DAY, t_save, fresh=True)
menu.register_token_action("rule_no", t_no)
for _kind, _prompt, _fn in (
        ("rlk", "🔑 알려드릴 <b>낱말</b>을 보내주세요. 예: <code>입금</code>\n신규 입장자만: <code>입금 | 신규</code>", i_keyword),
        ("rlu", "👤 지켜볼 <b>사람</b>을 보내주세요 (@아이디·정확한 이름·ID). 그 사람이 말하면 알려드려요.", i_user),
        ("rlq", "💤 몇 시간 조용하면 알려드릴까요? (1~72) 예: <code>3</code>", i_quiet)):
    menu.register_input(_kind, _prompt, "rl", _fn, s_list)
    menu.register_route(_kind, Route(s_list))       # 입력 화면의 [⬅️ 메뉴로]
