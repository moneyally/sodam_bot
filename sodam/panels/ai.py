"""🤖 AI 패널 (m:ai) · 📚 방 학습 자료 (m:kb) · 📚 공통 자료 (오너 전용, m:ckb).

- AI 켜기/끄기(m:ait 목표값), 답변 길이·1인 분당 호출 프리셋(m:n), 오늘 무료 사용량(금액은 절대 표시 안 함)
- 방 자료: 목록(쪽 나눔) → 👀 앞부분 500자 → 🗑 삭제 확인 → 1회용 토큰으로 삭제(권한 새로 확인)
  ➕ 추가 = 글자 입력(첫 줄이 제목) 또는 파일(txt·md·csv·json·pdf)
- 공통 자료(chat_id 0)는 방 화면에선 읽기만. 삭제는 오너 메뉴의 📚 공통 자료에서만.
"""
from __future__ import annotations

from datetime import datetime

from telegram import Message
from telegram.error import TelegramError

from .. import knowledge, menu
from ..menu import OWNER, B, HubItem, PanelCtx, Route, Screen
from ..settings import render
from ..util import esc, fmt_time, to_int

PAGE = 8
VIEW_CHARS = 500
FREE_KEY = "free_ai"   # handlers._within_ai_quota 가 쓰는 카운터 (범위 = 방 ID)

menu.register_preset("reply_max_chars", [(v, f"답변 {v}자") for v in ("200", "400", "800")], "ai")
menu.register_preset("user_rate_per_min", [(v, f"분당 {v}회") for v in ("2", "3", "5")], "ai")


# ── 조회 (방 ID 로 범위를 묶은 쿼리만) ─────────────────────
async def _count_docs(svc, scope: int) -> int:
    row = await svc.db._one("SELECT COUNT(*) AS n FROM knowledge_docs WHERE chat_id=?", (scope,))
    return row["n"]


