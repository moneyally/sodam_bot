"""🤬 패드립·성적 모욕 자동 제재 화면 (방 관리자 1:1). 감지·제재는 sodam/abuse.py.

m:abu:<방ID>                 모드·단계·초기화 시간·알림·글 지우기 · 허용 낱말 · 최근 7일 · 최근 기록
m:abx:<방ID>:<기록ID>:u|f|y   관리자 알림 버튼: ↩️ 되돌리기 / 🙅 오탐(취소+되돌리기) / 👍 맞음(기록만 모드)
                             — 누를 때마다 '사용자 차단' 권한을 지금 상태로 다시 확인, 한 번만 처리
"""
from __future__ import annotations

from datetime import datetime

from telegram.error import TelegramError

from .. import abuse, menu
from ..menu import ADMIN, B, HubItem, PanelCtx, Route, Screen
from ..permissions import may, no_right_text
from ..subscription import chat_title
from ..util import esc, mention, user_name
from . import log as log_panel

menu.register_preset("abuse_mode", [(k, v) for k, v in abuse.MODES.items()], "abu")
menu.register_preset("abuse_ladder", [(k, v) for k, v in abuse.LADDERS.items()], "abu")
menu.register_preset("abuse_reset_hours", [("1", "1시간"), ("24", "24시간"), ("168", "7일")], "abu")
menu.register_preset("abuse_notify", [("final", "🔔 추방만 알림"), ("all", "🔔 매번 알림")], "abu")
menu.register_toggle("abuse_delete", "abu")
log_panel.ACTIONS.setdefault("abuse_warn", "🤬 패드립·성적 모욕 경고")
log_panel.ACTIONS.setdefault("abuse_undo", "↩️ 패드립 제재 되돌림")
log_panel.ACTIONS.setdefault("abuse_fp", "🙅 패드립 제재 오탐")

MAX_ALLOW = 30


async def s_abu(c: PanelCtx) -> Screen:
    svc, cid = c.svc, c.cid
    s = await svc.db.get_settings(cid)
    st = await abuse.stats(svc.db, cid)
    allow = s.get("abuse_allow") or []
    lines = ["🤬 <b>패드립·성적 모욕 자동 제재</b>",
             "남의 <b>부모 욕(패드립)</b>과 사람을 향한 <b>성적 모욕</b>만 잡아요. "
             "'ㅅㅂ·존나' 같은 감탄 욕, 장난·닉네임·남 얘기, 소담에게 한 말은 안 잡아요.",
             "코드가 후보를 고르고 AI 가 한 번 더 확인해서, 확실할 때만 처리해요. 관리자·자유 멤버는 빼요.", "",
             f"모드: <b>{abuse.MODES.get(s['abuse_mode'], s['abuse_mode'])}</b>",
             f"단계: <b>{abuse.LADDERS.get(s['abuse_ladder'], s['abuse_ladder'])}</b> · "
             f"{s['abuse_reset_hours']}시간 지나면 횟수 초기화",
             f"관리자 알림: {'매번' if s['abuse_notify'] == 'all' else '추방할 때만'} ('사용자 차단' 권한 관리자 1:1)",
             f"허용 낱말: {esc(', '.join(allow)) if allow else '(없음)'} — 닉네임처럼 걸리면 안 되는 말"]
    if s["abuse_mode"] == "shadow":
        lines.append("👀 기록만: 아무도 제재하지 않고, 걸린 글을 관리자께 보내 👍/🙅 를 받아요. 오탐이 거의 없으면 ⚖️ 로 바꾸세요.")
    lines += ["", f"최근 7일: 걸림 {st['total']}건 · 추방 {st['final']} · 🙅 오탐 {st['fp']} · 👍 맞음 {st['ok']}"]
    rows_db = await abuse.recent(svc.db, cid, 6)
    if rows_db:
        lines.append("최근:")
        for r in rows_db:
            when = datetime.fromtimestamp(r["ts"], svc.cfg.tz).strftime("%m/%d %H:%M")
            state = abuse.STATUS.get(r["status"], "") or abuse.action_label(r["action"])
            lines.append(f"<code>{when}</code> {abuse.KINDS.get(r['kind'], '')} · {esc(r['text'][:30])} · {state}")
    rows = [menu._preset_row(s, cid, "abuse_mode")[:2], menu._preset_row(s, cid, "abuse_mode")[2:],
            *menu._chunks(menu._preset_row(s, cid, "abuse_ladder"), 2),
            menu._preset_row(s, cid, "abuse_reset_hours"), menu._preset_row(s, cid, "abuse_notify"),
            *menu._toggle_rows(s, cid, ["abuse_delete"]),
            [B("✏️ 허용 낱말", f"m:in:{cid}:abal")], menu._back(cid)]
    return Screen("\n".join(lines), menu._kb(rows))


