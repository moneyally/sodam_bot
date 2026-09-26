"""🕵️ 사기 의심 검사 화면 (방 관리자 1:1). 검사 자체는 sodam/scamguard.py.

m:sg:<방ID>                   켜기/끄기 · 검사 항목 토글 · AI 확인 토글 · 처리 방식 프리셋 · 하루 AI 사용량
m:sgk:<방ID>                  의심 키워드 목록 (누르면 삭제 확인 — 1회용 토큰) · ➕ 추가(글자 입력 m:in:sgk)
m:sgr:<방ID>                  ⭐ 추천 키워드 넣기 (이미 있는 건 건너뜀 → 두 번 눌러도 같음)
m:sgt:<방ID>:<0|1>            '괜찮음' 신뢰 목록 비우기 (0 = 확인 화면, 1 = 비우기)
m:sgx:<방ID>:<알림ID>:d|b|m|t 관리자 1:1 알림의 [🗑 지우기] [🚫 밴] [🔇 뮤트 1일] [✅ 괜찮음]
"""
from __future__ import annotations

from telegram.error import TelegramError

from .. import menu, scamguard
from ..menu import ADMIN, B, HubItem, PanelCtx, Route, Screen
from ..permissions import may, no_right_text
from ..subscription import chat_title
from ..util import esc, to_int

CHECKS = [("scam_check_keywords", "📝 의심 키워드"), ("scam_check_wallet", "💰 지갑주소"),
          ("scam_check_links", "🔗 외부 초대 링크"), ("scam_check_newbie", "🆕 신규 입장자 첫 메시지")]
for _key in ["scam_guard", "scam_ai"] + [k for k, _ in CHECKS]:
    menu.register_toggle(_key, "sg")
menu.register_preset("scam_action", list(scamguard.ACTIONS.items()), "sg")


def _mark(on) -> str:
    return "✅ " if on else "❌ "


async def s_sg(c: PanelCtx) -> Screen:
    svc, cid = c.svc, c.cid
    s = await svc.db.get_settings(cid)
    on = s["scam_guard"]
    words = await scamguard.keywords(svc.db, cid)
    used, cap = await scamguard.ai_used_today(svc, cid), s["scam_daily_ai"]
    lines = ["🕵️ <b>사기 의심 검사</b>",
             "켜면 아래에서 고른 항목에 걸린 메시지를 텔레그램 관리자님들께 1:1 로 알려드려요 "
             "(코인 사기·DM 유도·지갑주소 뿌리기·리딩방 모집 등). 관리자 메시지는 검사하지 않아요.",
             "자동 제재는 하지 않아요 — 알림의 [🗑 지우기] [🚫 밴] [🔇 뮤트] [✅ 괜찮음] 버튼으로 직접 정해요.", "",
             f"지금: <b>{'켜짐' if on else '꺼짐'}</b>"]
    if on and not await svc.paid_features(cid):
        lines.append("⚠️ 이용 기간(구독·체험) 중인 방에서만 동작해요. 지금은 쉬고 있어요.")
    lines += [f"📝 의심 키워드: {len(words)}개" + (" (비어 있어요 — 키워드 목록에서 넣어주세요)" if not words else ""),
              f"🆕 신규 입장자: 들어온 지 {s['newbie_link_hours'] or scamguard.NEWBIE_DEFAULT_HOURS}시간 안 된 사람의 "
              f"처음 {s['scam_first_msgs']}개 메시지 (AI 확인이 켜져 있을 때만)",
              f"처리: <b>{esc(scamguard.ACTIONS.get(s['scam_action'], s['scam_action']))}</b> — "
              + ("메시지는 두고 관리자님께 확인 요청" if s["scam_action"] == "ask"
                 else "바로 가리고 관리자님께 확인 요청 (봇에게 삭제 권한이 없으면 알림만)"),
              "🤖 AI 확인: " + (f"걸린 메시지를 AI 가 한 번 더 확인해서 오탐을 줄여요. 오늘 {used}/{cap}회 "
                               "(넘으면 규칙만)" if s["scam_ai"] else "꺼짐 — 걸리면 바로 관리자 확인 요청 (AI 비용 없음)")]
    trusted = await scamguard.trusted_count(svc.db, cid)
    if trusted:
        lines.append(f"✅ '괜찮음'으로 표시한 사람: {trusted}명 (이 사람들은 검사 안 해요)")
    rows = [[B(_mark(on) + "사기 의심 검사", f"m:t:{cid}:scam_guard:{0 if on else 1}")]]
    rows += menu._chunks([B(_mark(s[k]) + label, f"m:t:{cid}:{k}:{0 if s[k] else 1}") for k, label in CHECKS], 2)
    rows += [[B(_mark(s["scam_ai"]) + "🤖 AI 한 번 더 확인", f"m:t:{cid}:scam_ai:{0 if s['scam_ai'] else 1}")],
             menu._preset_row(s, cid, "scam_action"),
             [B(f"📝 의심 키워드 목록 ({len(words)})", f"m:sgk:{cid}")]]
    if trusted:
        rows.append([B("🧹 '괜찮음' 목록 비우기", f"m:sgt:{cid}:0")])
    rows.append(menu._back(cid))
    return Screen("\n".join(lines), menu._kb(rows))


