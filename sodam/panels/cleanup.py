"""🧹 멤버 정리 화면 (관리자 1:1) + 확인 카드 + AI 도구 (동작은 sodam/cleanup.py, 설계 docs/MEMBER_CLEANUP.md).

m:mc:<방> 요약 · mcs 다시 스캔 · mcp:<분류> 미리보기(분류 토글) · mcx:<분류> 전체 명단 CSV(1:1) · mck:<분류> 확인 카드
· mch 중지 · mcg 이어서 · mce 제외 목록. 전부 그 방 관리자 + 텔레그램 '사용자 차단' 권한 (누를 때마다 다시 확인).
확인 카드 = cards.card (menu.lasting_token: 만든 관리자만 · 한 번만 · 10분) → mc_ok 에서 cleanup.start_job 이 조건을 다시 본다.
AI 도구: member_profile·member_cleanup_status (읽기 전용) · member_cleanup (1:1 로 확인 카드까지만 — 실행은 관리자가 누름).
"""
from __future__ import annotations

import csv
import io
import time

from telegram import InputFile
from telegram.error import TelegramError

from .. import cards, cleanup as C, menu, profile as P, tools
from ..menu import B, PanelCtx, Route, Screen, _kb, register_hub, register_route, register_token_action
from ..permissions import Role, may, no_right_text
from ..tools import Tool, ToolCtx
from ..util import esc, fmt_time

CSV_GAP = 60
_csv_last: dict[int, float] = {}
TOGGLES = [("d", "🪦 탈퇴"), ("b", "🤖 봇"), ("i14", "💤 14일"), ("i30", "💤 30일"), ("u", "❔ 모름"), ("f", "👻 가라")]


async def _can(c: PanelCtx) -> Screen | None:
    if not await may(c.svc.perms, c.bot, c.cid, c.uid, "restrict"):
        return Screen(None, toast=no_right_text("restrict"), alert=True)
    return None


async def _job_line(svc, cid: int) -> tuple[str, list]:
    job = await C.get_job(svc.db, cid)
    if not job or job["state"] == "done":
        return "", []
    left = await C.remaining(svc.db, cid)
    if job["state"] == "running":
        return (f"\n⏳ <b>진행 중</b>: 내보냄 {job['kicked']} · 남은 {left}명", [[B("⏸ 중지", f"m:mch:{cid}")]])
    if left:
        return (f"\n⏸ <b>멈춤</b> ({esc(job['why'] or job['state'])}): 내보냄 {job['kicked']} · 남은 {left}명",
                [[B("▶️ 이어서", f"m:mcg:{cid}")]])
    return "", []


async def summary(svc, bot, cid: int, note: str = "") -> Screen:
    """요약 (숫자만 + 분류 버튼). 스캔이 없거나 30분 지났으면 [🔄 스캔]."""
    s = await C.last_scan(svc.db, cid)
    title = await C._title(svc, cid)
    tz = svc.cfg.tz
    job_text, job_rows = await _job_line(svc, cid)
    head = f"🧹 <b>멤버 정리</b> · {esc(C.clip(title, 40))}" + (f"\n{note}" if note else "")
    if not s:
        return Screen(head + "\n\n아직 스캔 안 했어요. 방 참가자 전체를 받아서 탈퇴 계정·봇·잠수·가라 의심을 골라요 (1:1 로만)."
                      + job_text, _kb([[B("🔄 스캔", f"m:mcs:{cid}")], *job_rows, [B("⬅️ 뒤로", f"m:g:{cid}")]]))
    k, p = s["counts"], s["prot"]
    stale = not C.scan_fresh(s)
    lines = [head, f"스캔 {fmt_time(s['ts'], tz)} · 참가자 <b>{s['total']}명</b>"
             + (" <i>(1만 명까지만 받음 — 받은 사람만 대상)</i>" if s["partial"] else ""),
             "🛡 보호(안 내보냄) " + " · ".join(f"{esc(n)} {v}" for n, v in p.items()),
             f"📶 접속 상태: {C.dist_line(s['dist'])}",
             "",
             f"🪦 탈퇴 계정 <b>{k.get('d', 0)}</b> · 🤖 다른 봇 <b>{k.get('b', 0)}</b>",
             f"💤 잠수 14일 <b>{k.get('i14', 0)}</b> · 30일 <b>{k.get('i30', 0)}</b> <i>(정확한 마지막 접속 + 소담 기록 둘 다)</i>",
             f"❔ 접속 모름 {k.get('u', 0)} <i>(숨김인지 오래전인지 구분 불가 — 기본 제외)</i>",
             f"👻 가라 의심 {k.get('f', 0)} <i>(신호 {C.FAKE_MIN}개 이상, 확정 아님 — 기본 제외)</i>"]
    if s.get("rec_since"):
        lines.append(f"<i>소담 기록은 {fmt_time(s['rec_since'], tz, '%y.%m.%d')} 부터 — '글 0개'는 그 뒤 기준이에요.</i>")
    if stale:
        lines.append("⚠️ 스캔이 30분 넘었어요. 내보내려면 다시 스캔해주세요.")
    rows = [] if stale else [[B(f"{label} {k.get(t, 0)}", f"m:mcp:{cid}:{t}") for t, label in TOGGLES[i:i + 2]]
                             for i in range(0, len(TOGGLES), 2)]
    rows += job_rows + [[B("🔄 다시 스캔", f"m:mcs:{cid}"), B("🚫 제외 목록", f"m:mce:{cid}")], [B("⬅️ 뒤로", f"m:g:{cid}")]]
    return Screen("\n".join(lines) + job_text, _kb(rows))


