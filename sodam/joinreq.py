"""🚪 가입 신청 1:1 확인 (join_verify, 기본 꺼짐).

방이 '가입 신청 승인' 방식이면(초대링크 승인제·공개방 가입 신청), 신청이 오면:
- 봇 계정 → 거절 · 자유 멤버(free.py) → 바로 승인 · 스팸 명단(CAS·lols)·공동 차단 명단(각 설정이 켜져 있을 때) → 거절
- 나머지는 신청자에게 1:1 로 확인 문제. 방식(join_verify_kind):
  · puzzle = 그림 버튼 6개 중 하나(captcha.puzzle)
  · number = 🤖 로봇 인증 — 비뚤게 그린 숫자 그림 + 숫자 버튼 판. 버튼으로 누르거나 1:1 채팅에 숫자를 보내도 됨
    (2026-10-16 뉴월드 '입요 넣기 전에 로봇이 아닙니다 인증'. 버튼 판이 있어서 1:1 을 /start 안 한 사람도 됨)
  맞히면 승인, 3번 틀리거나 시간이 지나면 거절. 숫자는 틀릴 때마다 새 숫자.
텔레그램은 신청 뒤 5분 동안 봇이 신청자에게 1:1 을 보낼 수 있게 해준다 (ChatJoinRequest.user_chat_id).
1:1 을 못 보내면 그대로 두어 관리자가 직접 승인한다. 통과한 사람은 입장 뒤 방 캡차·대량 입장 내보내기를 건너뛴다(passed).
봇에게 '사용자 초대(can_invite_users)' 권한이 있어야 신청을 받고 처리할 수 있다.
"""
from __future__ import annotations

import io
import logging
import random
import secrets
import time
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.error import TelegramError

from . import cas, fedban, free, hooks
from .captcha import MAX_ATTEMPTS, puzzle
from .db import register_columns, register_schema
from .settings import register_setting
from .util import esc

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)

register_setting("join_verify", False, "가입 신청 1:1 확인")
register_setting("join_verify_kind", "puzzle", "가입 확인 방식",
                 choices={"number": "🔢 숫자 입력 (로봇 인증)", "puzzle": "🖼 그림 버튼"})
register_setting("join_verify_digits", 4, "로봇 인증 숫자 자리 수", range_=(4, 6))
register_schema("""
CREATE TABLE IF NOT EXISTS join_requests (
    chat_id  INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    answer   INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    passed   INTEGER NOT NULL DEFAULT 0,
    expires  INTEGER NOT NULL,
    kind     TEXT NOT NULL DEFAULT 'puzzle',
    typed    TEXT NOT NULL DEFAULT '',
    dm_chat  INTEGER,
    dm_msg   INTEGER,
    test     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, user_id)
);
""", migrate={"join_requests": "composite"})
# 숫자 인증 (2026-10-16): 방식·버튼으로 누른 숫자·1:1 사진 위치(고치기용)·관리자 시험(승인 안 함)
register_columns("join_requests", {"kind": "TEXT NOT NULL DEFAULT 'puzzle'", "typed": "TEXT NOT NULL DEFAULT ''",
                                   "dm_chat": "INTEGER", "dm_msg": "INTEGER", "test": "INTEGER NOT NULL DEFAULT 0"})

PASS_KEEP = 3600   # 통과 후 이 시간 안에 들어오면 방 캡차 생략
_FONTS = ("BlackHanSans.ttf", "DoHyeon-Regular.ttf", "Jua-Regular.ttf", "NanumGothic-ExtraBold.ttf")
_FONT_DIR = Path(__file__).parent / "data_files" / "fonts"


def new_code(digits: int) -> str:
    """첫 자리는 0 이 아님 (answer 를 정수로 저장)."""
    digits = max(4, min(6, int(digits or 4)))
    return str(secrets.randbelow(9) + 1) + "".join(str(secrets.randbelow(10)) for _ in range(digits - 1))


