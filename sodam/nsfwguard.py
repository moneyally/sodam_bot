"""🔞 음란물 자동 감지·삭제 (2026-11-06 오너 급비상: 누가 음란물 스티커·사진 테러).

스티커·사진·GIF·영상이 올라오면 OpenAI omni-moderation(무료·이미지 'sexual' 점수, 하루 수천 건)으로 재서
기준 넘으면 지운다. 방별 nsfw_mode off(기본)/notify(알림만)/delete(지우고 알림)/mute(+1일 뮤트)/ban(+밴).
VPS(노래·음성과 CPU 경쟁) 라 로컬 NSFW 모델(ONNX/torch)은 CPU 부담 커서 안 씀 — moderation API 가 무료·빠름.

회피를 뚫는 장치 (2026-11-06, 오너 '한계는 뚫으라고 있는 것' — 조사: dHash 재인코딩 내성·omni 토큰한도·모자이크 공격):
  ① 지문(file_unique_id) 즉시삭제 — 같은 조각 재도배는 API 없이.
  ② 스티커팩 통째 차단(nsfw_set) — 조각낸 신체사진은 같은 커스텀 세트라, 한 팩에서 조각 2개↑ 걸리거나
     관리자가 [🧩 팩 전체 차단] 누르면 그 세트 전부 즉시삭제(AI·fetch 없이). 실수로 공개팩 통째 막는 것 방지 위해 자동은 2개↑일 때만.
  ③ dHash(64비트·해밍≤8) — 재인코딩·리사이즈·화질 낮춰 다시 올린 '똑같은 사진'을 로컬에서 즉시(파일 id 바뀐 회피 뚫음).
  ④ 모자이크 감지 — 한 사람이 짧은 시간에 미디어 우르르(BURST) 올리면 기준을 낮춰(조각은 점수 낮으니) 잡음.
  ⑤ 동시 폭주 합치기(coalescing) — 같은 조각이 한꺼번에 쏟아져도 API 는 한 번만(무료 분당 토큰 한도 보호).
  ⑥ mute/ban 모드 — 걸린 순간 범인 입막음 → 매크로 도배 원천 차단.

오탐 복구: 관리자가 '괜찮음' 누르면 그 방에선 그 조각 다시 안 지움(nsfw_ok).
제외: **봇 오너만** + 봇·익명/채널 글(사람 특정·제재 불가). **관리자·자유 멤버도 검사.** 구독과 무관하게 동작.
알림: '사용자 차단' 권한 관리자 1:1 (incidents, 같은 사람 한 통) + 버튼.
"""
from __future__ import annotations

import asyncio
import io
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from . import db as dbmod
from . import hooks, incidents, nsfwlocal, vision
from .permissions import Role
from .settings import register_setting
from .util import esc, mention, user_name

log = logging.getLogger(__name__)

THRESHOLD = 0.70          # 평소 sexual 기준 (포르노는 보통 0.9+, 0.7 은 안전 margin)
BURST_THRESHOLD = 0.45    # 도배(모자이크) 중엔 기준 낮춤 — 조각은 점수가 낮아서
SKIN_SEXUAL = 0.30        # 조각(신체 일부) 잡기: AI 가 이만큼이라도 의심하고 +
SKIN_RATIO = 0.60         # 화면의 이 비율 이상이 살색이면(=신체 조각) 기준 낮춰 잡음 (단, 자동 밴은 안 함)
LOCAL_SKIN_GATE = 0.35    # 살색이 이만큼↑ 이고 omni 가 안 걸렸으면 NudeNet(부위탐지) 한번 돌림 (호출 수 제한)
LOCAL_MIN = 0.45          # NudeNet 노출부위 점수 이상이면 음란물로 봄 (조각도 잡힘)
LOCAL_SANCTION = 0.85     # NudeNet 이 이만큼 확신하면 자동 뮤트/밴도 허용
BURST_N, BURST_SEC = 4, 20   # 한 사람이 BURST_SEC 초 안에 미디어 BURST_N 개↑ = 도배로 봄
SANCTION_MIN_SCORE = 0.85    # 자동 뮤트/밴은 이 점수 이상일 때만 (오탐으로 밴하는 것 방지)
SET_HITS_TO_BLOCK = 2     # 같은 스티커팩에서 조각 이만큼 걸리면 팩 통째 차단(공개팩 오폭 방지)
PHASH_DIST = 8            # dHash 해밍 거리 이하면 '같은 사진'(재인코딩 내성, 조사 기준 5~10)
PHASH_SCAN = 4000         # dHash 비교 상한
CACHE_TTL = 3600
MUTE_MINUTES = 1440

