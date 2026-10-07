"""🎵 소담 뮤직봇 — 봇 쪽 (노래 찾기·받기·틀기는 음성 담당 프로세스 sodam/voice/music.py).

명령 (멜론봇과 같은 이름 + 한국어):
  .노래 / /play <제목·유튜브 링크>  (음악 파일에 답장해도 됨)   .스킵 /skip   .일시정지 /pause   .다시재생 /resume
  .대기열 /queue   .빼기 /remove <번호>   .음소거 / .음소거해제 (노래만 — /mute 는 멤버 뮤트)   .이동 /seek <초|+초|-초>
  .노래끝 /end (음성채팅 나가기)   .볼륨 /volume <0~200>   .반복 /loop <0~10>   .지금곡 /np   .도우미부르기 /userbotjoin
  영어 이름을 '@봇' 없이 쓰는 건(`/play`) 방 설정 music_bare 를 켰을 때만 — 다른 음악봇(멜론봇 등)이 있는 방에서
  둘 다 틀지 않게. 명령 메뉴에서 고르면 텔레그램이 '/play@sodam_ai_bot' 로 붙여 줌.
권한: 신청 = music_who(누구나/관리자) · 넘기기·일시정지·되감기·음량 = 관리자 또는 지금 곡 신청자(music_ctrl=all 이면 누구나) ·
  끝내기 = 관리자(또는 남은 곡이 전부 내 곡) · 빼기 = 관리자는 아무 곡, 아니면 내 곡.
이용 기간 중인 방만. AI 비용 없음 (유튜브 → 음성채팅). 오너 메인 🎵 — 유튜브 상태·쿠키(서버 IP 가 막힐 때).
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

from telegram import Message
from telegram.error import TelegramError

from .. import hooks, menu, persist, settings, tools
from ..menu import ADMIN, OWNER, B, HubItem, PanelCtx, Route, Screen
from ..permissions import Role
from ..services import PendingInput
from ..util import esc, user_name
from ..voice import musicq, store
from . import voice

log = logging.getLogger(__name__)

settings.register_setting("music_enabled", True, "뮤직봇")
settings.register_setting("music_who", "all", "노래 신청",
                          choices={"all": "all", "누구나": "all", "전체": "all", "admin": "admin", "관리자": "admin",
                                   "관리자만": "admin"},
                          choice_labels={"all": "누구나", "admin": "관리자만"})
settings.register_setting("music_ctrl", "requester", "노래 넘기기·멈춤",
                          choices={"requester": "requester", "신청자": "requester", "all": "all", "누구나": "all"},
                          choice_labels={"requester": "관리자·그 곡 신청자", "all": "누구나"})
settings.register_setting("music_bare", False, "@ 없는 /play 받기")

WAIT_CTL = 8.0                  # 넘기기·일시정지 결과 기다림
COOLDOWN = 4.0                  # 한 사람 노래 신청 간격 (초)
TG_MAX = 20 * 1024 * 1024       # 봇이 받을 수 있는 파일 (Bot API getFile)
AUDIO_EXT = {".mp3", ".m4a", ".ogg", ".oga", ".opus", ".wav", ".flac", ".aac", ".webm"}
ENGLISH = {"play", "skip", "next", "pause", "resume", "queue", "remove", "seek", "end", "volume", "vol",
           "loop", "np", "nowplaying", "userbotjoin", "mmute", "munmute"}
RESULT = {"skipped": "⏭ 다음 곡으로 넘겼어요.", "paused": "⏸ 일시정지했어요. <code>.다시재생</code> 으로 이어서.",
          "resumed": "▶️ 다시 틀어요.", "muted": "🔇 노래 소리를 껐어요 (계속 흐름).", "unmuted": "🔊 노래 소리를 켰어요.",
          "ended": "⏹ 노래를 끝내고 음성채팅에서 나왔어요.", "no_music": "지금 틀고 있는 노래가 없어요."}
FAILED_CTL = {"skip": "넘길 곡이 없어요.", "pause": "이미 멈춰 있거나 틀고 있는 곡이 없어요.",
              "resume": "멈춘 곡이 없어요.", "seek": "그 위치로 못 가요 (곡 길이를 넘었어요)."}
END_REASON = {"idle": "틀 노래가 없어서", "chat_closed": "음성채팅이 닫혀서", "kicked": "음성채팅에서 내보내져서",
              "error:play": "소리 보내기 오류로"}


def data_dir(svc) -> Path:
    return Path(svc.cfg.db_path).parent


# ── 확인 ──────────────────────────────────────────────
async def precheck(svc, chat_id: int, role: Role) -> str | None:
    db = svc.db
    s = await db.get_settings(chat_id)
    if not s.get("music_enabled", True):
        return "🎵 이 방은 뮤직봇이 꺼져 있어요 (관리자: 메뉴 → 🎵 뮤직봇)."
    if s.get("music_who", "all") == "admin" and role < Role.ADMIN:
        return "🎵 이 방은 관리자만 노래를 신청할 수 있어요."
    if not await svc.paid_features(chat_id):
        return "🎵 이용 기간이 끝난 방이라 뮤직봇은 쉬어요."
    if not await store.assistant(db):
        return voice.RESULT_TEXT["no_assistant"]
    if not await store.worker_alive(db):
        return voice.RESULT_TEXT["no_worker"]
    return None


async def can_control(svc, chat_id: int, uid: int, role: Role) -> bool:
    if role >= Role.ADMIN:
        return True
    if (await svc.db.get_settings(chat_id)).get("music_ctrl", "requester") == "all":
        return True
    cur = await musicq.current(svc.db, chat_id)
    return bool(cur and cur["by_id"] == uid)


async def can_end(svc, chat_id: int, uid: int, role: Role) -> bool:
    if role >= Role.ADMIN or (await svc.db.get_settings(chat_id)).get("music_ctrl") == "all":
        return True
    rows = await svc.db._all("SELECT by_id FROM music_queue WHERE chat_id=? AND state IN ('queued','playing')", (chat_id,))
    return bool(rows) and all(r["by_id"] == uid for r in rows)


# ── 신청 ──────────────────────────────────────────────
async def _helper_ready(svc, bot, chat_id: int, uid: int) -> str | None:
    """도우미 계정이 방에 없으면 넣고 '음성채팅 관리' 권한까지 (AI 음성채팅과 같은 길). 실패면 방에 보일 이유."""
    if await musicq.active_session(svc.db, chat_id) or await store.active_call(svc.db, chat_id):
        return None                                     # 이미 음성채팅에 들어가 있음
    a = await store.assistant(svc.db)
    if not a:
        return voice.RESULT_TEXT["no_assistant"]
    prep = await voice._prepare_member(bot, chat_id, a["id"])
    if prep in ("banned", "no_invite_right"):
        return voice.RESULT_TEXT["banned"] if prep == "banned" else "🎵 봇에 '사용자 초대' 권한이 없어서 노래 도우미를 방에 못 넣어요."
    if prep:
        jid = await store.add_job(svc.db, chat_id, "join", prep, uid)
        if jid:
            st, res = await voice._wait(svc.db, jid)
            if st != "done":
                return voice._result_text(res)
    await voice._promote(bot, chat_id, a["id"])
    return None


async def request(svc, bot, chat_id: int, user, *, query: str = "", path: str | None = None, title: str = "",
                  duration: int = 0, status_msg: int | None = None) -> None:
    """뒤에서: 도우미 준비 → 음성 담당에 신청 → 결과(대기열 추가·재생 시작)는 음성 담당이 '찾는 중' 글을 고쳐서."""
    db = svc.db

    async def fail(text: str) -> None:
        try:
            if status_msg:
                await bot.edit_message_text(text, chat_id=chat_id, message_id=status_msg, parse_mode="HTML")
            else:
                await bot.send_message(chat_id, text, parse_mode="HTML")
        except TelegramError:
            pass

    why = await _helper_ready(svc, bot, chat_id, user.id)
    if why:
        return await fail(why)
    payload = {"query": query, "path": path, "title": title, "duration": duration, "status_msg": status_msg,
               "by_name": user_name(user)}
    jid = await store.add_job(db, chat_id, "music_play", payload, user.id, dedupe=False)
    st, res = await voice._wait(db, jid, timeout=voice.WAIT_JOB)
    await store.mark_notified(db, "voice_jobs", jid)
    if res == "slow":                                       # 아직 처리 중 (앞 신청이 밀림) → 결과는 음성 담당이 그 글을 고침
        return
    if st != "done" and not res.startswith("music:"):      # music:* 는 음성 담당이 이미 글을 고쳤음
        if res == "no_voice_right" and await voice._is_basic_group(bot, chat_id):
            res = "basic_group"
        await fail(voice._result_text(res).replace("📞", "🎵"))


async def _tg_audio(svc, bot, msg) -> tuple[str, str, int] | str | None:
    """답장한 음악 파일(오디오·음성·음악 문서) → data/music/tg_*.ext 로 받아 (경로, 제목, 길이). 못 받으면 이유."""
    src = msg.reply_to_message if msg is not None else None
    if src is None:
        return None
    f = src.audio or src.voice or (src.document if src.document and (
        (src.document.mime_type or "").startswith("audio/") or Path(src.document.file_name or "").suffix.lower() in AUDIO_EXT) else None)
    if f is None:
        return None
    if (f.file_size or 0) > TG_MAX:
        return "🎵 20MB 넘는 파일은 봇이 못 받아요."
    ext = Path(getattr(f, "file_name", None) or "").suffix.lower() or (".ogg" if src.voice else ".mp3")
    if ext not in AUDIO_EXT:
        ext = ".mp3"
    out = data_dir(svc) / "music" / f"tg_{f.file_unique_id}{ext}"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        try:
            tf = await bot.get_file(f.file_id)
            await tf.download_to_drive(str(out))
        except TelegramError as e:
            return f"🎵 파일을 못 받았어요 ({esc(e.message)})."
    title = " - ".join(x for x in (getattr(f, "performer", None), getattr(f, "title", None)) if x) \
        or getattr(f, "file_name", None) or "음성 메시지"
    return str(out), title[:120], int(getattr(f, "duration", 0) or 0)


async def play(svc, bot, chat_id: int, user, role: Role, query: str, msg=None) -> str | None:
    """신청 접수. 방에 보일 거절 이유를 돌려주거나, 접수했으면 None ('찾는 중' 글은 여기서 올림)."""
    why = await precheck(svc, chat_id, role)
    if why:
        return why
    query = (query or "").strip()[:300]
    last = svc.__dict__.setdefault("_music_last", {})
    now = time.monotonic()
    if now - last.get((chat_id, user.id), -1e9) < COOLDOWN:      # 연타 = 유튜브 검색만 늘어남 (서버 IP 막힘 위험)
        return f"⏳ 노래 신청은 {COOLDOWN:g}초에 한 번씩 해 주세요."
    last[(chat_id, user.id)] = now
    if len(last) > 5000:
        last.clear()
    tg = await _tg_audio(svc, bot, msg)
    if isinstance(tg, str):
        return tg
    if not tg and not query:
        return "🎵 노래 제목이나 유튜브 링크를 같이 써 주세요. 예: <code>.노래 아이유 밤편지</code>"
    head = f"🔎 <b>{esc(tg[1] if tg else query)[:80]}</b> 찾는 중…"
    try:
        sent = await (msg.reply_text(head, parse_mode="HTML") if msg is not None
                      else bot.send_message(chat_id, head, parse_mode="HTML"))
        status = sent.message_id
    except TelegramError:
        status = None
    if tg:
        persist.spawn(request(svc, bot, chat_id, user, path=tg[0], title=tg[1], duration=tg[2], status_msg=status))
    else:
        persist.spawn(request(svc, bot, chat_id, user, query=query, status_msg=status))
    return None


async def control(svc, chat_id: int, user_id: int, role: Role, op: str, value=None, relative: bool = False) -> str:
    """넘기기·일시정지·… → 결과 글 (HTML)."""
    if op == "end":
        if not await can_end(svc, chat_id, user_id, role):
            return "⏹ 끝내기는 관리자만 (또는 남은 곡이 전부 내가 신청한 곡일 때) 할 수 있어요."
    elif not await can_control(svc, chat_id, user_id, role):
        return "🎵 관리자나 지금 곡을 신청한 분만 할 수 있어요."
    if not await musicq.active_session(svc.db, chat_id):
        return RESULT["no_music"]
    payload = {"value": value, "relative": relative}
    jid = await store.add_job(svc.db, chat_id, f"music_{op}", payload, user_id, dedupe=op not in ("seek", "volume", "loop"))
    if not jid:
        return "⏳ 처리하는 중이에요."
    st, res = await voice._wait(svc.db, jid, timeout=WAIT_CTL)
    await store.mark_notified(svc.db, "voice_jobs", jid)
    if st == "done":
        if res.startswith("volume:"):
            return f"🔊 음량 {res[7:]}%"
        if res.startswith("loop:"):
            return f"🔁 지금 곡 {res[5:]}번 더 반복" if res[5:] != "0" else "🔁 반복 끔"
        if res.startswith("seek:"):
            return f"⏩ {musicq.fmt_dur(int(res[5:])) if int(res[5:]) else '0:00'} 부터 틀어요."
        return RESULT.get(res, "✅")
    if res == "no_music":
        return RESULT["no_music"]
    return FAILED_CTL.get(op) or voice._result_text(res).replace("📞", "🎵")


async def queue_text(svc, chat_id: int) -> str:
    cur = await musicq.current(svc.db, chat_id)
    rows = await musicq.waiting(svc.db, chat_id)
    if not cur and not rows:
        return "📃 대기열이 비어 있어요. <code>.노래 제목</code> 으로 신청!"
    lines = []
    if cur:
        lines.append(f"🎶 지금: <b>{esc(cur['title'])[:80]}</b> ({musicq.fmt_dur(cur['duration'])}) · {esc(cur['by_name'] or '')}")
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {esc(r['title'])[:70]} ({musicq.fmt_dur(r['duration'])}) · {esc(r['by_name'] or '')}")
    if rows:
        lines.append(f"\n빼기: <code>.빼기 번호</code> · 총 {len(rows)}곡 대기")
    return "\n".join(lines)


# ── 명령 ──────────────────────────────────────────────
def _bare_english(ctx) -> bool:
    """'/play' 처럼 @봇 없이 영어 이름 → 다른 음악봇이 있는 방이면 둘 다 틀게 됨 → 설정을 켠 방만."""
    text = (ctx.msg.text or ctx.msg.caption or "").strip()
    if not text.startswith("/"):
        return False
    head = text[1:].split(" ", 1)[0]
    return "@" not in head and head.lower() in ENGLISH


async def _skip_bare(ctx) -> bool:
    return _bare_english(ctx) and not (await ctx.svc.db.get_settings(ctx.chat_id)).get("music_bare")


async def c_play(ctx) -> None:
    if ctx.chat_id > 0:
        await ctx.reply("🎵 노래는 그룹방 음성채팅에서 틀어요. 방에서 <code>.노래 제목</code>")
        return
    if await _skip_bare(ctx):
        return
    why = await play(ctx.svc, ctx.bot, ctx.chat_id, ctx.user, ctx.role, ctx.argstr, ctx.msg)
    if why:
        await ctx.reply(why)


def _ctl(op: str, *, arg: str = ""):
    async def fn(ctx) -> None:
        if ctx.chat_id > 0 or await _skip_bare(ctx):
            return
        value, relative = None, False
        if arg:
            raw = (ctx.args[0] if ctx.args else "").strip()
            m = re.fullmatch(r"([+-]?)(\d{1,5})(?::(\d{1,2}))?", raw)
            if not m:
                await ctx.reply({"seek": "⏩ 초로 써 주세요. 예: <code>.이동 90</code> · <code>.이동 +30</code> · <code>.이동 1:30</code>",
                                 "volume": "🔊 0~200 사이 숫자로. 예: <code>.볼륨 50</code>",
                                 "loop": "🔁 0~10 숫자로. 예: <code>.반복 2</code> (0 = 끔)"}[op])
                return
            value = int(m.group(2)) * 60 + int(m.group(3)) if m.group(3) else int(m.group(2))
            if m.group(1):
                relative = True
                value = value if m.group(1) == "+" else -value
        await ctx.reply(await control(ctx.svc, ctx.chat_id, ctx.user.id, ctx.role, op, value, relative))
    return fn


async def c_queue(ctx) -> None:
    if ctx.chat_id > 0 or await _skip_bare(ctx):
        return
    await ctx.reply(await queue_text(ctx.svc, ctx.chat_id))


async def c_now(ctx) -> None:
    if ctx.chat_id > 0 or await _skip_bare(ctx):
        return
    cur = await musicq.current(ctx.svc.db, ctx.chat_id)
    await ctx.reply(musicq.card_text("now", dict(cur)) if cur else RESULT["no_music"])


async def c_remove(ctx) -> None:
    if ctx.chat_id > 0 or await _skip_bare(ctx):
        return
    n = int(ctx.args[0]) if ctx.args and ctx.args[0].isdecimal() else 0
    if not n:
        await ctx.reply("빼려는 곡 번호를 써 주세요. 예: <code>.빼기 2</code> (번호는 <code>.대기열</code>)")
        return
    row, why = await musicq.remove_nth(ctx.svc.db, ctx.chat_id, n, ctx.user.id, ctx.role >= Role.ADMIN)
    await ctx.reply({"ok": f"🗑 {n}번 <b>{esc(row['title'] if row else '')[:80]}</b> 뺐어요.",
                     "none": f"대기열에 {n}번 곡이 없어요.",
                     "not_yours": "내가 신청한 곡만 뺄 수 있어요 (관리자는 아무 곡)."}[why])


async def c_userbotjoin(ctx) -> None:
    """노래 도우미 계정을 방에 넣기만 (멜론봇 /userbotjoin)."""
    if ctx.chat_id > 0 or await _skip_bare(ctx):
        return
    if ctx.role < Role.ADMIN:
        await ctx.reply("관리자만 쓸 수 있어요.")
        return
    a = await store.assistant(ctx.svc.db)
    if not a:
        await ctx.reply(voice.RESULT_TEXT["no_assistant"])
        return
    prep = await voice._prepare_member(ctx.bot, ctx.chat_id, a["id"])
    if prep in ("banned", "no_invite_right"):
        await ctx.reply(voice.RESULT_TEXT["banned"] if prep == "banned" else "봇에 '사용자 초대' 권한이 없어요.")
        return
    if prep:
        jid = await store.add_job(ctx.svc.db, ctx.chat_id, "join", prep, ctx.user.id)
        st, res = await voice._wait(ctx.svc.db, jid) if jid else ("done", "")
        if st != "done":
            await ctx.reply(voice._result_text(res))
            return
    await voice._promote(ctx.bot, ctx.chat_id, a["id"])
    await ctx.reply(f"✅ 노래 도우미({esc(a.get('name') or '')})가 방에 있어요. <code>.노래 제목</code> 으로 틀어요.")


c_skip = _ctl("skip")
c_pause = _ctl("pause")
c_resume = _ctl("resume")
c_mute = _ctl("mute")
c_unmute = _ctl("unmute")
c_seek = _ctl("seek", arg="sec")
c_end = _ctl("end")
c_volume = _ctl("volume", arg="n")
c_loop = _ctl("loop", arg="n")
# 명령 등록은 sodam/commands.py (_music 이 늦게 이 모듈을 부름 — commands → menu → panels 순환 방지)


# ── 지금 곡 카드 버튼 mu:<op> ───────────────────────────
async def on_button(svc, bot, q, parts) -> None:
    op = parts[0] if parts else ""
    chat_id = q.message.chat_id
    role = await svc.perms.role(bot, chat_id, q.from_user.id)
    if op == "queue":
        text = re.sub(r"<[^>]+>", "", await queue_text(svc, chat_id))
        await q.answer(text[:190], show_alert=True)
        return
    if op not in ("pause", "resume", "skip", "end"):
        await q.answer()
        return
    out = await control(svc, chat_id, q.from_user.id, role, op)
    await q.answer(re.sub(r"<[^>]+>", "", out)[:190], show_alert=not out.startswith(("⏭", "⏸", "▶️", "⏹")))
    if op in ("skip", "end") and out.startswith(("⏭", "⏹")):
        try:
            await bot.send_message(chat_id, f"{out} ({esc(user_name(q.from_user))})", parse_mode="HTML")
        except TelegramError:
            pass


hooks.add_callback_handler("mu", on_button)


# ── 끝난 세션 안내 (30초 틱) ─────────────────────────────
async def tick(svc, bot) -> None:
    for row in await musicq.ended_unnotified(svc.db):
        if not await musicq.mark_notified(svc.db, row["id"]):
            continue
        why = END_REASON.get(row["reason"] or "")
        if not why:                                       # end(누가 끝냄 — 이미 답함)·restart(이어서 틂)
            continue
        try:
            await bot.send_message(row["chat_id"], f"🎵 노래 도우미가 음성채팅에서 나왔어요 ({why}, {row['tracks']}곡).")
        except TelegramError:
            pass

hooks.add_tick_hook(tick)


# ── AI 도구 ────────────────────────────────────────────
async def t_music(ctx: tools.ToolCtx, a: dict) -> str:
    action = str(a.get("action") or "play")
    if ctx.chat_id > 0:
        return "1:1 에선 노래를 못 틂 — 그룹방에서 '.노래 제목' 또는 '소담아 ○○ 틀어줘'."
    if action == "play":
        why = await play(ctx.svc, ctx.bot, ctx.chat_id, ctx.caller, ctx.role, str(a.get("query") or ""),
                         getattr(ctx, "request_msg", None))
        if why:
            await ctx.bot.send_message(ctx.chat_id, why, parse_mode="HTML")
            ctx.quiet = True
            return f"거절 안내를 방에 그대로 보냄 ({re.sub(r'<[^>]+>', '', why)[:60]}). 따로 답하지 않음."
        ctx.quiet = True
        return "노래 신청 접수 — '찾는 중' 글이 방에 올라갔고 결과(재생 시작·대기열 추가)는 그 글이 바뀜. 답은 보내지 않음."
    if action == "queue":
        return re.sub(r"<[^>]+>", "", await queue_text(ctx.svc, ctx.chat_id))
    if action == "now":
        cur = await musicq.current(ctx.svc.db, ctx.chat_id)
        return f"지금 곡: {cur['title']} ({musicq.fmt_dur(cur['duration'])}, 신청 {cur['by_name']})" if cur else "지금 틀고 있는 노래 없음."
    op = {"stop": "end"}.get(action, action)
    if op not in ("skip", "pause", "resume", "end", "volume"):
        return "모르는 동작."
    value = a.get("value")
    out = await control(ctx.svc, ctx.chat_id, ctx.caller.id, ctx.role, op,
                        int(value) if str(value or "").isdecimal() else (100 if op == "volume" else None))
    return re.sub(r"<[^>]+>", "", out)


tools.register_tool(tools.Tool(
    "music",
    "음성채팅에서 노래 틀기 (소담 뮤직봇, 유튜브). '아이유 밤편지 틀어줘'·'노래 틀어'·'유튜브 링크 틀어' → play(query=제목·가수·링크 그대로). "
    "'다음 곡'·'스킵' → skip · '멈춰'·'일시정지' → pause · '다시 틀어' → resume · '노래 꺼'·'노래 끝' → stop · "
    "'대기열'·'뭐 나와?' → queue / now · '소리 줄여 50' → volume(value). 음악 파일에 답장하며 '이거 틀어' → play(query 비움). "
    "AI 목소리 대화(voice_call)와 다름.",
    {"action": {"type": "string", "enum": ["play", "skip", "pause", "resume", "stop", "queue", "now", "volume"]},
     "query": {"type": "string", "description": "노래 제목·가수 또는 유튜브 링크 (요청 글 그대로)"},
     "value": {"type": "integer", "description": "volume 0~200"}},
    ["action"], t_music, where="room"))


# ── 방 허브 🎵 ─────────────────────────────────────────
menu.register_preset("music_who", [("all", "누구나"), ("admin", "관리자만")], "mus")
menu.register_preset("music_ctrl", [("requester", "관리자·신청자"), ("all", "누구나")], "mus")
menu.register_toggle("music_enabled", "mus")
menu.register_toggle("music_bare", "mus")


async def s_room(c: PanelCtx) -> Screen:
    s = await c.svc.db.get_settings(c.cid)
    cur = await musicq.current(c.svc.db, c.cid)
    n = len(await musicq.waiting(c.svc.db, c.cid))
    lines = ["🎵 <b>뮤직봇</b> — 소담이 음성채팅에서 노래를 틀어요 (유튜브 제목·링크·음악 파일).",
             f"지금: {('🎶 ' + esc(cur['title'])[:60] + f' · 대기 {n}곡') if cur else '쉬는 중'}", "",
             f"뮤직봇: <b>{'켜짐' if s['music_enabled'] else '꺼짐'}</b>",
             f"노래 신청: <b>{settings.render('music_who', s['music_who'])}</b>",
             f"넘기기·멈춤: <b>{settings.render('music_ctrl', s['music_ctrl'])}</b>",
             f"@ 없는 <code>/play</code>: <b>{'받음' if s['music_bare'] else '안 받음'}</b> "
             "(다른 음악봇이 있는 방은 끔 — 명령 메뉴에서 고르면 @소담이 붙어요)", "",
             "명령: <code>.노래 제목</code> · <code>.스킵</code> · <code>.일시정지</code> · <code>.다시재생</code> · "
             "<code>.대기열</code> · <code>.빼기 번호</code> · <code>.이동 초</code> · <code>.볼륨 50</code> · <code>.노래끝</code>",
             "도우미 계정이 방에 있어야 하고, 음성채팅이 꺼져 있으면 '음성채팅 관리' 권한으로 직접 켜요."]
    rows = [[B(("✅ 켜짐" if s["music_enabled"] else "❌ 꺼짐") + " — 바꾸기",
               f"m:t:{c.cid}:music_enabled:{0 if s['music_enabled'] else 1}")],
            menu._preset_row(s, c.cid, "music_who"), menu._preset_row(s, c.cid, "music_ctrl"),
            [B(("✅" if s["music_bare"] else "❌") + " @ 없는 /play 받기",
               f"m:t:{c.cid}:music_bare:{0 if s['music_bare'] else 1}")],
            menu._back(c.cid)]
    return Screen("\n".join(lines), menu._kb(rows))


menu.register_hub(HubItem(58, "mus", "🎵 뮤직봇", ADMIN))
menu.register_screen("mus", s_room, ADMIN)


# ── 오너 메인 🎵 (유튜브 상태·쿠키) ───────────────────────
COOKIE_MAX = 1024 * 1024


def cookie_dir(svc) -> Path:
    return data_dir(svc) / "yt_cookies"


async def s_owner(c: PanelCtx) -> Screen:
    if c.uid not in await c.svc.perms.owners():
        return Screen("오너만 볼 수 있어요.", None)
    h = await c.svc.db.get_state(0, musicq.HEALTH_KEY) or {}
    files = sorted(cookie_dir(c.svc).glob("*.txt")) if cookie_dir(c.svc).exists() else []
    now = time.time()

    def ago(ts) -> str:
        if not ts:
            return "없음"
        m = int((now - ts) // 60)
        return f"{m}분 전" if m < 120 else f"{m // 60}시간 전" if m < 2880 else f"{m // 1440}일 전"
    rows = await c.svc.db._all("SELECT COUNT(*) n FROM music_queue WHERE ts>?", (int(now) - 7 * 86400,))
    lines = ["🎵 <b>뮤직봇 (오너)</b>",
             "노래는 유튜브에서 받아 음성 도우미 계정이 틀어요. 서버 IP 를 유튜브가 '봇이냐?'로 막으면(지금 서버는 쿠키 없이 막힘 — 실측) "
             "<b>SoundCloud 에서 같은 노래</b>를 찾아 대신 틀어요 (원곡이 잠긴 곡은 못 찾음). 유튜브로 다 되게 하려면 "
             "<b>버리는 구글 계정</b>의 유튜브 쿠키(cookies.txt)를 넣어 주세요 — 본 계정 쿠키는 정지될 수 있어 쓰지 마세요.", "",
             f"마지막 성공: {ago(h.get('ok_ts'))}",
             f"마지막 막힘: {ago(h.get('err_ts'))}" + (f" — {esc(h.get('err', ''))[:80]}" if h.get("err_ts") else ""),
             f"쿠키 파일: {len(files)}개" + (f" (가장 최근 {ago(max(f.stat().st_mtime for f in files))})" if files else ""),
             f"최근 7일 신청: {rows[0]['n'] if rows else 0}곡",
             "", await voice.status_line(c.svc)]
    kb = [[B("🍪 쿠키 넣기", "m:muck")]]
    if files:
        kb.append([B("🗑 쿠키 전부 지우기", "m:mucx")])
    kb.append([B("⬅️ 처음으로", "m:home")])
    return Screen("\n".join(lines), menu._kb(kb))


async def r_cookie(c: PanelCtx) -> Screen:
    if c.uid not in await c.svc.perms.owners():
        return Screen("오너만 할 수 있어요.", None)
    c.svc.inputs[c.uid] = PendingInput("muc", 0)
    return Screen("🍪 <b>유튜브 쿠키 넣기</b>\n"
                  "1) PC 크롬 <b>시크릿 창</b>에서 버리는 구글 계정으로 youtube.com 로그인\n"
                  "2) 확장 프로그램 'Get cookies.txt LOCALLY' 로 youtube.com 쿠키를 <b>Netscape 형식 .txt</b> 로 저장\n"
                  "3) 시크릿 창은 로그아웃하지 말고 그냥 닫기 (로그아웃하면 쿠키가 무효)\n"
                  "4) 그 .txt 파일을 여기 1:1 로 보내기 — 받자마자 메시지는 지워요.\n\n5분 안에 · 그만두려면 <code>취소</code>",
                  menu._kb([[B("❌ 취소", "m:mu")]]))


def valid_cookies(text: str) -> bool:
    """Netscape cookies.txt (탭 7칸). 크롬 확장은 HttpOnly 쿠키를 '#HttpOnly_.youtube.com …' 으로 씀 — 주석 아님."""
    lines = [ln.removeprefix("#HttpOnly_") for ln in text.splitlines()
             if ln.strip() and (not ln.startswith("#") or ln.startswith("#HttpOnly_"))]
    yt = [ln for ln in lines if ln.split("\t")[0].lstrip(".").endswith(("youtube.com", "google.com"))]
    return bool(yt) and all(len(ln.split("\t")) >= 7 for ln in yt)


async def i_cookie(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    doc = msg.document
    if doc is None:
        return False, "🍪 cookies.txt <b>파일</b>로 보내 주세요."
    try:
        await msg.delete()
    except TelegramError:
        pass
    if (doc.file_size or 0) > COOKIE_MAX:
        return False, "파일이 너무 커요 (1MB 까지)."
    try:
        raw = await (await c.bot.get_file(doc.file_id)).download_as_bytearray()
        text = bytes(raw).decode("utf-8", errors="replace")
    except TelegramError as e:
        return False, f"파일을 못 받았어요 ({esc(e.message)})."
    if not valid_cookies(text):
        return False, "유튜브 쿠키(Netscape 형식, youtube.com 줄)가 아니에요."
    d = cookie_dir(c.svc)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    path = d / f"cookies_{int(time.time())}.txt"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    log.info("유튜브 쿠키 저장 (%d줄)", len(text.splitlines()))
    return True, "✅ 쿠키를 넣었어요. 다음 노래부터 써요."


async def r_cookie_clear(c: PanelCtx) -> Screen:
    if c.uid not in await c.svc.perms.owners():
        return Screen("오너만 할 수 있어요.", None)
    d = cookie_dir(c.svc)
    for f in d.glob("*.txt") if d.exists() else ():
        f.unlink(missing_ok=True)
    s = await s_owner(c)
    return Screen(s.text, s.kb, toast="쿠키를 지웠어요")


menu.register_main(94, "mu", "🎵 뮤직봇", OWNER)
menu.register_route("mu", Route(s_owner, OWNER, scoped=False))
menu.register_route("muck", Route(r_cookie, OWNER, scoped=False))
menu.register_route("mucx", Route(r_cookie_clear, OWNER, scoped=False))
menu.register_input("muc", "", "mu", i_cookie, s_owner, media=True, need=OWNER)