async def s_summary(c: PanelCtx) -> Screen:
    return await _can(c) or await summary(c.svc, c.bot, c.cid)


async def r_scan(c: PanelCtx) -> Screen:
    if (deny := await _can(c)) is not None:
        return deny
    s, err = await C.scan(c.svc, c.bot, c.cid, c.uid)
    if s is None:
        return Screen(None, toast=err[:190], alert=True)
    screen = await summary(c.svc, c.bot, c.cid)
    screen.toast = "10분 안에 스캔한 결과예요 (방마다 10분에 1번)." if s.get("reused") else "새로 스캔했어요."
    return screen


def _toggle(sel: list[str], t: str) -> list[str]:
    if t in sel:
        return [x for x in sel if x != t]
    if t[0] == "i":
        sel = [x for x in sel if x[0] != "i"]   # 잠수는 하나만
    return C.parse_sel(C.sel_str(sel + [t]))


async def _counts(svc, cid: int, toks: list[str]) -> dict[str, int]:
    """분류마다 (지금 제외·자유 멤버 뺀) 인원 — 토글 버튼 숫자."""
    skip = await C.excluded_ids(svc.db, cid) | await C.free_ids(svc.db, cid)
    now, out = time.time(), {t: 0 for t in toks}
    for r in await svc.db._all("SELECT * FROM cleanup_cands WHERE chat_id=?", (cid,)):
        if r["user_id"] not in skip:
            for t in toks:
                if C.cats_of(r, [t], now):
                    out[t] += 1
    return out


