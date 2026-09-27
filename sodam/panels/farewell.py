"""👋 퇴장 인사 화면 (그룹 허브, 인사 편집기와 같은 권한). 보내기는 sodam/farewell.py.

m:fw:<방>          켜기/끄기 · 자동 삭제 프리셋 · 문구(직접 쓴 것/말투 기본) · URL 버튼
m:fwt / m:fwb      글자 입력 kind 이름과 같게 (입력 뒤 '⬅️ 메뉴로' 도 이 화면)
m:in:<방>:fwt|fwb  문구 · URL 버튼 입력 (버튼 형식은 입장 인사와 같음)
m:fwd:<방>:t|b     삭제 확인 → 1회용 토큰
m:fwv:<방>         1:1 로 미리보기 + [🗑 닫기] (인사 편집기의 w_close 토큰)
"""
from __future__ import annotations

from telegram import Message
from telegram.error import TelegramError

from .. import farewell, menu
from ..greet import MAX_BUTTONS, clean_buttons, parse_buttons
from ..menu import B, HubItem, PanelCtx, Route, Screen
from ..styles import STYLES
from ..util import esc, josa
from .greet import CLOSE_TTL, _admin_name, _save

PARTS = {"t": "📄 문구", "b": "🔗 URL 버튼"}

menu.register_preset("farewell_mode", [("on", "✅ 켜기"), ("off", "❌ 끄기")], "fw")
menu.register_preset("farewell_delete_after", [(str(v), f"🗑 {label}") for v, label in farewell.DELETE_AFTER], "fw")


async def s_fw(c: PanelCtx) -> Screen:
    s = await c.svc.db.get_settings(c.cid)
    cid, tpl, btns = c.cid, s["farewell_template"], clean_buttons(s["farewell_buttons"])
    style = STYLES.get(s["style"])
    lines = ["👋 <b>퇴장 인사</b>",
             "멤버가 <b>스스로</b> 나가면 한마디 남겨요. 관리자·자동 관리(스팸·캡차 시간 초과 등)로 내보낸 사람, 봇, "
             "대량 입장 방어 중엔 안 해요.",
             f"{farewell.WINDOW}초에 한 번만 — 그 사이 나간 사람은 다음 인사에 합쳐요 ('OO 외 2명'). AI 비용 없음.", "",
             f"지금: <b>{'✅ 켜짐' if s['farewell_mode'] == 'on' else '❌ 꺼짐'}</b>",
             "자동 삭제: " + dict(farewell.DELETE_AFTER).get(s["farewell_delete_after"], f"{s['farewell_delete_after']}초")]
    if tpl:
        lines.append(f"📄 문구 (직접 씀): <i>{esc(tpl if len(tpl) <= 60 else tpl[:60] + '…')}</i>")
    else:
        lines.append(f"📄 문구: 방 말투({style.label if style else '정중'}) 기본 — "
                     f"<i>{esc(farewell.template_of(s))}</i>")
    lines.append(f"🔗 URL 버튼: {len(btns)}개" if btns else "🔗 URL 버튼: 없음")
    lines.append("🆔 나간 사람의 숫자 ID·@아이디 줄이 항상 붙어요 (사칭·먹튀 확인용).")
    rows = [menu._preset_row(s, cid, "farewell_mode"), menu._preset_row(s, cid, "farewell_delete_after"),
            [B("📄 문구 수정" if tpl else "📄 문구 쓰기", f"m:in:{cid}:fwt")] + ([B("🗑 삭제", f"m:fwd:{cid}:t")] if tpl else []),
            [B("🔗 URL 버튼 수정" if btns else "🔗 URL 버튼 추가", f"m:in:{cid}:fwb")]
            + ([B("🗑 삭제", f"m:fwd:{cid}:b")] if btns else []),
            [B("👀 미리보기 (1:1로 받기)", f"m:fwv:{cid}")],
            menu._back(cid)]
    return Screen("\n".join(lines), menu._kb(rows))