def code_image(code: str) -> bytes:
    """비뚤게 그린 숫자 그림 (PNG). 글자마다 글꼴·기울기·색·높이가 다르고 선·점 잡음을 얹는다 —
    사람 눈엔 쉽고, 글자 그대로 복사해 가는 자동 프로그램은 못 함."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont   # 늦게 (PIL 은 무거움)
    rnd = random.Random(secrets.randbits(64))
    w, h = 120 + 70 * len(code), 170
    img = Image.new("RGB", (w, h), tuple(rnd.randint(225, 250) for _ in range(3)))
    d = ImageDraw.Draw(img)
    for _ in range(260):   # 바탕 점
        x, y = rnd.randrange(w), rnd.randrange(h)
        d.point((x, y), fill=tuple(rnd.randint(120, 210) for _ in range(3)))
    x = 60
    for ch in code:
        font = ImageFont.truetype(str(_FONT_DIR / rnd.choice(_FONTS)), rnd.randint(88, 108))
        tile = Image.new("RGBA", (120, 140), (0, 0, 0, 0))
        ImageDraw.Draw(tile).text((60, 70), ch, font=font, anchor="mm",
                                  fill=tuple(rnd.randint(10, 110) for _ in range(3)) + (255,))
        tile = tile.rotate(rnd.uniform(-28, 28), resample=Image.BICUBIC, expand=False)
        img.paste(tile, (x - 30 + rnd.randint(-6, 6), 15 + rnd.randint(-12, 12)), tile)
        x += 70
    for _ in range(4):     # 숫자를 가로지르는 선
        pts = [(rnd.randint(0, w // 4), rnd.randint(20, h - 20)), (rnd.randint(w // 3, 2 * w // 3), rnd.randint(10, h - 10)),
               (rnd.randint(3 * w // 4, w), rnd.randint(20, h - 20))]
        d.line(pts, fill=tuple(rnd.randint(60, 160) for _ in range(3)), width=rnd.randint(2, 4))
    img = img.filter(ImageFilter.SMOOTH)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _keypad(chat_id: int) -> InlineKeyboardMarkup:
    b = lambda label, v: InlineKeyboardButton(label, callback_data=f"jr:{chat_id}:k:{v}")   # noqa: E731
    return InlineKeyboardMarkup([[b("1", 1), b("2", 2), b("3", 3)], [b("4", 4), b("5", 5), b("6", 6)],
                                 [b("7", 7), b("8", 8), b("9", 9)], [b("⌫ 지우기", "b"), b("0", 0), b("🔄 새 숫자", "n")]])


def _caption(title: str, digits: int, typed: str, minutes: int | None = None, note: str = "") -> str:
    slots = " ".join(typed[i] if i < len(typed) else "＿" for i in range(digits))
    head = f"🤖 <b>{esc(title)}</b> 로봇 인증이에요.\n" if title else "🤖 로봇 인증이에요.\n"
    when = f"<b>{minutes}분 안에</b> " if minutes else ""
    return (head + f"{when}그림 속 숫자 <b>{digits}자리</b>를 아래 버튼으로 누르거나, 여기 채팅으로 보내 주세요.\n"
            f"입력: <code>{slots}</code>" + (f"\n{note}" if note else ""))


def _photo(data: bytes):
    from .casino.basic import photo_file   # attach=True (editMessageMedia 용)
    return photo_file(data)


async def _row(svc: Services, chat_id: int, user_id: int):
    return await svc.db._one("SELECT * FROM join_requests WHERE chat_id=? AND user_id=?", (chat_id, user_id))


async def passed(svc: Services, chat_id: int, user_id: int) -> bool:
    row = await _row(svc, chat_id, user_id)
    return bool(row and row["passed"] and row["expires"] > time.time())


async def _decline(svc: Services, bot, chat_id: int, user_id: int, reason: str) -> None:
    await svc.db._write("DELETE FROM join_requests WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    try:
        await bot.decline_chat_join_request(chat_id, user_id)
    except TelegramError as e:   # 이미 처리됨(관리자가 직접)·신청 취소
        log.info("decline join request failed %s/%s: %s", chat_id, user_id, e)
    await svc.db.log_mod(chat_id, None, user_id, "join_decline", reason)


async def on_request(svc: Services, bot, req) -> None:
    if (await svc.db.get_settings(req.chat.id))["join_verify"]:
        from .subscription import chat_title  # 늦게 import (순환 방지)
        await verify(svc, bot, req, await chat_title(svc, req.chat.id))


async def verify(svc: Services, bot, req, title: str) -> bool:
    """신청 1건 확인 (방·채널 공용 — 채널은 channel.py 가 부름). 처리했으면 True, 1:1 을 못 보내 관리자 몫으로 남기면 False."""
    chat_id, user = req.chat.id, req.from_user
    s = await svc.db.get_settings(chat_id)
    if user.is_bot:
        await _decline(svc, bot, chat_id, user.id, "봇 계정")
        return True
    if await free.is_free(svc.db, chat_id, user.id):
        try:
            await bot.approve_chat_join_request(chat_id, user.id)
        except TelegramError as e:
            log.info("approve free member failed: %s", e)
        return True
    if await cas.blocks(svc, chat_id, user.id, s) or \
            (s["fedban_mode"] != "off" and await fedban.lookup(svc.db, user.id)):
        await _decline(svc, bot, chat_id, user.id, "스팸·공동 차단 명단")
        return True
    minutes = s["captcha_minutes"]
    if s.get("join_verify_kind") == "number":
        return await send_number(svc, bot, chat_id, user.id, req.user_chat_id, title, s, minutes)
    label, answer, rows = puzzle(f"jr:{chat_id}")
    text = (f"🚪 <b>{esc(title)}</b> 입장 신청 확인이에요.\n"
            f"<b>{minutes}분 안에</b> 아래에서 <b>{label}</b> 버튼을 눌러주세요. 맞히면 바로 들어가요.")
    try:
        await bot.send_message(req.user_chat_id, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    except TelegramError as e:   # 1:1 을 못 보냄 → 관리자가 직접 승인하도록 그대로 둠
        log.info("join verify dm failed %s/%s: %s", chat_id, user.id, e)
        return False
    await svc.db._write("INSERT OR REPLACE INTO join_requests(chat_id, user_id, answer, expires) VALUES(?, ?, ?, ?)",
                        (chat_id, user.id, answer, int(time.time()) + minutes * 60))
    return True


async def send_number(svc: Services, bot, chat_id: int, user_id: int, dm_chat: int, title: str, s: dict,
                      minutes: int, test: bool = False) -> bool:
    """숫자 그림 + 버튼 판을 1:1 로. 못 보내면 False (관리자 몫). test = 관리자 시험 (맞혀도 승인 안 함)."""
    digits = int(s.get("join_verify_digits") or 4)
    code = new_code(digits)
    try:
        sent = await bot.send_photo(dm_chat, _photo(code_image(code)), parse_mode="HTML", reply_markup=_keypad(chat_id),
                                    caption=_caption(title, digits, "", minutes, "🧪 관리자 시험이에요 (승인은 안 해요)" if test else ""))
    except TelegramError as e:
        log.info("join verify number dm failed %s/%s: %s", chat_id, user_id, e)
        return False
    await svc.db._write(
        "INSERT OR REPLACE INTO join_requests(chat_id, user_id, answer, expires, kind, typed, dm_chat, dm_msg, test) "
        "VALUES(?, ?, ?, ?, 'number', '', ?, ?, ?)",
        (chat_id, user_id, int(code), int(time.time()) + minutes * 60, dm_chat, getattr(sent, "message_id", None), int(test)))
    return True


async def _show(bot, row, caption: str, *, keypad: bool, image: bytes | None = None) -> None:
    """숫자 인증 1:1 사진 고치기 (버튼으로 눌렀든 채팅으로 보냈든 같은 메시지)."""
    if not row["dm_msg"]:
        return
    markup = _keypad(row["chat_id"]) if keypad else None
    try:
        if image is not None:
            await bot.edit_message_media(chat_id=row["dm_chat"], message_id=row["dm_msg"], reply_markup=markup,
                                         media=InputMediaPhoto(_photo(image), caption=caption, parse_mode="HTML"))
        else:
            await bot.edit_message_caption(chat_id=row["dm_chat"], message_id=row["dm_msg"], caption=caption,
                                           parse_mode="HTML", reply_markup=markup)
    except TelegramError as e:   # 같은 글(안 바뀜)·지운 메시지
        log.debug("join verify edit failed: %s", e)


async def _check(svc: Services, bot, row, code: str) -> tuple[bool, str]:
    """숫자 확인. (끝났나, 사람에게 보일 한 줄). 맞으면 승인, 틀리면 새 숫자, 3번 틀리면 거절."""
    chat_id, uid = row["chat_id"], row["user_id"]
    title = await _title(svc, chat_id)
    digits = len(str(row["answer"]))
    if code == str(row["answer"]):
        if row["test"]:
            await svc.db._write("DELETE FROM join_requests WHERE chat_id=? AND user_id=?", (chat_id, uid))
            msg = "✅ 시험 통과! 실제 신청이면 여기서 바로 승인돼요."
            await _show(bot, row, f"🤖 로봇 인증 — {msg}", keypad=False)
            return True, msg
        msg = await _approve(svc, bot, chat_id, uid)
        await _show(bot, row, f"🤖 <b>{esc(title)}</b> 로봇 인증\n{msg}", keypad=False)
        return True, msg
    attempts = row["attempts"] + 1
    if attempts >= MAX_ATTEMPTS:
        if row["test"]:
            await svc.db._write("DELETE FROM join_requests WHERE chat_id=? AND user_id=?", (chat_id, uid))
        else:
            await _decline(svc, bot, chat_id, uid, f"로봇 인증 {MAX_ATTEMPTS}회 실패")
        msg = "❌ 3번 틀려서 신청이 거절됐어요. 잠시 뒤 다시 신청해 주세요."
        await _show(bot, row, f"🤖 <b>{esc(title)}</b> 로봇 인증\n{msg}", keypad=False)
        return True, msg
    fresh = new_code(digits)
    await svc.db._write("UPDATE join_requests SET attempts=?, answer=?, typed='' WHERE chat_id=? AND user_id=?",
                        (attempts, int(fresh), chat_id, uid))
    msg = f"틀렸어요! 새 숫자로 바꿨어요. 남은 기회 {MAX_ATTEMPTS - attempts}번"
    await _show(bot, row, _caption(title, digits, "", None, "❗ " + msg), keypad=True, image=code_image(fresh))
    return False, msg


async def _title(svc: Services, chat_id: int) -> str:
    from .subscription import chat_title   # 늦게 import (순환 방지)
    try:
        return await chat_title(svc, chat_id)
    except Exception:   # 채널 등 이름을 못 찾아도 인증은 계속
        return ""


async def _approve(svc: Services, bot, chat_id: int, uid: int) -> str:
    """통과로 먼저 차지 → 승인. 사람에게 보일 한 줄을 돌려줌 (두 번 와도 승인·기록은 한 번)."""
    claimed = await svc.db.atomic(lambda c: c.execute(
        "UPDATE join_requests SET passed=1, expires=? WHERE chat_id=? AND user_id=? AND passed=0",
        (int(time.time()) + PASS_KEEP, chat_id, uid)).rowcount)
    if not claimed:
        return "이미 끝난 확인이에요."
    try:
        await bot.approve_chat_join_request(chat_id, uid)
    except TelegramError as e:
        if "right" in e.message.lower() or "admin" in e.message.lower():   # 봇에 '사용자 초대' 권한이 없어짐
            await svc.db.log_mod(chat_id, None, uid, "join_no_right", e.message[:120])
            return "✅ 확인됐어요! 다만 소담에게 승인 권한이 없어서, 관리자가 승인하면 들어가요."
        await svc.db._write("DELETE FROM join_requests WHERE chat_id=? AND user_id=?", (chat_id, uid))
        return f"이 신청은 이미 처리됐어요. ({esc(e.message[:80])})"
    await svc.db.log_mod(chat_id, None, uid, "join_pass", "가입 신청 1:1 확인")
    return "✅ 확인됐어요! 방에 들어갔어요. 환영합니다 🙌"


async def _keypress(svc: Services, bot, q, chat_id: int, key: str) -> None:
    uid = q.from_user.id
    row = await _row(svc, chat_id, uid)
    if not row or row["passed"] or row["kind"] != "number":
        return await q.answer("이미 끝난 확인이에요.")
    title = await _title(svc, chat_id)
    digits = len(str(row["answer"]))
    if key == "n":   # 새 숫자 (기회는 안 줄어듦, 잘 안 보일 때)
        fresh = new_code(digits)
        await svc.db._write("UPDATE join_requests SET answer=?, typed='' WHERE chat_id=? AND user_id=?", (int(fresh), chat_id, uid))
        await q.answer("새 숫자예요")
        return await _show(bot, row, _caption(title, digits, ""), keypad=True, image=code_image(fresh))
    typed = row["typed"][:-1] if key == "b" else (row["typed"] + key if key.isdecimal() and len(key) == 1 else None)
    if typed is None:
        return await q.answer()
    typed = typed[:digits]
    # 연타가 겹쳐도 한 번에 한 글자씩만 (읽은 값 그대로일 때만 바꿈)
    ok = await svc.db.atomic(lambda c: c.execute(
        "UPDATE join_requests SET typed=? WHERE chat_id=? AND user_id=? AND passed=0 AND typed=?",
        (typed, chat_id, uid, row["typed"])).rowcount)
    if not ok:
        return await q.answer("천천히 눌러 주세요")
    if len(typed) < digits:
        await q.answer()
        return await _show(bot, row, _caption(title, digits, typed), keypad=True)
    done, msg = await _check(svc, bot, row, typed)
    await q.answer(msg[:180], show_alert=not done)


def _digits(text: str) -> str | None:
    """1:1 로 보낸 글이 숫자뿐이면 그 숫자 (전각·띄어쓰기·하이픈 허용)."""
    t = unicodedata.normalize("NFKC", text or "").replace(" ", "").replace("-", "")
    return t if 3 <= len(t) <= 8 and t.isascii() and t.isdecimal() else None


async def on_dm_text(svc: Services, bot, msg) -> bool:
    """1:1 에 숫자만 보냄 + 진행 중인 숫자 인증 → 확인 (hooks.DM_TEXT_HOOKS)."""
    code = _digits(getattr(msg, "text", None) or "")
    if code is None or not msg.from_user:
        return False
    row = await svc.db._one("SELECT * FROM join_requests WHERE user_id=? AND passed=0 AND kind='number' AND expires>? "
                            "ORDER BY expires DESC LIMIT 1", (msg.from_user.id, int(time.time())))
    if not row:
        return False
    done, text = await _check(svc, bot, row, code)
    try:
        await msg.reply_text(text)
    except TelegramError:
        pass
    return True


hooks.add_dm_text_hook(on_dm_text)


async def on_callback(svc: Services, bot, q, parts: list[str]) -> None:
    """jr:<방ID>:<번호> — 신청자 1:1 의 그림 버튼 · jr:<방ID>:k:<0-9|b|n> — 숫자 인증 버튼 판."""
    if len(parts) >= 3 and parts[1] == "k":
        try:
            return await _keypress(svc, bot, q, int(parts[0]), parts[2])
        except ValueError:
            return await q.answer()
    try:
        chat_id, choice = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return await q.answer()
    uid = q.from_user.id
    row = await _row(svc, chat_id, uid)
    if not row or row["passed"] or row["kind"] != "puzzle":
        await q.answer("이미 끝난 확인이에요.")
        return
    if choice != row["answer"]:
        attempts = row["attempts"] + 1
        if attempts < MAX_ATTEMPTS:
            await svc.db._write("UPDATE join_requests SET attempts=? WHERE chat_id=? AND user_id=?",
                                (attempts, chat_id, uid))
            return await q.answer(f"틀렸어요! 남은 기회 {MAX_ATTEMPTS - attempts}번", show_alert=True)
        await _decline(svc, bot, chat_id, uid, f"가입 확인 {MAX_ATTEMPTS}회 실패")
        await q.answer()
        return await _edit(q, "❌ 확인에 실패해서 신청이 거절됐어요. 잠시 뒤 다시 신청해 주세요.")
    # 먼저 '통과'로 차지한다 (정답을 두 번 눌러도 승인·기록은 한 번, 두 번째는 '이미 끝난 확인')
    text = await _approve(svc, bot, chat_id, uid)
    if text == "이미 끝난 확인이에요.":
        return await q.answer(text)
    await q.answer("확인됐어요!" if text.startswith("✅") else None)
    await _edit(q, text)


async def _edit(q, text: str) -> None:
    try:
        await q.edit_message_text(text)
    except TelegramError:
        pass


async def expire(svc: Services, bot) -> None:
    """30초마다: 시간이 지난 미확인 신청은 거절, 통과 기록은 정리."""
    now = int(time.time())
    for row in await svc.db._all("SELECT * FROM join_requests WHERE passed=0 AND expires<=?", (now,)):
        if row["test"]:
            await svc.db._write("DELETE FROM join_requests WHERE chat_id=? AND user_id=?", (row["chat_id"], row["user_id"]))
        else:
            await _decline(svc, bot, row["chat_id"], row["user_id"], "가입 확인 시간 초과")
        if row["kind"] == "number":
            await _show(bot, row, "🤖 로봇 인증 — ⏰ 시간이 지나 신청이 거절됐어요. 다시 신청해 주세요.", keypad=False)
    await svc.db._write("DELETE FROM join_requests WHERE passed=1 AND expires<=?", (now,))