async def preview(svc, bot, cid: int, sel: list[str]) -> Screen:
    s = await C.last_scan(svc.db, cid)
    if not C.scan_fresh(s):
        return Screen("🧹 스캔이 없거나 30분이 지났어요. 다시 스캔해주세요.", _kb([[B("🔄 다시 스캔", f"m:mcs:{cid}")],
                                                                         [B("⬅️ 요약", f"m:mc:{cid}")]]))
    rows = await C.selection(svc, cid, sel)
    tz = svc.cfg.tz
    per = {t: sum(1 for r in rows if t in r["cats"]) for t in sel}
    try:
        now_n = await bot.get_chat_member_count(cid)
    except TelegramError:
        now_n = s["total"]
    title = await C._title(svc, cid)
    lines = [f"🧹 <b>미리보기</b> · {esc(C.clip(title, 40))}",
             "고른 분류: " + (" · ".join(f"{C.cat_label(t)} {per[t]}" for t in sel) if sel else "(없음 — 아래에서 골라주세요)"),
             f"내보낼 사람 <b>{len(rows)}명</b> · 방 인원 약 {now_n} → {max(0, now_n - len(rows))}"]
    for r in rows[:C.PREVIEW_NAMES]:
        why = "·".join(C.cat_label(t).split(" ", 1)[1] if t[0] != "i" else f"잠수{t[1:]}" for t in r["cats"])
        lines.append(f"• {C.name_html(r)} · {esc(why)} · {esc(C.status_text(r['status'], r['was_online'], tz))}")
    if len(rows) > C.PREVIEW_NAMES:
        lines.append(f"… 외 {len(rows) - C.PREVIEW_NAMES}명 (📄 CSV 로 전체)")
    if "f" in sel:
        lines.append("<i>👻 가라 의심은 기록된 신호 개수일 뿐 확정이 아니에요.</i>")
    lines.append("<i>보호(관리자·오너·봇 관리자·소담·자유 멤버·제외 명단·최근 14일 글)는 빠져 있고, 실행 때 한 명씩 다시 확인해요.</i>")
    idle_now = next((t for t in sel if t[0] == "i"), None)
    opts = TOGGLES + ([(idle_now, f"💤 {idle_now[1:]}일")] if idle_now and idle_now not in ("i14", "i30") else [])
    avail = await _counts(svc, cid, [t for t, _ in opts])
    # 걸리는 사람이 있는 분류(또는 이미 고른 것)만 버튼 — 0명짜리 토글은 누를 일이 없음
    toggles = [B(("✅ " if t in sel else "☐ ") + f"{label} {avail.get(t, 0)}", f"m:mcp:{cid}:{C.sel_str(_toggle(sel, t)) or '-'}")
               for t, label in opts if t in sel or avail.get(t)]
    kb = [toggles[i:i + 2] for i in range(0, len(toggles), 2)]
    if rows:
        kb += [[B("📄 전체 명단 CSV", f"m:mcx:{cid}:{C.sel_str(sel)}")],
               [B(f"🧹 {len(rows)}명 내보내기", f"m:mck:{cid}:{C.sel_str(sel)}")]]
    kb.append([B("⬅️ 요약", f"m:mc:{cid}")])
    return Screen("\n".join(lines), _kb(kb))


async def s_preview(c: PanelCtx) -> Screen:
    return await _can(c) or await preview(c.svc, c.bot, c.cid, C.parse_sel(c.arg(0)))


async def r_csv(c: PanelCtx) -> Screen:
    if (deny := await _can(c)) is not None:
        return deny
    sel = C.parse_sel(c.arg(0))
    if not C.scan_fresh(await C.last_scan(c.svc.db, c.cid)) or not sel:
        return Screen(None, toast="스캔을 다시 해주세요.", alert=True)
    now = time.time()
    if now - _csv_last.get(c.uid, 0) < CSV_GAP:
        return Screen(None, toast="CSV 는 1분에 한 번만 받을 수 있어요.", alert=True)
    _csv_last[c.uid] = now
    from .members import _cell   # 엑셀 수식 주입 막기 (이름은 멤버가 마음대로 정함)
    rows = await C.selection(c.svc, c.cid, sel)
    tz = c.svc.cfg.tz
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "name", "username", "categories", "status", "last_online", "last_activity_here", "messages_seen"])
    for r in rows:
        w.writerow([r["user_id"], _cell(r["name"] or ""), _cell(r["username"] or ""),
                    " ".join(C.REASON[t[0]] + (t[1:] if t[0] == "i" else "") for t in r["cats"]), r["status"],
                    fmt_time(r["was_online"], tz, "%Y-%m-%d %H:%M") if r["was_online"] else "",
                    fmt_time(r["last_act"], tz, "%Y-%m-%d %H:%M") if r["last_act"] else "", r["msgs"]])
    data = ("﻿" + buf.getvalue()).encode("utf-8")
    await c.bot.send_document(c.uid, InputFile(io.BytesIO(data), filename=f"cleanup_{c.cid}.csv"),
                              caption=f"🧹 멤버 정리 후보 {len(rows)}명 (1:1 에만 보냄)")
    await c.svc.db.log_mod(c.cid, c.uid, None, "export", f"cleanup csv {len(rows)}")
    return Screen(None, toast=f"1:1 로 CSV 를 보냈어요 ({len(rows)}명).")


