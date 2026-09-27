"""🗓️ 예약공지 패널 (그룹 허브 → m:sc).

m:sc:<방>            목록 (🟢 켜짐 / ⏸ 꺼짐 · 언제 · 제목) + ➕ 새로 만들기
m:sci:<방>:<번호>    항목: 👀 미리보기 · ✏️ 수정 · 켜기/끄기 · 🗑 삭제
m:scp / m:sce / m:sct:<방>:<번호>:<0|1> / m:scd   미리보기 · 수정 · 켜기/끄기(목표값) · 삭제 확인(1회용 토큰)
m:scn:<방>           새로 만들기 (방당 announce.MAX_PER_CHAT 개)
m:scs:<방>           🤖 AI 작업 종류 고르기 → m:in:<방>:cr<스킬> / ⏰ 알람 m:in:<방>:crremind ('<언제> | <내용>' 한 줄)
m:scx:<방>:<번호>[:<대상방>|all]   이 예약을 내가 관리자인 다른 방에도 (대상마다 관리자 확인)
AI 에게 말로 예약('내일 9시에 알려줘')하면 방에 확인 카드 → 토큰 cron_save / cron_no.

번호로 찾을 때는 항상 get_schedule(방, 번호) — 다른 방의 번호를 버튼에 넣어 보내도 못 건드린다.
만들기·수정은 announce.Announcer 마법사를 1:1 에서 돌린다 (대화는 1:1, 공지는 그 방에).
"""
from __future__ import annotations

from datetime import datetime

from telegram.error import TelegramError

from .. import cards, cron, menu
from ..announce import CLOSE_KB, MAX_PER_CHAT, MEDIA_LABEL, describe_when, parse_time
from ..menu import CID_RE, B, HubItem, PanelCtx, Route, Screen
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
    if row["action"] != "post":
        return " " + cron.describe(row).split()[0]
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
        lines.append("\n아직 등록된 공지가 없어요. <b>➕ 새 공지</b>를 누르면 제목·내용·시간을 차례로 물어봐요. "
                     "⏰ 알람·🤖 AI 작업(대화 요약·검색 소식·통계·글쓰기)도 정한 시각에 올릴 수 있어요.")
    if not await c.svc.paid_features(c.cid):
        lines.append("\n⚠️ 이용 기간이 끝나서 지금은 공지가 올라가지 않아요.")
    btns = [[B(f"{'🟢' if r['enabled'] else '⏸'} {_when(r)}{_flags(r)} · {_name(r)[:24]}", f"m:sci:{c.cid}:{r['id']}")]
            for r in rows]
    btns.append([B("➕ 새 공지", f"m:scn:{c.cid}"), B("⏰ 알람", f"m:in:{c.cid}:crremind"),
                 B("🤖 AI 작업", f"m:scs:{c.cid}")])
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
    if r["action"] != "post":   # 알람·AI 작업: 제목·사진 대신 종류·지시 (수정은 지우고 다시 만들기)
        lines[0] = f"{cron.describe(r)} <b>#{sid}</b>"
        lines[3] = "종류: " + cron.describe(r)
        lines[4] = f"최근 실행: {datetime.fromtimestamp(r['last_sent'], tz):%m/%d %H:%M}" if r["last_sent"] else "최근 실행: 아직 없음"
    toggle = (B("⏸ 끄기", f"m:sct:{c.cid}:{sid}:0") if r["enabled"] else B("▶️ 켜기", f"m:sct:{c.cid}:{sid}:1"))
    first = [B("👀 미리보기", f"m:scp:{c.cid}:{sid}")] + ([B("✏️ 수정", f"m:sce:{c.cid}:{sid}")] if r["action"] == "post" else [])
    kb = menu._kb([first, [toggle, B("🗑 삭제", f"m:scd:{c.cid}:{sid}")],
                   [B("📤 다른 방에도", f"m:scx:{c.cid}:{sid}"), B("⬅️ 목록", f"m:sc:{c.cid}")]])
    return Screen("\n".join(lines), kb)


