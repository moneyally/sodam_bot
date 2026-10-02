"""✏️ 인사 편집기 (그룹헬프식): 인사말 · 미디어(사진/영상/GIF) · URL 버튼을 각각 수정·보기·삭제, 전체 미리보기.

m:w:<방>            편집기 (그룹 허브에서)
m:wt / m:wm / m:wb  인사말 · 미디어 · URL 버튼 보기 (글자 입력 kind 이름과 같아서 입력 뒤 '⬅️ 메뉴로' 도 여기로)
m:in:<방>:wt|wm|wb  새로 입력 (미디어는 사진·영상·GIF 만 온 메시지도 받음)
m:wd:<방>:t|m|b     삭제 확인 → 1회용 토큰(m:k, 권한 새로 확인)
m:wv:<방>:m|all     1:1 로 새 메시지 미리보기 + [🗑 닫기](토큰, 누르면 그 메시지 삭제)

실제 인사는 sodam/greet.py 의 send_greeting 이 보낸다 (미리보기도 같은 함수 → 보이는 그대로 나감).
인사말은 esc() 하고 {names} 자리에만 코드가 멘션을 넣는다. 이름은 AI에게 가지 않는다.
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.error import TelegramError

from .. import menu
from ..greet import (FALLBACKS, MAX_BUTTONS, MAX_TEMPLATE, MEDIA_TYPES, button_rows, clean_buttons, fill,
                     media_of, parse_buttons, send_greeting, with_names)
from ..menu import B, HubItem, PanelCtx, Route, Screen
from ..util import esc, josa, mention, user_name

PARTS = {"t": "📄 인사말", "m": "🖼 미디어", "b": "🔗 URL 버튼"}
MEDIA_OBJ = {"photo": "사진을", "video": "영상을", "animation": "GIF를"}
CLOSE_TTL = 48 * 3600    # 봇은 48시간 지난 메시지를 못 지운다
menu.register_toggle("greet_mention", "w")   # 🏷 이름 태그 켜기/끄기 → 편집기로 다시
menu.register_toggle("greet_reply_bot", "w")  # 🤝 다른 봇 글에 답장으로 인사


def _kb(rows):
    return InlineKeyboardMarkup([r for r in rows if r])


def _back(cid: int) -> list[InlineKeyboardButton]:
    return [B("⬅️ 인사 편집기", f"m:w:{cid}")]


async def _save(c: PanelCtx, detail: str, **values) -> None:
    """바뀐 값만 저장하고 관리 기록에 남긴다."""
    s = await c.svc.db.get_settings(c.cid)
    changed = False
    for key, value in values.items():
        if s.get(key) != value:
            await c.svc.db.set_setting(c.cid, key, value)
            changed = True
    if changed:
        await c.svc.db.log_mod(c.cid, c.uid, None, "setting", detail)


def _has(s: dict, part: str) -> bool:
    return bool({"t": s["greet_template"], "m": media_of(s), "b": clean_buttons(s["greet_buttons"])}[part])


# ── 화면 ─────────────────────────────────────────────────
async def s_editor(c: PanelCtx) -> Screen:
    s = await c.svc.db.get_settings(c.cid)
    tpl, media, btns = s["greet_template"], media_of(s), clean_buttons(s["greet_buttons"])
    lines = ["✏️ <b>인사 편집기</b>",
             "새 멤버가 들어오면 (캡차가 켜져 있으면 통과한 뒤) 이렇게 인사해요.",
             "입장 인사: " + ("✅ 켜짐" if s["greet_enabled"] else "❌ 꺼짐 — 🚪 입장·인사에서 켜야 인사해요"), ""]
    if tpl:
        short = tpl if len(tpl) <= 40 else tpl[:40] + "…"
        lines.append(f"📄 인사말: <i>{esc(short)}</i>")
    else:
        lines.append("📄 인사말: 비어 있음 → AI가 매번 새로 써요")
    lines.append(f"🖼 미디어: {MEDIA_TYPES[media[0]] if media else '없음'}")
    lines.append(f"🔗 URL 버튼: {len(btns)}개" if btns else "🔗 URL 버튼: 없음")
    lines.append("🏷 이름 태그: " + ("켜짐 — 새 멤버 이름을 멘션(파란 글씨)으로" if s["greet_mention"]
                                    else "꺼짐 — 멘션 없이 (인사말에 {names} 가 없으면 이름도 안 붙여요)"))
    if s["greet_reply_bot"]:
        lines.append("🤝 봇 글에 답장: 켜짐 — ✅ 믿는 봇이 방금 올린 글(환영 글 등)에 답장으로 인사해요. 그 봇이 인사말을 명령처럼 받아요. "
                     "그 봇이 봇 글을 무시하면, 사람이 같은 말을 쳤을 때 그 봇이 올렸던 답 글을 소담이 복사해 올려요"
                     + ("" if s["botlink_mode"] != "off" else "\n⚠️ 🤝 다른 봇 연동이 꺼져 있어서 지금은 그냥 인사해요"))
    cid = c.cid
    rows = [[B("📄 인사말 수정" if tpl else "📄 인사말 쓰기", f"m:in:{cid}:wt"), B("👀 보기", f"m:wt:{cid}")]
            + ([B("🗑 삭제", f"m:wd:{cid}:t")] if tpl else []),
            [B("🖼 미디어 바꾸기" if media else "🖼 미디어 추가", f"m:in:{cid}:wm")]
            + ([B("👀 보기", f"m:wm:{cid}"), B("🗑 삭제", f"m:wd:{cid}:m")] if media else []),
            [B("🔗 URL 버튼 수정" if btns else "🔗 URL 버튼 추가", f"m:in:{cid}:wb")]
            + ([B("👀 보기", f"m:wb:{cid}"), B("🗑 삭제", f"m:wd:{cid}:b")] if btns else []),
            [B(("✅" if s["greet_mention"] else "❌") + " 이름 태그", f"m:t:{cid}:greet_mention:{0 if s['greet_mention'] else 1}")],
            [B(("✅" if s["greet_reply_bot"] else "❌") + " 봇 글에 답장",
               f"m:t:{cid}:greet_reply_bot:{0 if s['greet_reply_bot'] else 1}")],
            [B("👀 전체 미리보기 (1:1로 받기)", f"m:wv:{cid}:all")],
            [B("⬅️ 뒤로", f"m:g:{cid}")]]
    return Screen("\n".join(lines), _kb(rows))


async def s_text(c: PanelCtx) -> Screen:
    tpl = (await c.svc.db.get_settings(c.cid))["greet_template"]
    if tpl:
        text = (f"📄 <b>인사말</b> ({len(tpl)}자)\n\n<blockquote>{esc(tpl)}</blockquote>\n\n"
                "<code>{names}</code> 자리에 새 멤버 이름(멘션)이 들어가요.")
    else:
        text = ("📄 <b>인사말</b>: 비어 있음\n\n지금은 AI가 방 말투에 맞춰 매번 새로 인사해요.\n"
                f"예: <i>{esc(FALLBACKS[0].replace('{names}', '홍길동'))}</i>\n\n"
                "직접 쓰면 항상 그 글로 인사해요.")
    rows = [[B("✏️ 수정" if tpl else "✏️ 쓰기", f"m:in:{c.cid}:wt")] + ([B("🗑 삭제", f"m:wd:{c.cid}:t")] if tpl else []),
            _back(c.cid)]
    return Screen(text, _kb(rows))


async def s_media(c: PanelCtx) -> Screen:
    media = media_of(await c.svc.db.get_settings(c.cid))
    if media:
        text = (f"🖼 <b>미디어</b>: {MEDIA_TYPES[media[0]]}\n\n"
                "인사말이 이 미디어의 설명으로 붙어서 나가요.\n(설명이 1024자를 넘으면 미디어와 글을 따로 보내요)")
        rows = [[B("📨 1:1로 받아보기", f"m:wv:{c.cid}:m")],
                [B("✏️ 바꾸기", f"m:in:{c.cid}:wm"), B("🗑 삭제", f"m:wd:{c.cid}:m")]]
    else:
        text = "🖼 <b>미디어</b>: 없음\n\n사진·영상·GIF 하나를 인사에 붙일 수 있어요."
        rows = [[B("➕ 추가", f"m:in:{c.cid}:wm")]]
    return Screen(text, _kb(rows + [_back(c.cid)]))


async def s_buttons(c: PanelCtx) -> Screen:
    s = await c.svc.db.get_settings(c.cid)
    btns = clean_buttons(s["greet_buttons"])
    if btns:
        lines = [f"🔗 <b>URL 버튼</b> ({len(btns)}/{MAX_BUTTONS})", ""]
        lines += [f"{i}. {esc(t)} → <code>{esc(u)}</code>" for i, (t, u) in enumerate(btns, 1)]
        lines.append("\n아래가 인사에 붙는 실제 모양이에요 (눌러서 주소 확인 가능).")
        rows = button_rows(s) + [[B("✏️ 수정", f"m:in:{c.cid}:wb"), B("🗑 삭제", f"m:wd:{c.cid}:b")]]
    else:
        lines = ["🔗 <b>URL 버튼</b>: 없음", "", f"인사 아래에 링크 버튼을 {MAX_BUTTONS}개까지 붙일 수 있어요."]
        rows = [[B("➕ 추가", f"m:in:{c.cid}:wb")]]
    return Screen("\n".join(lines), _kb(rows + [_back(c.cid)]))


# ── 미리보기 (1:1 로 새 메시지 + 🗑 닫기) ──────────────────
async def _admin_name(c: PanelCtx) -> str:
    row = await c.svc.db._one("SELECT first_name, last_name, username FROM users WHERE user_id=?", (c.uid,))
    return (user_name(row) if row else "") or "대표님"


async def r_view(c: PanelCtx) -> Screen:
    what = c.arg(0)
    if what not in ("m", "all"):
        return Screen(None)
    s = await c.svc.db.get_settings(c.cid)
    media = media_of(s)
    if what == "m" and not media:
        return Screen(None, toast="붙어 있는 미디어가 없어요.", alert=True)
    ids: list[int] = []  # 보낸 뒤 채운다 → 닫기 버튼이 이 메시지들을 지움
    close = [[B("🗑 닫기", f"m:k:{menu.token(c.svc, c.uid, c.cid, 'w_close', ids, CLOSE_TTL)}")]]
    if what == "m":
        kind, file_id = media
        send = {"photo": c.bot.send_photo, "video": c.bot.send_video, "animation": c.bot.send_animation}[kind]
        sent = [await send(c.uid, file_id, reply_markup=InlineKeyboardMarkup(close))]
        toast = "1:1 로 미디어를 보냈어요 👇"
    else:
        ai = not s["greet_template"]
        tpl = FALLBACKS[0] if ai else s["greet_template"]
        tag = bool(s["greet_mention"])
        name = await _admin_name(c)
        sent = await send_greeting(c.bot, c.uid, s, fill(with_names(tpl, tag), mention(c.uid, name) if tag else esc(name)),
                                   close)
        toast = "1:1 로 미리보기를 보냈어요 👇" + (" (AI 인사는 매번 달라서 예시 문구로 보여드려요)" if ai else "")
    ids.extend(m.message_id for m in sent)
    return Screen(None, toast=toast)


async def t_close(c: PanelCtx, ids) -> Screen:
    for mid in ids if isinstance(ids, list) else []:
        try:
            await c.bot.delete_message(c.uid, mid)
        except TelegramError:
            pass  # 이미 지웠거나 48시간 지남
    return Screen(None, toast="닫았어요.")


# ── 삭제 (확인 → 1회용 토큰) ──────────────────────────────
async def r_ask_delete(c: PanelCtx) -> Screen:
    part = c.arg(0)
    if part not in PARTS:
        return Screen(None)
    if not _has(await c.svc.db.get_settings(c.cid), part):
        screen = await s_editor(c)
        screen.toast = "이미 비어 있어요."
        return screen
    note = {"t": "\n(비우면 AI가 매번 새로 인사해요)", "m": "", "b": ""}[part]
    tok = menu.token(c.svc, c.uid, c.cid, "w_del", part)
    return Screen(f"{josa(PARTS[part])} 삭제할까요?{note}",
                  _kb([[B("🗑 삭제", f"m:k:{tok}"), B("취소", f"m:w:{c.cid}")]]))


async def t_delete(c: PanelCtx, part) -> Screen:
    if part == "t":
        await _save(c, "greet_template=", greet_template="")
    elif part == "m":
        await _save(c, "greet_media=", greet_media_type="", greet_media_id="")
    elif part == "b":
        await _save(c, "greet_buttons=", greet_buttons=[])
    screen = await s_editor(c)
    screen.toast = "삭제했어요."
    return screen


# ── 글자·미디어 입력 ──────────────────────────────────────
async def in_text(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    tpl = (msg.text or "").strip()
    if not tpl:
        return False, "인사말은 글자로 보내주세요."
    if len(tpl) > MAX_TEMPLATE:
        return False, f"인사말은 {MAX_TEMPLATE}자까지예요. (보낸 글: {len(tpl)}자)"
    await _save(c, f"greet_template={tpl[:80]}", greet_template=tpl)
    extra = "" if "{names}" in tpl else "\n(<code>{names}</code> 가 없어서 이름은 맨 앞에 붙여요)"
    return True, "✅ 인사말을 저장했어요." + extra


async def in_media(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    # GIF 는 document 도 같이 오니까 animation 을 먼저 본다
    if msg.animation:
        kind, file_id = "animation", msg.animation.file_id
    elif msg.photo:
        kind, file_id = "photo", msg.photo[-1].file_id  # 가장 큰 크기
    elif msg.video:
        kind, file_id = "video", msg.video.file_id
    else:
        return False, "사진·영상·GIF 중 하나를 보내주세요. (파일·스티커는 안 돼요)"
    await _save(c, f"greet_media={kind}", greet_media_type=kind, greet_media_id=file_id)
    note = "\n(같이 보낸 설명은 쓰지 않아요. 인사말은 📄 인사말에서 바꿔요)" if msg.caption else ""
    return True, f"✅ {MEDIA_OBJ[kind]} 인사에 붙였어요." + note


async def in_buttons(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    btns, err = parse_buttons(msg.text or "")
    if err:
        return False, "❌ " + err
    await _save(c, f"greet_buttons={len(btns)}개 " + ", ".join(u for _, u in btns), greet_buttons=btns)
    return True, f"✅ URL 버튼 {len(btns)}개를 저장했어요."


# ── 등록 ──────────────────────────────────────────────────
menu.register_hub(HubItem(21, "w", "✏️ 인사 편집기"))
menu.register_screen("w", s_editor)
menu.register_screen("wt", s_text)
menu.register_screen("wm", s_media)
menu.register_screen("wb", s_buttons)
menu.register_route("wd", Route(r_ask_delete))
menu.register_route("wv", Route(r_view))
menu.register_token_action("w_del", t_delete, fresh=True)
menu.register_token_action("w_close", t_close, need=menu.PUBLIC)  # 자기 1:1 의 미리보기만 지움 (토큰이 사용자에 묶임)

menu.register_input("wt", (
    "📄 새 <b>인사말</b>을 보내주세요.\n"
    "<code>{names}</code> 자리에 새 멤버 이름(멘션)이 들어가요. 없으면 맨 앞에 붙여요.\n"
    "예: <code>{names} 대표님, 환영합니다! 공지 꼭 읽어주세요 🙌</code>\n"
    f"(최대 {MAX_TEMPLATE}자)"), "w", in_text, s_editor)
menu.register_input("wm", (
    "🖼 인사에 붙일 <b>사진·영상·GIF</b> 하나를 보내주세요.\n"
    "인사말은 그 미디어의 설명으로 붙어요."), "w", in_media, s_editor, media=True)
menu.register_input("wb", (
    f"🔗 <b>URL 버튼</b>을 한 줄에 하나씩 보내주세요. (최대 {MAX_BUTTONS}개, 기존 버튼은 보낸 것으로 전부 바뀌어요)\n"
    "<code>공지 채널 - https://t.me/sodam_notice</code>\n"
    "<code>홈페이지 - https://example.com</code>\n"
    "주소는 <code>https://</code> 또는 <code>tg://</code> 만 돼요. 버튼 글자는 30자까지."), "w", in_buttons, s_editor)