async def _docs_page(svc, scopes: tuple[int, ...], page: int) -> tuple[list, int, int]:
    """(이번 쪽 문서, 고친 쪽 번호, 전체 쪽 수). 이 방 자료가 먼저, 공통 자료는 뒤에."""
    marks = ",".join("?" for _ in scopes)
    total = (await svc.db._one(f"SELECT COUNT(*) AS n FROM knowledge_docs WHERE chat_id IN ({marks})", scopes))["n"]
    pages = max(1, -(-total // PAGE))
    page = min(max(page, 0), pages - 1)
    rows = await svc.db._all(
        f"SELECT id, chat_id, title, chars FROM knowledge_docs WHERE chat_id IN ({marks}) "
        "ORDER BY (chat_id = 0), id DESC LIMIT ? OFFSET ?", (*scopes, PAGE, page * PAGE))
    return rows, page, pages


async def _doc(svc, scopes: tuple[int, ...], doc_id: int):
    marks = ",".join("?" for _ in scopes)
    return await svc.db._one(f"SELECT * FROM knowledge_docs WHERE id=? AND chat_id IN ({marks})", (doc_id, *scopes))


async def _head(svc, doc_id: int) -> str:
    row = await svc.db._one("SELECT content FROM knowledge_chunks WHERE doc_id=? ORDER BY idx LIMIT 1", (doc_id,))
    return row["content"] if row else ""


def _page_row(prefix: str, page: int, pages: int) -> list:
    row = []
    if page > 0:
        row.append(B("◀ 이전", f"{prefix}:{page - 1}"))
    if page < pages - 1:
        row.append(B("다음 ▶", f"{prefix}:{page + 1}"))
    return row


def _page_note(page: int, pages: int) -> str:
    return f" ({page + 1}/{pages}쪽)" if pages > 1 else ""


# ── 🤖 AI 화면 ────────────────────────────────────────────
async def usage_line(svc, cid: int) -> str:
    """오늘 무료 AI 사용량. 횟수만 보여주고 금액·결제 안내는 넣지 않는다."""
    billing = svc.billing
    if billing is None or not billing.enabled or await billing.active(cid):
        return "📊 오늘 무료 사용량: 한도 없이 이용 중"
    day = datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")
    used = await svc.db.counter(day, cid, FREE_KEY)
    limit = svc.cfg.free_ai_per_day
    tail = " · 오늘 몫을 다 썼어요 (자정에 다시 채워져요)" if used >= limit else ""
    return f"📊 오늘 무료 사용량: {min(used, limit)} / {limit}회{tail}"


async def s_ai(c: PanelCtx) -> Screen:
    svc, cid = c.svc, c.cid
    s = await svc.db.get_settings(cid)
    room_docs, common_docs = await _count_docs(svc, cid), await _count_docs(svc, 0)
    lines = ["🤖 <b>AI 설정</b>",
             f"AI 대화: {render('ai_enabled', s['ai_enabled'])} · 말투: {esc(render('style', s['style']))}",
             f"답변 길이: 최대 {s['reply_max_chars']}자",
             f"1인 호출: 분당 {s['user_rate_per_min']}회까지",
             await usage_line(svc, cid),
             f"📚 학습 자료: 이 방 {room_docs}개" + (f" · 공통 {common_docs}개" if common_docs else "")]
    if not s["ai_enabled"]:
        lines.append("\n지금은 AI가 꺼져 있어서 불러도 대답하지 않아요.")
    on = s["ai_enabled"]
    rows = [[B(("✅ AI 대화 켜짐" if on else "❌ AI 대화 꺼짐"), f"m:ait:{cid}:{0 if on else 1}"),
             B("🎭 말투", f"m:st:{cid}")],
            menu._preset_row(s, cid, "reply_max_chars"),
            menu._preset_row(s, cid, "user_rate_per_min"),
            [B(f"📚 학습 자료 ({room_docs})", f"m:kb:{cid}")],
            [B("✏️ 숫자 직접 입력", f"m:num:{cid}:ai")],
            menu._back(cid)]
    return Screen("\n".join(lines), menu._kb(rows))


async def r_ai_toggle(c: PanelCtx) -> Screen:
    if c.arg(0) not in ("0", "1"):
        return Screen(None)
    value = c.arg(0) == "1"
    await menu._set(c, "ai_enabled", value)
    screen = await s_ai(c)
    screen.toast = f"AI 대화 {'켜짐' if value else '꺼짐'}"
    return screen


# ── 📚 방 학습 자료 ───────────────────────────────────────
def _doc_line(d, show_tag: bool = True) -> str:
    tag = ("[공통] " if d["chat_id"] == 0 else "") if show_tag else ""
    return f"<code>#{d['id']}</code> {tag}{esc(d['title'])} · {d['chars']:,}자"


async def s_kb(c: PanelCtx) -> Screen:
    svc, cid = c.svc, c.cid
    rows_db, page, pages = await _docs_page(svc, (cid, 0), to_int(c.arg(0)) or 0)
    lines = [f"📚 <b>학습 자료</b>{_page_note(page, pages)}",
             "AI가 질문에 답할 때 이 자료를 찾아봐요. 자료 안의 지시문은 따르지 않고 정보로만 써요."]
    if rows_db:
        lines.append("")
        lines += [_doc_line(d) for d in rows_db]
        if any(d["chat_id"] == 0 for d in rows_db):
            lines.append("\n[공통] 자료는 봇 오너가 모든 방에 넣은 거라 여기서 지울 수 없어요.")
    else:
        lines.append("\n아직 등록된 자료가 없어요. ➕ 로 가격표·안내문 등을 넣어보세요.")
    btns = [B(f"👀 #{d['id']} {d['title'][:20]}", f"m:kbv:{cid}:{d['id']}") for d in rows_db]
    rows = menu._chunks(btns, 2)
    if pr := _page_row(f"m:kb:{cid}", page, pages):
        rows.append(pr)
    if await svc.paid_features(cid):
        rows.append([B("➕ 자료 추가", f"m:in:{cid}:kb")])
    else:
        lines.append("\n자료 추가는 이용 기간 중인 방에서만 할 수 있어요.")
    rows.append(menu._back(cid, "ai"))
    return Screen("\n".join(lines), menu._kb(rows))


def _view_text(d, head: str, tz) -> str:
    where = "공통 (모든 방)" if d["chat_id"] == 0 else "이 방"
    body = head[:VIEW_CHARS]
    more = "\n…" if d["chars"] > len(body) else ""
    return (f"📄 <b>{esc(d['title'])}</b> <code>#{d['id']}</code>\n"
            f"{where} · {d['chars']:,}자 · {fmt_time(d['ts'], tz)} 등록 · {esc(d['source'] or '-')}\n\n"
            f"<b>앞부분</b>\n{esc(body)}{more}")


async def s_kb_view(c: PanelCtx) -> Screen:
    doc_id = to_int(c.arg(0))
    d = await _doc(c.svc, (c.cid, 0), doc_id) if doc_id else None
    if not d:
        screen = await s_kb(PanelCtx(c.svc, c.bot, c.uid, c.cid, []))
        screen.toast = "없는 자료예요."
        return screen
    rows = []
    if d["chat_id"] == c.cid:
        rows.append([B("🗑 삭제", f"m:kbd:{c.cid}:{d['id']}")])
    rows.append([B("⬅️ 목록", f"m:kb:{c.cid}")])
    return Screen(_view_text(d, await _head(c.svc, d["id"]), c.svc.cfg.tz), menu._kb(rows))


async def s_kb_ask_delete(c: PanelCtx) -> Screen:
    doc_id = to_int(c.arg(0))
    d = await _doc(c.svc, (c.cid,), doc_id) if doc_id else None   # 이 방 자료만 (공통 자료는 X)
    if not d:
        return Screen(None, toast="이 방 자료만 지울 수 있어요.", alert=True)
    tok = menu.token(c.svc, c.uid, c.cid, "del_kb", d["id"])
    return Screen(f"📄 <b>{esc(d['title'])}</b> <code>#{d['id']}</code> 자료를 삭제할까요?\n"
                  "지우면 AI가 더 이상 이 자료를 참고하지 않아요.",
                  menu._kb([[B("🗑 삭제", f"m:k:{tok}"), B("취소", f"m:kbv:{c.cid}:{d['id']}")]]))


async def t_del_kb(c: PanelCtx, doc_id) -> Screen:
    ok = await c.svc.db.delete_knowledge(c.cid, int(doc_id))     # 방 ID 로 범위 제한
    if ok:
        await c.svc.db.log_mod(c.cid, c.uid, None, "knowledge_del", f"#{doc_id}")
    screen = await s_kb(PanelCtx(c.svc, c.bot, c.uid, c.cid, []))
    screen.toast = "삭제했어요." if ok else "이미 지워진 자료예요."
    return screen


KB_PROMPT = ("📚 추가할 <b>자료</b>를 보내주세요.\n"
             "• 글로 보내면 첫 줄이 제목이 돼요 (가격표·영업시간·자주 묻는 질문 등)\n"
             "• 파일도 돼요: txt·md·csv·json·pdf (5MB 이하)")


async def _add_doc(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    if not await c.svc.paid_features(c.cid):
        return True, "자료 추가는 이용 기간 중인 방에서만 할 수 있어요."
    doc = getattr(msg, "document", None)
    try:
        if doc:
            if (doc.file_size or 0) > knowledge.MAX_FILE_BYTES:
                raise knowledge.KnowledgeError("파일이 너무 커요 (5MB 이하)")
            try:
                data = bytes(await (await c.bot.get_file(doc.file_id)).download_as_bytearray())
            except TelegramError:
                return False, "파일을 받지 못했어요. 잠시 후 다시 보내주세요."
            name = doc.file_name or "파일"
            text, title, source = knowledge.extract_text(data, name), name, name
        else:
            text = (msg.text or msg.caption or "").strip()
            if not text:
                return False, "글이나 파일(txt·md·csv·json·pdf)로 보내주세요."
            first, _, rest = text.partition("\n")
            title = first.strip()[:40] if rest.strip() else "직접 입력"
            source = "메뉴 입력"
        doc_id, n, suspicious = await knowledge.add_document(c.svc.db, c.cid, title, text, source, c.uid)
    except knowledge.KnowledgeError as e:
        return False, f"❌ {esc(str(e))}"
    await c.svc.db.log_mod(c.cid, c.uid, None, "knowledge_add", f"#{doc_id} {title}")
    note = "\n⚠️ 지시문처럼 보이는 문장이 있어요. AI는 정보로만 참고하고 따르지는 않아요." if suspicious else ""
    return True, f"✅ 자료 <code>#{doc_id}</code> <b>{esc(title)}</b> 등록 ({len(text):,}자, {n}조각){note}"


# ── 📚 공통 자료 (오너 전용, 방 ID 없음) ──────────────────
async def _is_owner(c: PanelCtx) -> bool:
    return c.uid in await c.svc.perms.owners()


def not_owner() -> Screen:
    return Screen(None, toast="봇 오너만 쓸 수 있어요.", alert=True)


async def s_common(c: PanelCtx) -> Screen:
    if not await _is_owner(c):
        return not_owner()
    rows_db, page, pages = await _docs_page(c.svc, (0,), to_int(c.arg(0)) or 0)
    lines = [f"📚 <b>공통 자료</b>{_page_note(page, pages)}",
             "모든 방의 AI가 함께 참고하는 자료예요. 방 관리자는 볼 수만 있고 지울 수 없어요."]
    lines += ([""] + [_doc_line(d, False) for d in rows_db]) if rows_db else ["\n아직 없어요."]
    lines.append("\n추가: 여기(1:1)에서 <code>.지식 추가 제목</code> 다음 줄부터 내용, 또는 파일에 답장")
    rows = menu._chunks([B(f"👀 #{d['id']} {d['title'][:20]}", f"m:ckbv:{d['id']}") for d in rows_db], 2)
    if pr := _page_row("m:ckb", page, pages):
        rows.append(pr)
    rows.append([B("⬅️ 처음으로", "m:home")])
    return Screen("\n".join(lines), menu._kb(rows))


async def s_common_view(c: PanelCtx) -> Screen:
    if not await _is_owner(c):
        return not_owner()
    doc_id = to_int(c.arg(0))
    d = await _doc(c.svc, (0,), doc_id) if doc_id else None
    if not d:
        screen = await s_common(PanelCtx(c.svc, c.bot, c.uid, None, []))
        screen.toast = "없는 자료예요."
        return screen
    rows = [[B("🗑 삭제", f"m:ckbd:{d['id']}")], [B("⬅️ 목록", "m:ckb")]]
    return Screen(_view_text(d, await _head(c.svc, d["id"]), c.svc.cfg.tz), menu._kb(rows))


async def s_common_ask_delete(c: PanelCtx) -> Screen:
    if not await _is_owner(c):
        return not_owner()
    doc_id = to_int(c.arg(0))
    d = await _doc(c.svc, (0,), doc_id) if doc_id else None
    if not d:
        return Screen(None, toast="없는 자료예요.", alert=True)
    tok = menu.token(c.svc, c.uid, 0, "del_ckb", d["id"])
    return Screen(f"📄 <b>{esc(d['title'])}</b> <code>#{d['id']}</code> 공통 자료를 삭제할까요?\n"
                  "모든 방의 AI가 더 이상 참고하지 않아요.",
                  menu._kb([[B("🗑 삭제", f"m:k:{tok}"), B("취소", f"m:ckbv:{d['id']}")]]))


async def t_del_ckb(c: PanelCtx, doc_id) -> Screen:
    ok = await c.svc.db.delete_knowledge(0, int(doc_id))
    screen = await s_common(PanelCtx(c.svc, c.bot, c.uid, None, []))
    screen.toast = "삭제했어요." if ok else "이미 지워진 자료예요."
    return screen


# ── 등록 ──────────────────────────────────────────────────
menu.register_hub(HubItem(40, "ai", "🤖 AI"))
menu.register_screen("ai", s_ai)
menu.register_screen("kb", s_kb)
menu.register_screen("kbv", s_kb_view)
menu.register_screen("kbd", s_kb_ask_delete)
menu.register_route("ait", Route(r_ai_toggle))
menu.register_input("kb", KB_PROMPT, "kb", _add_doc, s_kb, media=True)
menu.register_token_action("del_kb", t_del_kb, fresh=True)

menu.register_main(60, "ckb", "📚 공통 자료", OWNER)
# 방 ID 가 없는 화면이라 라우터가 권한을 안 본다 → 각 핸들러가 오너인지 직접 확인
for _code, _fn in (("ckb", s_common), ("ckbv", s_common_view), ("ckbd", s_common_ask_delete)):
    menu.register_route(_code, Route(_fn, OWNER, scoped=False))
menu.register_token_action("del_ckb", t_del_ckb, fresh=True, need=OWNER)