async def in_allow(c: PanelCtx, msg) -> tuple[bool, str]:
    raw = (msg.text or "").strip()
    if raw in ("없음", "비우기", "-"):
        words = []
    else:
        words = []
        for w in raw.replace("\n", ",").split(","):
            w = w.strip()
            if 2 <= len(w) <= 20 and w not in words:
                words.append(w)
        if not words:
            return False, "2~20자 낱말을 쉼표로 보내주세요. 예: <code>딸딸, 슈퍼엠창</code> (비우려면 <code>없음</code>)"
    await c.svc.db.set_setting(c.cid, "abuse_allow", words[:MAX_ALLOW])
    await c.svc.db.log_mod(c.cid, c.uid, None, "setting", f"abuse_allow={', '.join(words[:MAX_ALLOW])}")
    return True, f"✅ 허용 낱말 {len(words[:MAX_ALLOW])}개를 저장했어요."


async def r_action(c: PanelCtx) -> Screen:
    """관리자 알림 버튼."""
    sid, act = c.arg(0), c.arg(1)
    row = await abuse.get(c.svc.db, c.cid, int(sid)) if sid.isdecimal() and len(sid) <= 12 else None
    if row is None or act not in ("u", "f", "y"):
        return Screen(None, toast="만료된 알림이에요.", alert=True)
    if not await may(c.svc.perms, c.bot, c.cid, c.uid, "restrict"):
        return Screen(None, toast=no_right_text("restrict"), alert=True)
    svc, bot = c.svc, c.bot
    if act == "y":
        if not await abuse.claim(svc.db, row["id"], ("shadow",), "ok", c.uid):
            return Screen(None, toast="이미 처리했어요.", alert=True)
        toast = "👍 맞음으로 표시했어요."
    else:
        frm = ("active", "shadow") if act == "f" else ("active",)
        to = "fp" if act == "f" else "undone"
        if not await abuse.claim(svc.db, row["id"], frm, to, c.uid):
            return Screen(None, toast="이미 처리했어요.", alert=True)
        try:
            if row["status"] == "active":
                await abuse.undo(svc, bot, row, c.uid)
        except TelegramError as e:
            await svc.db._write("UPDATE abuse_strikes SET status=?, by_id=NULL WHERE id=?", (row["status"], row["id"]))
            return Screen(None, toast=f"실패했어요: {str(e)[:100]} (봇에게 '사용자 차단' 권한이 있는지 확인해주세요)", alert=True)
        await svc.db.log_mod(c.cid, c.uid, row["user_id"], "abuse_fp" if act == "f" else "abuse_undo",
                             f"기록 #{row['id']}")
        toast = "🙅 오탐으로 처리하고 되돌렸어요 (횟수에서도 빠짐)." if act == "f" else "↩️ 되돌렸어요 (횟수에서도 빠짐)."
    row = await abuse.get(svc.db, c.cid, row["id"])
    urow = await svc.db._one("SELECT first_name, last_name, username FROM users WHERE user_id=?", (row["user_id"],))
    who = mention(row["user_id"], user_name(urow) if urow else str(row["user_id"]))
    text = abuse.detail_text(await chat_title(svc, c.cid), row, svc.cfg.tz, who)
    kb = abuse.buttons(c.cid, row)
    return Screen(text, kb or menu._kb([]), toast=toast)


menu.register_hub(HubItem(39, "abu", "🤬 패드립·성적 모욕"))
menu.register_screen("abu", s_abu)
menu.register_route("abx", Route(r_action, ADMIN, fresh=True))
menu.register_input("abal", "허용 낱말(닉네임 등)을 쉼표로 보내주세요. 예: <code>딸딸, 슈퍼엠창</code> · 비우려면 <code>없음</code>",
                    "abu", in_allow, s_abu)