MODES = {"off": "꺼짐", "notify": "알림만(안 지움)", "delete": "지우고 알림",
         "mute": "지우고 1일 뮤트", "ban": "지우고 밴"}
DELETE_MODES = {"delete", "mute", "ban"}

_CACHE: dict[str, tuple[float, float]] = {}   # file_unique_id -> (sexual, ts)
_LOCKS: dict[str, asyncio.Lock] = {}          # file_unique_id -> lock (동시 폭주 합치기)
_RECENT: dict[tuple[int, int], list[float]] = {}   # (방,사람) -> 최근 미디어 시각 (도배 감지)

register_setting("nsfw_mode", "off", "음란물 자동 차단",
                 choices={**{k: k for k in MODES}, "꺼짐": "off", "끄기": "off", "알림": "notify", "알림만": "notify",
                          "삭제": "delete", "지우기": "delete", "지우고 알림": "delete", "켜기": "delete", "on": "delete",
                          "뮤트": "mute", "지우고 뮤트": "mute", "밴": "ban", "지우고 밴": "ban"},
                 choice_labels=MODES)

dbmod.register_schema(
    "CREATE TABLE IF NOT EXISTS nsfw_bad (file_uid TEXT PRIMARY KEY, chat_id INTEGER, score REAL, ts INTEGER)")
dbmod.register_schema(   # 관리자가 '괜찮음' 한 조각 — 그 방에선 다시 안 지움
    "CREATE TABLE IF NOT EXISTS nsfw_ok (chat_id INTEGER NOT NULL, file_uid TEXT NOT NULL, "
    "PRIMARY KEY (chat_id, file_uid))")
dbmod.register_schema(   # 차단된 스티커 세트 (조각낸 신체사진 = 같은 커스텀 팩)
    "CREATE TABLE IF NOT EXISTS nsfw_set (set_name TEXT PRIMARY KEY, ts INTEGER)")
dbmod.register_schema(   # 팩에서 걸린 조각들 (distinct 수가 SET_HITS_TO_BLOCK 되면 팩 통째 차단)
    "CREATE TABLE IF NOT EXISTS nsfw_set_hits (set_name TEXT NOT NULL, file_uid TEXT NOT NULL, "
    "PRIMARY KEY (set_name, file_uid))")
dbmod.register_schema(   # 재인코딩 내성 지문 (dHash, 16진 문자열)
    "CREATE TABLE IF NOT EXISTS nsfw_phash (dhash TEXT PRIMARY KEY, chat_id INTEGER, ts INTEGER)")
dbmod.register_schema(
    "CREATE TABLE IF NOT EXISTS nsfw_alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, user_id INTEGER, "
    "msg_id INTEGER, name TEXT, file_uid TEXT, set_name TEXT, score REAL, deleted INTEGER, done TEXT, ts INTEGER)")


def media_obj(msg):
    """메시지의 사진·스티커·GIF·영상·동그라미·이미지 문서 (file_unique_id 있는 객체) 또는 None."""
    for attr in ("sticker", "animation", "video", "video_note"):
        o = getattr(msg, attr, None)
        if o is not None:
            return o
    if getattr(msg, "photo", None):
        return msg.photo[-1]
    doc = getattr(msg, "document", None)
    if doc is not None and (getattr(doc, "mime_type", "") or "").startswith("image/"):
        return doc
    return None


def _burst(chat_id: int, user_id: int) -> bool:
    """이 사람이 최근 BURST_SEC 초 안에 미디어를 BURST_N 개↑ 올렸나 (도배/모자이크)."""
    now = time.time()
    key = (chat_id, user_id)
    xs = [t for t in _RECENT.get(key, []) if now - t < BURST_SEC]
    xs.append(now)
    _RECENT[key] = xs[-BURST_N:]
    if len(_RECENT) > 5000:
        for k in [k for k, v in _RECENT.items() if not v or now - v[-1] > BURST_SEC]:
            _RECENT.pop(k, None)
    return len(xs) >= BURST_N