async def r_view(c: PanelCtx) -> Screen:
    s = await c.svc.db.get_settings(c.cid)
    ids: list[int] = []
    close = [B("🗑 닫기", f"m:k:{menu.token(c.svc, c.uid, c.cid, 'w_close', ids, CLOSE_TTL)}")]
    user = await c.svc.db._one("SELECT username FROM users WHERE user_id=?", (c.uid,))
    tpl, count = farewell.template_of(s), None
    if "{count}" in tpl:
        try:
            count = await c.bot.get_chat_member_count(c.cid)
        except TelegramError:
            pass
    text = farewell.render(tpl, [(c.uid, await _admin_name(c), user["username"] if user else None)], count)
    sent = await c.bot.send_message(c.uid, text, parse_mode="HTML",
                                    reply_markup=menu._kb(farewell.button_rows(s) + [close]))
    ids.append(sent.message_id)
    return Screen(None, toast="1:1 로 미리보기를 보냈어요 👇 (이름·ID 는 대표님 것으로)")


async def r_ask_delete(c: PanelCtx) -> Screen:
    part = c.arg(0)
    if part not in PARTS:
        return Screen(None)
    s = await c.svc.db.get_settings(c.cid)
    if not (s["farewell_template"] if part == "t" else clean_buttons(s["farewell_buttons"])):
        screen = await s_fw(c)
        screen.toast = "이미 비어 있어요."
        return screen
    tok = menu.token(c.svc, c.uid, c.cid, "fw_del", part)
    note = "\n(비우면 방 말투 기본 문구로 인사해요)" if part == "t" else ""
    return Screen(f"{josa(PARTS[part])} 삭제할까요?{note}",
                  menu._kb([[B("🗑 삭제", f"m:k:{tok}"), B("취소", f"m:fw:{c.cid}")]]))


async def t_delete(c: PanelCtx, part) -> Screen:
    if part == "t":
        await _save(c, "farewell_template=", farewell_template="")
    elif part == "b":
        await _save(c, "farewell_buttons=", farewell_buttons=[])
    screen = await s_fw(c)
    screen.toast = "삭제했어요."
    return screen


async def in_text(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    tpl = (msg.text or "").strip()
    if not tpl:
        return False, "문구는 글자로 보내주세요."
    if len(tpl) > farewell.MAX_TEMPLATE:
        return False, f"문구는 {farewell.MAX_TEMPLATE}자까지예요. (보낸 글: {len(tpl)}자)"
    await _save(c, f"farewell_template={tpl[:80]}", farewell_template=tpl)
    notes = [n for ok, n in (("{name}" in tpl, "이름은 맨 앞에 붙여요"), ("{id}" in tpl, "🆔 줄은 맨 아래에 붙여요")) if not ok]
    return True, "✅ 퇴장 인사 문구를 저장했어요." + (f"\n({' · '.join(notes)})" if notes else "")


async def in_buttons(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    btns, err = parse_buttons(msg.text or "")
    if err:
        return False, "❌ " + err
    await _save(c, f"farewell_buttons={len(btns)}개 " + ", ".join(u for _, u in btns), farewell_buttons=btns)
    return True, f"✅ URL 버튼 {len(btns)}개를 저장했어요."


menu.register_hub(HubItem(22, "fw", "👋 퇴장 인사"))
menu.register_screen("fw", s_fw)
menu.register_screen("fwt", s_fw)
menu.register_screen("fwb", s_fw)
menu.register_route("fwd", Route(r_ask_delete))
menu.register_route("fwv", Route(r_view))
menu.register_token_action("fw_del", t_delete, fresh=True)
menu.register_input("fwt", (
    "📄 새 <b>퇴장 인사 문구</b>를 보내주세요.\n"
    "<code>{name}</code> 나간 사람 이름 (멘션 아님) · <code>{username}</code> @아이디 · "
    "<code>{id}</code> 숫자 ID · <code>{count}</code> 남은 멤버 수\n"
    "예: <code>{name}님이 나가셨어요. 현재 {count}명</code>\n"
    f"<code>{{id}}</code> 가 없으면 맨 아래에 🆔 줄을 붙여요. (최대 {farewell.MAX_TEMPLATE}자)"), "fw", in_text, s_fw)
menu.register_input("fwb", (
    f"🔗 <b>URL 버튼</b>을 한 줄에 하나씩 보내주세요. (최대 {MAX_BUTTONS}개, 기존 버튼은 전부 바뀌어요)\n"
    "<code>공지 채널 - https://t.me/sodam_notice</code>\n"
    "주소는 <code>https://</code> 또는 <code>tg://</code> 만 돼요. 버튼 글자는 30자까지."), "fw", in_buttons, s_fw)