async def send_card(svc, bot, cid: int, uid: int, sel: list[str]) -> tuple[bool, str]:
    """관리자 1:1 에 확인 카드 (만든 사람만·한 번만·10분). 방엔 안 올림."""
    s = await C.last_scan(svc.db, cid)
    if not C.scan_fresh(s):
        return False, "스캔이 없거나 30분이 지났어요. 다시 스캔해주세요."
    rows = await C.selection(svc, cid, sel)
    if not rows:
        return False, "고른 분류에 내보낼 사람이 없어요."
    job = await C.get_job(svc.db, cid)
    if job and job["state"] == "running":
        return False, "이 방은 이미 멤버 정리가 진행 중이에요."
    try:
        now_n = await bot.get_chat_member_count(cid)
    except TelegramError:
        now_n = s["total"]
    title = await C._title(svc, cid)
    kb = await cards.card(svc, uid, cid, "member_cleanup", "mc_ok", "mc_no", {"sel": C.sel_str(sel), "scan": s["ts"]},
                          ok_label=f"🧹 {len(rows)}명 내보내기", ttl=C.CARD_TTL)
    text = (f"⚠️ <b>정말 내보낼까요?</b> · {esc(C.clip(title, 40))}\n"
            f"분류: {esc(', '.join(C.cat_label(t) for t in sel))} · <b>{len(rows)}명</b>\n"
            f"방 인원 약 {now_n} → {max(0, now_n - len(rows))}\n"
            f"한 명씩 {C.KICK_GAP:g}초 간격으로 내보내요 (밴 아님 — 다시 들어올 수 있어요). 실행 때 사람마다 아직 멤버인지·"
            "관리자·보호 대상이 아닌지 다시 확인해요.\n<i>요청한 관리자만 · 한 번만 · 10분 뒤 만료</i>")
    try:
        await bot.send_message(uid, text, parse_mode="HTML", reply_markup=kb)
    except TelegramError:
        return False, "1:1 로 확인 카드를 못 보냈어요. 소담과 1:1 대화를 먼저 시작해주세요."
    await svc.db.audit(cid, uid, None, "ask_cleanup", f"{len(rows)}명 {C.sel_str(sel)}")
    return True, f"1:1 로 확인 카드를 보냈어요 ({len(rows)}명)."


async def r_ask(c: PanelCtx) -> Screen:
    if (deny := await _can(c)) is not None:
        return deny
    ok, text = await send_card(c.svc, c.bot, c.cid, c.uid, C.parse_sel(c.arg(0)))
    return Screen(None, toast=text, alert=not ok)


async def t_ok(c: PanelCtx, spec) -> Screen:
    if not isinstance(spec, dict) or not await cards.claim(c.svc, spec, "ok"):
        return Screen(None, toast=cards.ALREADY, alert=True)
    ok, text = await C.start_job(c.svc, c.bot, c.cid, c.uid, C.parse_sel(str(spec.get("sel", ""))), int(spec.get("scan") or 0))
    await cards.record(c.svc, c.cid, c.uid, "member_cleanup", ("✅ " if ok else "❌ ") + text[:120])
    await c.svc.db.audit(c.cid, c.uid, None, "press_cleanup", "시작" if ok else f"거절: {text[:80]}")
    kb = _kb([[B("⏸ 중지", f"m:mch:{c.cid}")]]) if ok else _kb([[B("🧹 멤버 정리", f"m:mc:{c.cid}")]])
    return Screen(("🧹 " if ok else "❌ ") + text, kb)


async def t_no(c: PanelCtx, spec) -> Screen:
    if not await cards.claim(c.svc, spec, "no"):
        return Screen(None, toast=cards.ALREADY, alert=True)
    await c.svc.db.audit(c.cid, c.uid, None, "press_cleanup", "취소")
    return Screen("❌ 멤버 정리를 취소했어요.", _kb([[B("🧹 멤버 정리", f"m:mc:{c.cid}")]]))