def _dhash(data: bytes) -> int | None:
    """64비트 difference hash (재인코딩·리사이즈에 강함). PIL 없거나 못 열면 None."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("L").resize((9, 8))
        px = list(im.getdata())
        bits = 0
        for row in range(8):
            for col in range(8):
                bits = (bits << 1) | (1 if px[row * 9 + col] > px[row * 9 + col + 1] else 0)
        return bits
    except Exception as e:
        log.debug("dhash 실패: %s", e)
        return None


def _ham(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _skin_ratio(data: bytes) -> float | None:
    """화면에서 살색 픽셀 비율 (0~1). 신체 일부 조각은 거의 살색이라, AI 가 애매할 때 조각을 잡는 신호.
    YCbCr 살색 규칙(조사: Cr 135~180·Cb 85~135). 64x64 로 줄여서 재서 가벼움(~ms). PIL 없으면 None."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGB").resize((64, 64))
        skin = 0
        px = list(im.getdata())
        for r, g, b in px:
            y = 0.299 * r + 0.587 * g + 0.114 * b
            cb = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b
            cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b
            if 135 <= cr <= 180 and 85 <= cb <= 135 and y > 60:
                skin += 1
        return skin / len(px) if px else 0.0
    except Exception as e:
        log.debug("skin ratio 실패: %s", e)
        return None


async def _phash_hit(svc, dh: int | None) -> bool:
    if dh is None:
        return False
    rows = await svc.db._all("SELECT dhash FROM nsfw_phash ORDER BY ts DESC LIMIT ?", (PHASH_SCAN,))
    for r in rows:
        try:
            if _ham(dh, int(r["dhash"], 16)) <= PHASH_DIST:
                return True
        except (ValueError, TypeError):
            pass
    return False


async def _set_blocked(svc, set_name: str | None) -> bool:
    if not set_name:
        return False
    return bool(await svc.db._one("SELECT 1 FROM nsfw_set WHERE set_name=?", (set_name,)))


async def on_message(svc, bot, msg, role) -> None:
    chat_id, user = msg.chat_id, msg.from_user
    # 오너만 제외 (관리자·자유 멤버도 검사). 봇·익명/채널 글은 사람 특정·제재가 안 돼 기술적 제외.
    if role >= Role.OWNER or chat_id >= 0 or not user or getattr(user, "is_bot", False) or getattr(msg, "sender_chat", None):
        return
    obj = media_obj(msg)
    if obj is None:
        return
    mode = (await svc.db.get_settings(chat_id)).get("nsfw_mode", "off")
    if mode == "off":
        return
    uid = getattr(obj, "file_unique_id", None)
    if not uid:
        return
    set_name = getattr(obj, "set_name", None)
    if await svc.db._one("SELECT 1 FROM nsfw_ok WHERE chat_id=? AND file_uid=?", (chat_id, uid)):
        return
    burst = _burst(chat_id, user.id)
    # ── 값싼 즉시 차단 (fetch·API 없이) ──
    if await _set_blocked(svc, set_name):        # 차단된 스티커팩의 조각
        return await act(svc, bot, msg, user, uid, 1.0, mode, set_name=set_name)
    known = await svc.db._one("SELECT score FROM nsfw_bad WHERE file_uid=?", (uid,))
    if known:                                    # 전에 걸린 그 조각 (지문)
        return await act(svc, bot, msg, user, uid, float(known["score"]), mode, set_name=set_name)
    # ── 처음 보는 미디어: 받아서 dHash → (닮은 음란물) 아니면 AI ──
    res = await _score(svc, bot, msg, uid)
    if res is None:
        return
    score, dh, skin, local = res
    eff = BURST_THRESHOLD if burst else THRESHOLD
    # 조각(신체 일부) 잡기 — 두 길:
    #  ① 살색 휴리스틱: AI 가 애매하게 의심(≥SKIN_SEXUAL)하는데 화면이 거의 살색.
    #  ② NudeNet 부위탐지: 노출 부위를 LOCAL_MIN 이상 확신 (omni 가 통째론 놓친 크롭·타일).
    fragment = (skin is not None and skin >= SKIN_RATIO and score >= SKIN_SEXUAL) or \
               (local is not None and local >= LOCAL_MIN)
    if score < eff and not fragment:
        return
    strong = score >= SANCTION_MIN_SCORE or (local is not None and local >= LOCAL_SANCTION)
    await act(svc, bot, msg, user, uid, score, mode, set_name=set_name, dh=dh, strong=strong)


