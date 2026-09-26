"""🗓️ 예약공지 패널 (그룹 허브 → m:sc).

m:sc:<방>            목록 (🟢 켜짐 / ⏸ 꺼짐 · 언제 · 제목) + ➕ 새로 만들기
m:sci:<방>:<번호>    항목: 👀 미리보기 · ✏️ 수정 · 켜기/끄기 · 🗑 삭제
m:scp / m:sce / m:sct:<방>:<번호>:<0|1> / m:scd   미리보기 · 수정 · 켜기/끄기(목표값) · 삭제 확인(1회용 토큰)
m:scn:<방>           새로 만들기 (방당 announce.MAX_PER_CHAT 개)

번호로 찾을 때는 항상 get_schedule(방, 번호) — 다른 방의 번호를 버튼에 넣어 보내도 못 건드린다.
만들기·수정은 announce.Announcer 마법사를 1:1 에서 돌린다 (대화는 1:1, 공지는 그 방에).
"""
from __future__ import annotations

from datetime import datetime

from telegram.error import TelegramError

from .. import menu
from ..announce import CLOSE_KB, MAX_PER_CHAT, MEDIA_LABEL, describe_when
from ..menu import B, HubItem, PanelCtx, Route, Screen
from ..util import esc

PREVIEW_CHARS = 300


def _name(row) -> str:
    if row["title"]:
        return row["title"]
    text = (row["text"] or "").replace("\n", " ")
    return (text[:20] + ("…" if len(text) > 20 else "")) or "(내용 없음)"


def _when(row) -> str:
    return describe_when(row["kind"], row["at_time"], row["interval_min"])


def _flags(row) -> str:
    return (" 📌" if row["pin"] else "") + (f" [{MEDIA_LABEL.get(row['media_type'], '파일')}]" if row["media_type"] else "")


def _sid(c: PanelCtx, i: int = 0) -> int | None:
    raw = c.arg(i)
    return int(raw) if raw.isdecimal() and len(raw) <= 12 else None


async def _row(c: PanelCtx):
    """이 방의 예약공지만 (다른 방 번호면 None)."""
    sid = _sid(c)
    return await c.svc.db.get_schedule(c.cid, sid) if sid is not None else None


GONE = "없는 예약공지예요. 목록을 새로 불러왔어요."


# ── 화면 ──────────────────────────────────────────────────
async def s_list(c: PanelCtx) -> Screen:
    rows = await c.svc.db.schedules(c.cid)
    lines = [f"🗓️ <b>예약공지</b> ({len(rows)}/{MAX_PER_CHAT})",
             "정해진 시각이나 간격마다 방에 공지를 올려요."]
    if rows:
        lines.append("공지를 누르면 미리보기·수정·켜기/끄기·삭제를 할 수 있어요.\n🟢 켜짐 · ⏸ 꺼짐 · 📌 고정")
    else:
        lines.append("\n아직 등록된 공지가 없어요. <b>➕ 새로 만들기</b>를 누르면 제목·내용·시간을 차례로 물어봐요.")
    if not await c.svc.paid_features(c.cid):
        lines.append("\n⚠️ 이용 기간이 끝나서 지금은 공지가 올라가지 않아요.")
    btns = [[B(f"{'🟢' if r['enabled'] else '⏸'} {_when(r)}{_flags(r)} · {_name(r)[:24]}", f"m:sci:{c.cid}:{r['id']}")]
            for r in rows]
    btns.append([B("➕ 새로 만들기", f"m:scn:{c.cid}")])
    btns.append([B("⬅️ 뒤로", f"m:g:{c.cid}")])
    return Screen("\n".join(lines), menu._kb(btns))