async def r_stop(c: PanelCtx) -> Screen:
    if (deny := await _can(c)) is not None:
        return deny
    ok = await C.stop_job(c.svc, c.cid, c.uid)
    screen = await summary(c.svc, c.bot, c.cid)
    screen.toast = "멈출게요 (지금 사람까지 하고 멈춰요)." if ok else "진행 중인 작업이 없어요."
    return screen


async def r_resume(c: PanelCtx) -> Screen:
    if (deny := await _can(c)) is not None:
        return deny
    ok, text = await C.resume_job(c.svc, c.bot, c.cid, c.uid)
    screen = await summary(c.svc, c.bot, c.cid)
    screen.toast, screen.alert = text, not ok
    return screen


async def s_excl(c: PanelCtx) -> Screen:
    if (deny := await _can(c)) is not None:
        return deny
    rows = await C.excluded_rows(c.svc.db, c.cid)
    lines = ["🚫 <b>멤버 정리 제외 목록</b> (절대 안 내보냄)",
             "추가: 방에서 <code>.멤버정리 제외 @아이디</code> (답장·ID 도 돼요)"]
    lines += [f"{i + 1}. {esc(C.clip(' '.join(x for x in (r['first_name'], r['last_name']) if x) or str(r['user_id']), 24))}"
              + (f" @{esc(r['username'])}" if r["username"] else "") + f" <code>{r['user_id']}</code>"
              for i, r in enumerate(rows[:30])] or ["(없음)"]
    btns = [B(f"❌ {i + 1}", f"m:k:{menu.token(c.svc, c.uid, c.cid, 'mc_unex', r['user_id'], menu.LIST_TOKEN_TTL)}")
            for i, r in enumerate(rows[:30])]
    return Screen("\n".join(lines), _kb([btns[i:i + 5] for i in range(0, len(btns), 5)] + [[B("⬅️ 요약", f"m:mc:{c.cid}")]]))


async def t_unexclude(c: PanelCtx, uid) -> Screen:
    if (deny := await _can(c)) is not None:
        return deny
    await C.exclude(c.svc.db, c.cid, int(uid), c.uid, False)
    screen = await s_excl(c)
    screen.toast = "제외 목록에서 뺐어요."
    return screen


register_hub(menu.HubItem(71, "mc", "🧹 멤버 정리"))
register_route("mc", Route(s_summary))
register_route("mcs", Route(r_scan, fresh=True))
register_route("mcp", Route(s_preview))
register_route("mcx", Route(r_csv, fresh=True))
register_route("mck", Route(r_ask, fresh=True))
register_route("mch", Route(r_stop, fresh=True))
register_route("mcg", Route(r_resume, fresh=True))
register_route("mce", Route(s_excl))
register_token_action("mc_ok", t_ok, fresh=True)
register_token_action("mc_no", t_no)
register_token_action("mc_unex", t_unexclude, fresh=True)


# ── AI 도구 (관리자·그룹방) ─────────────────────────────────
async def t_member_profile(ctx: ToolCtx, a: dict) -> str:
    row, err = await tools._resolve(ctx, str(a.get("name", "")))
    if err:
        return err
    if not P.allow(ctx.caller.id):
        return "프로필 조회가 너무 잦음. 1분 뒤 다시."
    p = await P.gather(ctx.svc, ctx.bot, ctx.chat_id, row["user_id"], row["username"] or "")
    ctx.tainted = True   # 소개글·이름 = 그 사람이 쓴 글 (숨은 지시가 같은 답변의 쓰기 도구로 이어지지 않게)
    return P.card_text(row["user_id"], p, ctx.svc.cfg.tz)