async def _score(svc, bot, msg, uid: str):
    """(sexual 0~1, dHash|None, 살색비율|None, NudeNet점수|None) 또는 None. 동시 폭주는 lock 으로 한 번만."""
    hit = _CACHE.get(uid)
    if hit and time.time() - hit[1] < CACHE_TTL:
        return (hit[0], None, None, None)
    lock = _LOCKS.setdefault(uid, asyncio.Lock())
    try:
        async with lock:
            hit = _CACHE.get(uid)                       # 기다리는 동안 먼저 끝났으면
            if hit and time.time() - hit[1] < CACHE_TTL:
                return (hit[0], None, None, None)
            known = await svc.db._one("SELECT score FROM nsfw_bad WHERE file_uid=?", (uid,))
            if known:
                s = float(known["score"])
                _CACHE[uid] = (s, time.time())
                return (s, None, None, None)
            try:
                md = vision._media_of(msg)
                att = await vision._fetch(bot, msg, md) if md else None
            except Exception as e:
                log.warning("nsfw 이미지 받기 실패: %s", e)
                return None
            if not att or not getattr(att, "data", None):
                return None
            dh = _dhash(att.data)
            if await _phash_hit(svc, dh):               # 재인코딩된 같은 음란물 (API 없이)
                _CACHE[uid] = (1.0, time.time())
                return (1.0, dh, None, None)
            v = await svc.llm.moderate_image(att.data, att.mime or "image/jpeg")
            if v is None:
                return None
            s = v["sexual"]
            skin = local = None
            if s < THRESHOLD:        # omni 가 통째론 안 걸린 것만 조각 검사 (CPU 아낌)
                skin = _skin_ratio(att.data)
                if skin is not None and skin >= LOCAL_SKIN_GATE:   # 살색 어느 정도 → NudeNet 부위탐지 한번
                    local = await nsfwlocal.nude_score(att.data)
            _CACHE[uid] = (s, time.time())
            if len(_CACHE) > 8000:
                _CACHE.clear()
            return (s, dh, skin, local)
    finally:
        if uid in _LOCKS and not _LOCKS[uid].locked():
            _LOCKS.pop(uid, None)


async def _register_set_hit(svc, set_name: str, uid: str) -> bool:
    """팩에서 걸린 조각 기록. distinct 수가 SET_HITS_TO_BLOCK 되면 팩 통째 차단하고 True."""
    await svc.db._write("INSERT OR IGNORE INTO nsfw_set_hits(set_name, file_uid) VALUES(?,?)", (set_name, uid))
    row = await svc.db._one("SELECT COUNT(*) AS n FROM nsfw_set_hits WHERE set_name=?", (set_name,))
    if row and row["n"] >= SET_HITS_TO_BLOCK and not await _set_blocked(svc, set_name):
        await svc.db._write("INSERT OR IGNORE INTO nsfw_set(set_name, ts) VALUES(?,?)", (set_name, dbmod.now()))
        return True
    return False