async def s_sgk(c: PanelCtx) -> Screen:
    words = await scamguard.keywords(c.svc.db, c.cid)
    lines = ["📝 <b>의심 키워드</b>",
             "이 말이 들어간 메시지를 검사해요 (띄어쓰기·대소문자 무시). 관리자가 직접 넣고 빼요.",
             menu._joined(words) if words else "(없음)"]
    if words:
        lines.append("\n키워드를 누르면 삭제할 수 있어요.")
    btns = [B(f"🗑 {w[:20]}", f"m:k:{menu.token(c.svc, c.uid, c.cid, 'sgk_ask', w, menu.LIST_TOKEN_TTL)}")
            for w in words[:menu.LIST_SHOW]]
    rows = menu._chunks(btns, 2) + [[B("➕ 키워드 추가", f"m:in:{c.cid}:sgk")],
                                    [B("⭐ 추천 키워드 넣기", f"m:sgr:{c.cid}")],
                                    [B("⬅️ 뒤로", f"m:sg:{c.cid}")]]
    return Screen("\n".join(lines), menu._kb(rows))


async def r_recommend(c: PanelCtx) -> Screen:
    new, err = await scamguard.add_keywords(c.svc.db, c.cid, scamguard.RECOMMENDED)
    if new:
        await c.svc.db.log_mod(c.cid, c.uid, None, "setting", "scam_keyword+=" + ",".join(new))
    screen = await s_sgk(c)
    screen.toast = err or (f"⭐ 추천 키워드 {len(new)}개 넣었어요" if new else "추천 키워드는 이미 다 있어요")
    screen.alert = bool(err)
    return screen


async def t_ask(c: PanelCtx, word) -> Screen:
    tok = menu.token(c.svc, c.uid, c.cid, "sgk_del", word)
    return Screen(f"📝 의심 키워드 <b>{esc(word)}</b> 삭제할까요?",
                  menu._kb([[B("🗑 삭제", f"m:k:{tok}"), B("취소", f"m:sgk:{c.cid}")]]))


async def t_del(c: PanelCtx, word) -> Screen:
    await scamguard.remove_keyword(c.svc.db, c.cid, word)
    await c.svc.db.log_mod(c.cid, c.uid, None, "setting", f"scam_keyword-={word}")
    screen = await s_sgk(c)
    screen.toast = "삭제했어요."
    return screen


async def i_add(c: PanelCtx, msg) -> tuple[bool, str]:
    items = [x for x in menu._split_items((msg.text or msg.caption or "").strip()) if len(x) <= scamguard.KEYWORD_MAX_LEN]
    items = items[:menu.MAX_ITEMS_PER_INPUT]
    if not items:
        return False, f"키워드는 2~{scamguard.KEYWORD_MAX_LEN}자로 보내주세요."
    new, err = await scamguard.add_keywords(c.svc.db, c.cid, items)
    if err:
        return False, err
    if new:
        await c.svc.db.log_mod(c.cid, c.uid, None, "setting", "scam_keyword+=" + ",".join(new))
    return True, f"✅ 의심 키워드 {len(new)}개 추가했어요." if new else "이미 있는 키워드예요."