async def t_cleanup_status(ctx: ToolCtx, a: dict) -> str:
    s = await C.last_scan(ctx.svc.db, ctx.chat_id)
    job = await C.get_job(ctx.svc.db, ctx.chat_id)
    out = []
    if s:
        k = s["counts"]
        out.append(f"마지막 스캔 {fmt_time(s['ts'], ctx.svc.cfg.tz)} 참가자 {s['total']}명"
                   + (" (1만 명에서 잘림)" if s["partial"] else "")
                   + f" · 탈퇴 {k.get('d', 0)} · 봇 {k.get('b', 0)} · 잠수14일 {k.get('i14', 0)} · 잠수30일 {k.get('i30', 0)}"
                   f" · 접속 모름 {k.get('u', 0)} · 가라 의심 {k.get('f', 0)} (확정 아님) · 보호 {sum(s['prot'].values())}명"
                   f" · 접속 상태 {C.dist_line(s['dist'])}")
    else:
        out.append("아직 스캔 기록 없음 (관리자가 .멤버정리 로 스캔).")
    if job:
        out.append(f"작업 {job['state']}: 내보냄 {job['kicked']} · 이미 나감 {job['gone']} · 실패 {job['failed']} · "
                   f"남은 {await C.remaining(ctx.svc.db, ctx.chat_id)}명")
    return " / ".join(out) + " (명단은 관리자 1:1 화면에서만)"


CAT_ARG = {"deleted": "d", "bot": "b", "idle": "i", "unknown": "u", "fake": "f"}


async def t_member_cleanup(ctx: ToolCtx, a: dict) -> str:
    """스캔 → 요청한 관리자 1:1 에 미리보기 + 확인 카드까지만. 실행은 관리자가 카드를 눌러야."""
    if not await may(ctx.svc.perms, ctx.bot, ctx.chat_id, ctx.caller.id, "restrict"):
        return "텔레그램 '사용자 차단' 권한이 있는 관리자만 멤버 정리를 할 수 있음."
    cats = [str(x) for x in (a.get("categories") or []) if str(x) in CAT_ARG]
    if not cats:
        return "categories 에 deleted/bot/idle/unknown/fake 중 하나 이상."
    days = max(1, min(int(a.get("idle_days") or 30), C.IDLE_MAX))
    sel = C.parse_sel(".".join(CAT_ARG[x] + (str(days) if x == "idle" else "") for x in cats))
    s, err = await C.scan(ctx.svc, ctx.bot, ctx.chat_id, ctx.caller.id)
    if s is None:
        return err
    ok, text = await send_card(ctx.svc, ctx.bot, ctx.chat_id, ctx.caller.id, sel)
    if not ok:
        return text
    return text + " 아직 실행된 게 아님 — 관리자가 1:1 카드를 눌러야 시작된다고 짧게 안내할 것 (방에 명단 말하지 말 것)."


tools.register_tool(Tool(
    "member_profile", "멤버 한 명의 텔레그램 프로필(소개글·접속 상태·프사·프리미엄·공통 방·계정 생성 추정) + 이 방 기록 (관리자).",
    {"name": {"type": "string", "description": "@username, 이름, 또는 숫자 ID"}}, ["name"], t_member_profile,
    Role.ADMIN, where="room"), read_only=True)
tools.register_tool(Tool(
    "member_cleanup_status", "멤버 정리 마지막 스캔 요약(분류별 인원·접속 상태 분포, 숫자만)과 진행 중 작업 (관리자).",
    {}, [], t_cleanup_status, Role.ADMIN, where="room"), read_only=True)
tools.register_tool(Tool(
    "member_cleanup", "탈퇴 계정·다른 봇·잠수·접속 모름·가라 의심 멤버를 스캔해서 요청한 관리자 1:1 에 확인 카드를 보냄 "
    "(내보내기는 관리자가 카드를 눌러야). 보호 대상(관리자·최근 14일 글 등)은 자동 제외.",
    {"categories": {"type": "array", "items": {"type": "string", "enum": list(CAT_ARG)}},
     "idle_days": {"type": "integer", "description": "잠수 기준 일수 (idle 일 때, 기본 30)"}},
    ["categories"], t_member_cleanup, Role.ADMIN, where="room"))


# ── 뒤에서: 죽은 작업 이어 받기(30초 틱) · 종료 때 멈춤 (DB 엔 '진행 중' → 다음 시작 때 __main__ 이 이어서) ─
from .. import casino as _casino, hooks as _hooks  # noqa: E402

_hooks.add_tick_hook(C.tick)
if C.shutdown not in _casino.SHUTDOWN_HOOKS:
    _casino.SHUTDOWN_HOOKS.append(C.shutdown)