async def act(svc, bot, msg, user, uid: str, score: float, mode: str, *, set_name=None, dh=None, strong=None) -> None:
    chat_id = msg.chat_id
    # 아는 것으로 기록 (다음엔 즉시) — 멱등
    await svc.db._write("INSERT OR REPLACE INTO nsfw_bad(file_uid, chat_id, score, ts) VALUES(?,?,?,?)",
                        (uid, chat_id, score, dbmod.now()))
    if dh is not None:
        await svc.db._write("INSERT OR IGNORE INTO nsfw_phash(dhash, chat_id, ts) VALUES(?,?,?)",
                            (f"{dh:016x}", chat_id, dbmod.now()))
    if set_name:
        await _register_set_hit(svc, set_name, uid)
    deleted = False
    if mode in DELETE_MODES and await svc.perms.bot_can_moderate(bot, chat_id):
        try:
            await msg.delete()
            deleted = True
        except TelegramError as e:
            log.info("nsfw delete failed %s: %s", chat_id, e)
    sanction = None
    if strong is None:
        strong = score >= SANCTION_MIN_SCORE
    if deleted and mode in ("mute", "ban") and strong:
        reason = f"음란물 자동 감지 (약 {int(score * 100)}%)"
        try:
            if mode == "mute":
                await svc.mod.mute(bot, chat_id, user.id, MUTE_MINUTES, None, reason)
            else:
                await svc.mod.ban(bot, chat_id, user.id, None, reason)
            sanction = mode
        except Exception as e:   # 보호멤버·권한 등 — 삭제는 이미 됐으니 넘어감
            log.info("nsfw sanction(%s) failed %s: %s", mode, chat_id, e)
    act_name = "nsfw_ban" if sanction == "ban" else "nsfw_mute" if sanction == "mute" else \
               "nsfw_del" if deleted else "nsfw_alert"
    await svc.db.log_mod(chat_id, None, user.id, act_name, f"sexual {int(score * 100)}%")   # 기록(📒)엔 항상 남김
    name = user_name(user)
    alert_id = await svc.db._write(   # 집계·기록용 (버튼은 알림만 모드에서만 보냄)
        "INSERT INTO nsfw_alerts(chat_id, user_id, msg_id, name, file_uid, set_name, score, deleted, ts) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (chat_id, user.id, msg.message_id, name[:64], uid, set_name, score, int(deleted), dbmod.now()))
    # 🔇 지우고/뮤트/밴 모드는 **완전 조용히** — 방 문구도, 관리자 1:1 알림도 안 보냄 (도배 때 알림 폭주·요청 폭주 방지, 오너 결정 2026-11-06).
    #    알림·버튼은 '알림만(notify)' 모드에서만 (incidents 가 같은 사람 10분 한 통으로 합침).
    if mode != "notify":
        return
    from .subscription import chat_title   # 늦게 import (순환 방지)
    body = alert_text(await chat_title(svc, chat_id), user.id, name, score, deleted, mode, sanction, set_name)
    kb = alert_kb(chat_id, alert_id, deleted, bool(set_name))
    admins = [a for a in await svc.perms.admin_users(bot, chat_id) if not getattr(a, "is_bot", False)]
    await incidents.open_or_bump(svc, bot, chat_id, "nsfw", f"nsfw:{user.id}", body, kb, admins, sub=user.id)


def alert_text(title, user_id, name, score, deleted, mode, sanction=None, set_name=None) -> str:
    if sanction == "ban":
        status = "지우고 <b>밴</b>했어요."
    elif sanction == "mute":
        status = "지우고 <b>1일 뮤트</b>했어요."
    elif deleted:
        status = "방에서 지웠어요."
    elif mode in DELETE_MODES:
        status = "⚠️ 봇에게 '메시지 삭제' 권한이 없어서 못 지웠어요 — 방에서 직접 지워주세요."
    else:
        status = "메시지는 방에 그대로 있어요 (알림만 모드)."
    extra = f"\n🧩 스티커팩: <code>{esc(set_name)}</code>" if set_name else ""
    return (f"🔞 <b>음란물로 판단된 미디어</b>\n"
            f"방: <b>{esc(title)}</b>\n"
            f"보낸 사람: {mention(user_id, name)} (ID <code>{user_id}</code>)\n"
            f"음란도: 약 {int(score * 100)}%{extra}\n"
            f"{status} 어떻게 할까요?")


def alert_kb(chat_id: int, alert_id: int, deleted: bool, has_set: bool) -> InlineKeyboardMarkup:
    cb = f"m:nsx:{chat_id}:{alert_id}:"
    b = InlineKeyboardButton
    first = [b("🚫 밴", callback_data=cb + "b"), b("🔇 뮤트 1일", callback_data=cb + "m")]
    if not deleted:
        first.insert(0, b("🗑 지우기", callback_data=cb + "d"))
    rows = [first]
    if has_set:
        rows.append([b("🧩 이 스티커팩 전체 차단", callback_data=cb + "s")])
    rows.append([b("✅ 오탐(이 조각 통과)", callback_data=cb + "t")])
    return InlineKeyboardMarkup(rows)


hooks.add_group_message_hook(on_message)