async def s_item(c: PanelCtx) -> Screen:
    r = await _row(c)
    if r is None:
        screen = await s_list(c)
        screen.toast = GONE
        return screen
    tz = c.svc.cfg.tz
    body = (r["text"] or "").strip()
    if len(body) > PREVIEW_CHARS:
        body = body[:PREVIEW_CHARS] + "…"
    media = f"[{MEDIA_LABEL.get(r['media_type'], '파일')}] " if r["media_type"] else ""
    last = datetime.fromtimestamp(r["last_sent"], tz).strftime("%m/%d %H:%M") if r["last_sent"] and r["last_msg_id"] else "아직 없음"
    lines = [f"🗓️ <b>예약공지 #{r['id']}</b>",
             f"상태: {'🟢 켜짐' if r['enabled'] else '⏸ 꺼짐'}",
             f"언제: {_when(r)}" + (" · 📌 고정" if r["pin"] else ""),
             f"제목: {esc(r['title']) if r['title'] else '(없음)'}",
             f"최근 발송: {last}",
             f"내용:\n<blockquote>{media}{esc(body) if body else '(글 없음)'}</blockquote>"]
    sid = r["id"]
    toggle = (B("⏸ 끄기", f"m:sct:{c.cid}:{sid}:0") if r["enabled"] else B("▶️ 켜기", f"m:sct:{c.cid}:{sid}:1"))
    kb = menu._kb([[B("👀 미리보기", f"m:scp:{c.cid}:{sid}"), B("✏️ 수정", f"m:sce:{c.cid}:{sid}")],
                   [toggle, B("🗑 삭제", f"m:scd:{c.cid}:{sid}")],
                   [B("⬅️ 목록", f"m:sc:{c.cid}")]])
    return Screen("\n".join(lines), kb)


# ── 동작 ──────────────────────────────────────────────────
async def r_preview(c: PanelCtx) -> Screen:
    r = await _row(c)
    if r is None:
        return await s_item(c)
    try:  # 패널은 그대로 두고 1:1 에 새 메시지로 (닫기 버튼으로 지움)
        await c.svc.announcer.send(c.bot, c.uid, title=r["title"], text=r["text"], media_type=r["media_type"],
                                   media_id=r["media_id"], reply_markup=CLOSE_KB, rules_chat=c.cid)
    except TelegramError as e:
        return Screen(None, toast=f"미리보기를 보내지 못했어요: {e.message[:80]}", alert=True)
    return Screen(None, toast="👀 아래에 미리보기를 보냈어요.")


async def r_toggle(c: PanelCtx) -> Screen:
    r = await _row(c)
    if r is None or c.arg(1) not in ("0", "1"):
        return await s_item(c)
    on = c.arg(1) == "1"  # 목표값: 두 번 눌리거나 옛 패널을 눌러도 결과가 같다
    if bool(r["enabled"]) != on:
        await c.svc.db.set_schedule_enabled(c.cid, r["id"], on)
        await c.svc.db.log_mod(c.cid, c.uid, None, "schedule", f"#{r['id']} {'on' if on else 'off'}")
    screen = await s_item(c)
    screen.toast = f"#{r['id']} {'켰어요' if on else '껐어요'}"
    return screen


async def r_ask_delete(c: PanelCtx) -> Screen:
    r = await _row(c)
    if r is None:
        return await s_item(c)
    tok = menu.token(c.svc, c.uid, c.cid, "del_sc", r["id"])
    return Screen(f"🗑 예약공지 <b>#{r['id']}</b> ({_when(r)} · {esc(_name(r))})\n삭제할까요? 되돌릴 수 없어요.",
                  menu._kb([[B("🗑 삭제", f"m:k:{tok}"), B("취소", f"m:sci:{c.cid}:{r['id']}")]]))


async def t_delete(c: PanelCtx, sid) -> Screen:
    ok = await c.svc.db.delete_schedule(c.cid, int(sid))  # 토큰 안의 방으로만
    if ok:
        await c.svc.db.log_mod(c.cid, c.uid, None, "schedule_del", f"#{sid}")
    screen = await s_list(c)
    screen.toast = "삭제했어요." if ok else "이미 없는 공지예요."
    return screen


async def _wizard(c: PanelCtx, row=None) -> Screen:
    reason = await c.svc.announcer.start_dm(c.bot, c.uid, c.cid, edit_row=row)
    if reason:
        return Screen(None, toast=reason, alert=True)
    return Screen(None, toast="✏️ 아래 안내대로 보내주세요." if row is not None else "🗓️ 아래 안내대로 보내주세요.")


async def r_edit(c: PanelCtx) -> Screen:
    r = await _row(c)
    if r is None:
        return await s_item(c)
    return await _wizard(c, r)


async def r_new(c: PanelCtx) -> Screen:
    return await _wizard(c)


# ── 등록 ──────────────────────────────────────────────────
menu.register_hub(HubItem(50, "sc", "🗓️ 예약공지"))
menu.register_screen("sc", s_list)
menu.register_screen("sci", s_item)
menu.register_route("scp", Route(r_preview))
menu.register_route("sct", Route(r_toggle))
menu.register_route("scd", Route(r_ask_delete))
menu.register_route("sce", Route(r_edit))
menu.register_route("scn", Route(r_new))
menu.register_token_action("del_sc", t_delete, fresh=True)