async def r_trust_clear(c: PanelCtx) -> Screen:
    if c.arg(0) != "1":
        return Screen("🧹 '괜찮음'으로 표시한 사람을 모두 다시 검사할까요?",
                      menu._kb([[B("🧹 비우기", f"m:sgt:{c.cid}:1"), B("취소", f"m:sg:{c.cid}")]]))
    await scamguard.clear_trust(c.svc.db, c.cid)
    await c.svc.db.log_mod(c.cid, c.uid, None, "setting", "scam_trust=비움")
    screen = await s_sg(c)
    screen.toast = "비웠어요."
    return screen


# ── 관리자 1:1 알림 버튼 ─────────────────────────────────
DONE = {"d": "🗑 메시지를 지웠어요.", "b": "🚫 {who}님을 내보냈어요.", "m": "🔇 {who}님을 1일 뮤트했어요.",
        "t": "✅ {who}님은 괜찮은 사람으로 표시했어요. 이 방에선 앞으로 사기 의심 검사를 하지 않아요."}


async def r_alert(c: PanelCtx) -> Screen:
    aid, act = to_int(c.arg(0)), c.arg(1)
    row = await c.svc.db._one("SELECT * FROM scam_alerts WHERE id=? AND chat_id=?", (aid, c.cid)) if aid else None
    if not row or act not in DONE:
        return Screen(None, toast="만료된 알림이에요.", alert=True)
    right = "delete" if act == "d" else "restrict"
    if not await may(c.svc.perms, c.bot, c.cid, c.uid, right):
        return Screen(None, toast=no_right_text(right), alert=True)
    if (row["done"] in (act, "b", "t")) or (act == "d" and row["deleted"]):
        return Screen(None, toast="이미 처리된 알림이에요.", alert=True)
    uid = row["user_id"]
    reason = "사기 의심: " + row["reason"][:80]
    svc, bot = c.svc, c.bot
    try:
        if act == "d":
            await bot.delete_message(c.cid, row["msg_id"])
            await svc.db.log_mod(c.cid, c.uid, uid, "scam_delete", row["reason"][:100])
        elif act == "b":
            await svc.mod.ban(bot, c.cid, uid, c.uid, reason)
        elif act == "m":
            await svc.mod.mute(bot, c.cid, uid, scamguard.MUTE_MINUTES, c.uid, reason)
        else:
            await scamguard.trust(svc.db, c.cid, uid, c.uid)
            await svc.db.log_mod(c.cid, c.uid, uid, "scam_trust", "괜찮음")
    except TelegramError as e:
        need = "메시지 삭제" if act == "d" else "사용자 차단"
        return Screen(None, toast=f"실패했어요: {e.message[:100]} (봇에게 '{need}' 권한이 있는지 확인해주세요)",
                      alert=True)
    await svc.db._write("UPDATE scam_alerts SET done=? WHERE id=?", (act, aid))
    who = f"{esc(row['name'])}(<code>{uid}</code>)"
    title = esc(await chat_title(svc, c.cid))
    kb = None
    if act == "d":  # 지운 뒤에도 밴·뮤트·괜찮음은 할 수 있게
        kb = scamguard.alert_kb(c.cid, aid, True)
    elif act == "m":  # 뮤트 뒤엔 밴·괜찮음(뮤트는 그대로)
        kb = menu._kb([[B("🚫 밴", f"m:sgx:{c.cid}:{aid}:b"), B("✅ 괜찮음(이 사람 믿기)", f"m:sgx:{c.cid}:{aid}:t")]])
    return Screen(f"🕵️ <b>{title}</b>\n" + DONE[act].format(who=who), kb, toast="처리했어요.")


menu.register_hub(HubItem(33, "sg", "🕵️ 사기 의심 검사"))
menu.register_screen("sg", s_sg)
menu.register_screen("sgk", s_sgk)
menu.register_route("sgr", Route(r_recommend, ADMIN))
menu.register_route("sgt", Route(r_trust_clear, ADMIN, fresh=True))
menu.register_route("sgx", Route(r_alert, ADMIN))
menu.register_input("sgk", "📝 추가할 <b>의심 키워드</b>를 보내주세요. 예: <code>수익 보장</code>\n"
                    "여러 개면 쉼표나 줄바꿈으로 구분 (한 번에 최대 20개)", "sgk", i_add, s_sgk)
menu.register_token_action("sgk_ask", t_ask)
menu.register_token_action("sgk_del", t_del, fresh=True)