# ── 동작 ──────────────────────────────────────────────────
async def r_preview(c: PanelCtx) -> Screen:
    r = await _row(c)
    if r is None:
        return await s_item(c)
    if r["action"] != "post":   # 알람 문구 / AI 작업을 지금 한 번 돌려 1:1 로 (방엔 안 올림)
        try:
            out = r["text"] if r["action"] == "remind" else await cron.run_skill(c.svc, r)
            await c.bot.send_message(c.uid, f"👀 미리보기 ({cron.describe(r)})\n\n{esc(out or '(결과 없음)')[:3500]}",
                                     parse_mode="HTML", reply_markup=CLOSE_KB)
        except Exception as e:   # AI 한도·연결 오류도 토스트로
            return Screen(None, toast=f"미리보기 실패: {str(e)[:80]}", alert=True)
        return Screen(None, toast="👀 아래에 미리보기를 보냈어요.")
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


# ── 알람·AI 작업 (sodam/cron.py) ──────────────────────────
async def s_skills(c: PanelCtx) -> Screen:
    lines = ["🤖 <b>AI 작업</b> — 정한 시각에 소담이 방에 올려요.",
             "대화를 읽는 작업(요약)도 AI 에겐 다른 행동 권한이 없어서 대화 속 이상한 지시는 따르지 않아요.", ""]
    lines += [f"{s.label}: {esc(s.need_text)}" for s in cron.SKILLS.values()]
    rows = menu._chunks([B(s.label, f"m:in:{c.cid}:cr{k}") for k, s in cron.SKILLS.items()], 2)
    return Screen("\n".join(lines), menu._kb(rows + [[B("⬅️ 뒤로", f"m:sc:{c.cid}")]]))


async def _save(c: PanelCtx, action: str, skill: str | None, when_raw: str, text: str) -> tuple[bool, str]:
    try:
        when = parse_time(when_raw, c.svc.cfg.tz)
    except ValueError as e:
        return False, f"⏱ {e}"
    if action == "ai" and when[0] == "interval" and when[2] < 60:
        return False, "AI 작업은 1시간 이상 간격으로만 반복할 수 있어요."
    if len(await c.svc.db.schedules(c.cid)) >= MAX_PER_CHAT:
        return False, f"예약은 방마다 {MAX_PER_CHAT}개까지예요."
    [sid] = await cron.create(c.svc, [c.cid], uid=c.uid, when=when, action=action, skill=skill, text=text[:500])
    return True, f"✅ 예약했어요 (#{sid} · {describe_when(*when[:3])})"


def _input(action: str, skill: str | None):
    async def handle(c: PanelCtx, msg) -> tuple[bool, str]:
        when_raw, _, text = (msg.text or "").partition("|")
        if not text.strip() and skill != "stats":
            return False, "<code>언제 | 내용</code> 형식으로 보내주세요. 예: <code>매일 22:00 | 핵심 3줄로</code>"
        return await _save(c, action, skill, when_raw.strip(), text.strip())
    return handle


async def save_cron(svc, cid: int, uid: int, spec: dict) -> tuple[bool, str]:
    """말로 한 예약 저장 (확인 카드 [✅ 예약] · 오늘은 확인 생략). (성공?, 안내 HTML)."""
    if len(await svc.db.schedules(cid)) >= MAX_PER_CHAT:
        return False, f"예약은 방마다 {MAX_PER_CHAT}개까지예요. 관리자 1:1 메뉴 🗓️ 에서 정리해 주세요."
    [sid] = await cron.create(svc, [cid], uid=uid, when=tuple(spec["when"]), action=spec["action"],
                              skill=spec["skill"], text=spec["text"], title=spec["title"],
                              deliver=spec.get("deliver", "room"))
    return True, f"✅ 예약했어요 (#{sid} · {describe_when(*spec['when'][:3])} · {esc(spec['text'][:60])})"


def cron_line(ok: bool, spec: dict, html: str) -> str:
    """AI 맥락용 결과 한 줄 (HTML 없이)."""
    when = describe_when(*spec["when"][:3])
    return f"✅ 예약 저장됨: {when} {spec['text'][:40]}" if ok else f"⚠️ 예약 못 함: {html[:60]}"


async def t_cron_save(c: PanelCtx, spec) -> Screen:
    """말로 한 예약의 확인 카드 [✅ 예약] / [✅ + 오늘은 확인 생략] (토큰 = 요청한 관리자만, 카드당 한 번)."""
    if not await cards.claim(c.svc, spec, "day" if spec.get("day") else "ok"):
        return Screen(None, toast=cards.ALREADY)
    ok, text = await save_cron(c.svc, c.cid, c.uid, spec)
    await cards.pressed(c.svc, c.cid, c.uid, "schedule_task", spec, cron_line(ok, spec, text), done=ok)
    if not ok:
        return Screen(text, None)
    return Screen(text + "\n끄기·삭제는 관리자 1:1 메뉴 🗓️ 예약공지에서" + cards.day_note(spec), None, toast="예약했어요")


async def t_cron_no(c: PanelCtx, spec) -> Screen:
    if not await cards.claim(c.svc, spec, "no"):
        return Screen(None, toast=cards.ALREADY)
    await cards.pressed(c.svc, c.cid, c.uid, "schedule_task", spec, "❌ 예약 취소 (저장 안 함)", done=False)
    return Screen("예약하지 않았어요.", None)


async def r_copy(c: PanelCtx) -> Screen:
    """이 예약(공지·알람·AI 작업)을 내가 관리자인 다른 방에도. 대상마다 관리자 확인."""
    r = await _row(c)
    if r is None:
        return await s_item(c)
    groups = [(g, t) for g, t in await menu.admin_groups(c.svc, c.bot, c.uid) if g != c.cid]
    target = c.arg(1)
    if not target:
        rows = [[B(f"➡️ {t[:28]}", f"m:scx:{c.cid}:{r['id']}:{g}")] for g, t in groups[:15]]
        rows += [[B(f"📤 전부 ({len(groups)}개 방)", f"m:scx:{c.cid}:{r['id']}:all")]] if len(groups) > 1 else []
        return Screen(f"📤 <b>#{r['id']} 을 어느 방에도 만들까요?</b>" if groups else "관리 중인 다른 방이 없어요.",
                      menu._kb(rows + [[B("⬅️ 뒤로", f"m:sci:{c.cid}:{r['id']}")]]))
    mine = {g for g, _ in groups}
    ids = [g for g in (mine if target == "all" else [int(target)] if CID_RE.fullmatch(target) else []) if g in mine]
    ids = [g for g in ids if len(await c.svc.db.schedules(g)) < MAX_PER_CHAT]
    if not ids:
        return Screen(None, toast="만들 수 있는 방이 없어요 (관리 중인 방이 아니거나 예약이 가득).", alert=True)
    for g in ids:
        sid = await c.svc.db.add_schedule(g, kind=r["kind"], at_time=r["at_time"], interval_min=r["interval_min"],
                                          title=r["title"], text=r["text"], media_type=r["media_type"],
                                          media_id=r["media_id"], pin=bool(r["pin"]), created_by=c.uid,
                                          action=r["action"], skill=r["skill"], at_ts=r["at_ts"], deliver=r["deliver"])
        await c.svc.db.log_mod(g, c.uid, None, "schedule", f"#{sid} ← {c.cid}#{r['id']} 복사")
    screen = await s_item(c)
    screen.toast = f"📤 {len(ids)}개 방에 만들었어요"
    return screen


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
menu.register_screen("scs", s_skills)
menu.register_route("scx", Route(r_copy))
menu.register_token_action("cron_save", t_cron_save, fresh=True)
menu.register_token_action("cron_save" + cards.DAY, t_cron_save, fresh=True)
menu.register_token_action("cron_no", t_cron_no)
WHEN_HELP = ("\n\n<b>언제</b>: <code>매일 09:00</code> · <code>반복 2시간</code> · <code>30분 뒤</code> · "
             "<code>내일 09:00</code> · <code>09-28 21:00</code>")
menu.register_input("crremind", "⏰ <code>언제 | 알람 문구</code> 한 줄로 보내주세요. 예: <code>내일 09:00 | 회의 시작</code>"
                    + WHEN_HELP, "sc", _input("remind", None), s_list)
menu.register_route("crremind", Route(s_list))       # 입력 화면의 [⬅️ 메뉴로] (m:<입력종류>:<방>)
for _k, _s in cron.SKILLS.items():
    menu.register_input(f"cr{_k}", f"{_s.label}\n<code>언제 | {esc(_s.need_text)}</code> 한 줄로 보내주세요." + WHEN_HELP,
                        "scs", _input("ai", _k), s_list)
    menu.register_route(f"cr{_k}", Route(s_list))
