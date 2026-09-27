"""🚀 빠른 설정 마법사 — 새 방 대표님이 설정 40개를 몰라도 5분 안에 방 종류에 맞게 켜 두게 (1:1, 그 방 TG 관리자·오너).

m:ob:<방>               1단계 방 종류 [💬 소통방][💱 거래·업자방][🎮 게임·이벤트방][📢 공지·채널 연결방][⚙️ 직접 할게요]
m:obt:<방>:<종류>       종류 고름 → DB 에 초안(onboard_state, 사람·방마다 1개, 30분) → 2단계
m:obq:<방>              2단계 (질문 3개, 초안에서 다시 그림)
m:oba:<방>:<번호>:<값>  답 고름 (목표값, 화이트리스트만) → 2단계
m:obp:<방>              3단계 미리보기 '지금 → 바꿀 값' + [✅ 적용][↩️ 뒤로][❌ 취소]
m:obx:<방>:<판>         적용: 초안 줄 DELETE 로 한 번만 차지 + 설정 JSON 한 번 쓰기 + 되돌림 기록 + mod_log 'onboard:<종류>'
                        를 db.atomic 하나로. <판> = 초안 판 번호 → 미리보기 뒤 답을 바꿨으면 옛 [✅ 적용] 은 거절
m:obu:<방>:<기록>       되돌리기 (10분, 한 번만): 그 사이 직접 바꾼 설정은 그대로 둠
m:obc:<방>              취소 (초안 삭제 → 관리 화면)

프리셋은 이미 있는 설정 키만 (가져올 때 _validate 가 DEFAULTS·coerce 로 확인 → 틀리면 import 실패).
보수적으로: 보통 대화를 지우는 쪽으로는 안 바꿈 (거래방은 링크 차단을 오히려 끄고 신규 3일만 막음, 게임방은 도배 기준 느슨).
그룹 허브 첫 줄에 🚀 버튼 + 한 번도 안 한 방이면 한 줄 안내 (menu.s_hub 를 감쌈), 봇 초대 때 대표님 1:1 에도 버튼
(subscription.DM_EXTRA_ROWS).
"""
from __future__ import annotations

import json
import time
from typing import Any

from telegram import InlineKeyboardButton

from .. import menu, subscription
# 프리셋이 쓰는 설정 키를 등록하는 모듈들 (패널 import 순서와 무관하게 _validate 전에 등록되게)
from .. import ai_settings, anomaly, botlink, farewell, gametime, moderation, namehist, raid, reports, scamguard, spamshield, tagnotify  # noqa: F401,E501
from ..casino import core as _casino_core  # noqa: F401
from ..db import register_schema
from ..menu import TG_ADMIN, B, HubItem, PanelCtx, Route, Screen
from ..settings import DEFAULTS, LABELS, coerce, render
from ..styles import STYLES
from ..util import esc
from . import log as logpanel

STATE_TTL = 30 * 60
UNDO_TTL = 10 * 60

# ── 프리셋 (이미 있는 설정 키만) ──────────────────────────
_BASE: dict[str, Any] = {
    "captcha_enabled": True, "recent_account_captcha": True, "cas_enabled": True, "raid_guard": True,
    "anomaly_mode": "notify", "impersonation_guard": True, "name_change_notice": True,
    "ai_enabled": True, "digest_hour": 21,
}
TYPES: dict[str, tuple[str, str]] = {
    "chat": ("💬 소통방", "멤버끼리 수다·정보 나누는 방. 인사·AI 대화 중심, 기본 보안."),
    "trade": ("💱 거래·업자방", "업자·거래 방. 사기 의심 알림·신규 링크 3일 금지, 링크는 기존 멤버 허용."),
    "game": ("🎮 게임·이벤트방", "게임·이벤트 방. 포인트 게임·장시간 게임 알림, 도배 기준 느슨하게."),
    "notice": ("📢 공지·채널 연결방", "채널 댓글·공지 방. 입장 인사 없이 조용히, 스팸 방어 위주."),
}
PRESETS: dict[str, dict[str, Any]] = {
    "chat": {**_BASE, "greet_enabled": True, "farewell_mode": "on", "style": "friendly", "ai_follow_up": True,
             "tag_notify": True, "link_filter": True, "newbie_link_hours": 24, "forward_filter": "newbie",
             "games_enabled": True},
    "trade": {**_BASE, "greet_enabled": True, "farewell_mode": "off", "style": "polite", "link_filter": False,
              "newbie_link_hours": 72, "forward_filter": "newbie", "scam_guard": True, "scam_action": "ask",
              "spamshield_mode": "shadow", "games_enabled": False, "casino_enabled": False},
    "game": {**_BASE, "greet_enabled": True, "farewell_mode": "off", "style": "free", "games_enabled": True,
             "casino_enabled": True, "gt_enabled": True, "gt_action": "notify", "botlink_mode": "observe",
             "flood_count": 10, "flood_seconds": 8, "flood_mute_minutes": 10, "dup_limit": 5,
             "link_filter": True, "newbie_link_hours": 24},
    "notice": {**_BASE, "greet_enabled": False, "delete_join_message": True, "farewell_mode": "off",
               "style": "polite", "link_filter": True, "newbie_link_hours": 72, "forward_filter": "newbie",
               "spamshield_mode": "shadow", "games_enabled": False, "casino_enabled": False},
}
_YN = [("1", "예", True), ("0", "아니오", False)]
_STYLE_Q = ("style", "AI 대답 말투는?", [("polite", "정중", "polite"), ("friendly", "친근", "friendly"),
                                          ("free", "자유분방(반말)", "free")])
_CAPTCHA_Q = ("captcha_enabled", "입장할 때 버튼 캡차로 스팸 봇을 걸러낼까요?", _YN)
# 종류별 가장 중요한 질문 3개: (설정 키, 질문, [(버튼 코드, 글자, 저장값)])
QUESTIONS: dict[str, list[tuple[str, str, list[tuple[str, str, Any]]]]] = {
    "chat": [_CAPTCHA_Q, ("farewell_mode", "나가는 멤버에게 인사할까요?", [("1", "예", "on"), ("0", "아니오", "off")]),
             _STYLE_Q],
    "trade": [_CAPTCHA_Q, ("link_filter", "기존 멤버 링크도 막을까요? (아니오 = 신규만 3일 금지)", _YN),
              ("scam_guard", "사기 의심 글(지갑주소·초대링크 등)을 관리자에게 알릴까요?", _YN)],
    "game": [("casino_enabled", "포인트 게임(!주사위 등)을 켤까요?", _YN),
             ("gt_enabled", "한 사람이 너무 오래 게임하면 알려드릴까요?", _YN), _STYLE_Q],
    "notice": [_CAPTCHA_Q, ("greet_enabled", "새로 온 사람에게 입장 인사할까요?", _YN),
               ("ai_enabled", "'소담아' 부르면 AI 가 대답할까요?", _YN)],
}


def _raw(v: Any) -> str:
    return ("on" if v else "off") if isinstance(v, bool) else str(v)


def _check(ok: bool, what: str) -> None:
    if not ok:
        raise ValueError(f"빠른 설정 프리셋 오류: {what}")


def _valid(k: str, v: Any) -> bool:
    try:
        return k in DEFAULTS and k in LABELS and type(v) is type(DEFAULTS[k]) and coerce(k, _raw(v)) == v
    except ValueError:
        return False


def _validate() -> None:
    """프리셋·질문 값이 전부 있는 설정 키이고 coerce 를 통과하는 값인지 (틀리면 import 에서 바로 실패)."""
    _check(set(PRESETS) == set(TYPES) == set(QUESTIONS), "종류 목록")
    for kind, preset in PRESETS.items():
        for k, v in preset.items():
            _check(_valid(k, v), f"{kind}.{k}={v!r}")
        _check(len(QUESTIONS[kind]) == 3, f"{kind} 질문 수")
        for k, _, opts in QUESTIONS[kind]:
            _check(k in preset and any(val == preset[k] for _, _, val in opts), f"{kind} 질문 {k}")
            for _, _, val in opts:
                _check(_valid(k, val), f"{kind} 질문 {k}={val!r}")


# ── DB ────────────────────────────────────────────────────
register_schema("""
CREATE TABLE IF NOT EXISTS onboard_state (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    kind    TEXT NOT NULL,
    answers TEXT NOT NULL,
    ver     INTEGER NOT NULL,
    expires INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS onboard_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    kind    TEXT NOT NULL,
    prev    TEXT NOT NULL,
    new     TEXT NOT NULL,
    ts      INTEGER NOT NULL,
    undone  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_onboard_log_chat ON onboard_log(chat_id, id);
""", migrate={"onboard_state": "composite", "onboard_log": "plain"})

for _k, (_label, _) in TYPES.items():
    logpanel.ACTIONS.setdefault(f"onboard:{_k}", f"🚀 빠른 설정 ({_label[2:]})")
logpanel.ACTIONS.setdefault("onboard_undo", "↩️ 빠른 설정 되돌림")


async def _state(c: PanelCtx) -> dict | None:
    row = await c.svc.db._one("SELECT * FROM onboard_state WHERE chat_id=? AND user_id=? AND expires>?",
                              (c.cid, c.uid, int(time.time())))
    if not row or row["kind"] not in PRESETS:
        return None
    return {"kind": row["kind"], "answers": json.loads(row["answers"]), "ver": row["ver"]}


async def onboarded(svc, cid: int) -> bool:
    return await svc.db._one("SELECT 1 FROM onboard_log WHERE chat_id=? AND undone=0 LIMIT 1", (cid,)) is not None


def _target(st: dict) -> dict[str, Any]:
    return {**PRESETS[st["kind"]], **st["answers"]}


def _write_settings(cn, cid: int, values: dict[str, Any]) -> None:
    """DB 스레드(atomic 안)에서 설정 JSON 을 한 번에 (db.set_setting 과 같은 형식: 기본값과 다른 것만)."""
    row = cn.execute("SELECT settings FROM chats WHERE chat_id=?", (cid,)).fetchone()
    cur = {**DEFAULTS, **(json.loads(row[0]) if row and row[0] else {}), **values}
    changed = {k: v for k, v in cur.items() if DEFAULTS.get(k) != v}
    cn.execute("UPDATE chats SET settings=? WHERE chat_id=?", (json.dumps(changed, ensure_ascii=False), cid))


def _read_settings(cn, cid: int) -> dict[str, Any]:
    row = cn.execute("SELECT settings FROM chats WHERE chat_id=?", (cid,)).fetchone()
    return {**DEFAULTS, **(json.loads(row[0]) if row and row[0] else {})}


def _forget_cache(svc, cid: int) -> None:
    svc.db._settings_cache.pop(cid, None)
    svc.db._settings_at.pop(cid, None)


# ── 화면 ──────────────────────────────────────────────────
def _expired(cid: int) -> Screen:
    return Screen("⌛ 빠른 설정 시간(30분)이 지났거나 이미 끝났어요. 처음부터 다시 골라주세요.",
                  menu._kb([[B("🚀 빠른 설정 처음부터", f"m:ob:{cid}")], menu._back(cid)]))


async def s_start(c: PanelCtx) -> Screen:
    cid = c.cid
    lines = [f"🚀 <b>빠른 설정</b> — {esc(await subscription.chat_title(c.svc, cid))}",
             "방 종류만 고르면 알맞은 설정을 추천해 드려요. 질문 3개 → 바뀌는 것 미리보기 → 적용 (10분 안엔 되돌리기 가능).", ""]
    lines += [f"{label} — {desc}" for label, desc in TYPES.values()]
    if await onboarded(c.svc, cid):
        lines += ["", "✅ 이 방은 이미 빠른 설정을 했어요. 다시 하면 고른 종류대로 덮어써요."]
    rows = [[B(label, f"m:obt:{cid}:{k}")] for k, (label, _) in TYPES.items()]
    st = await _state(c)
    if st:
        rows.insert(0, [B(f"▶️ 이어서 하기 ({TYPES[st['kind']][0]})", f"m:obq:{cid}")])
    rows.append([B("⚙️ 직접 할게요", f"m:g:{cid}")])
    return Screen("\n".join(lines), menu._kb(rows))


async def r_type(c: PanelCtx) -> Screen:
    kind = c.arg(0)
    if kind not in PRESETS:
        return Screen(None)
    await c.svc.db._write(
        "INSERT INTO onboard_state(chat_id, user_id, kind, answers, ver, expires) VALUES(?,?,?,?,1,?) "
        "ON CONFLICT(chat_id, user_id) DO UPDATE SET kind=excluded.kind, answers='{}', ver=onboard_state.ver+1, "
        "expires=excluded.expires", (c.cid, c.uid, kind, "{}", int(time.time()) + STATE_TTL))
    return await s_questions(c)


async def s_questions(c: PanelCtx) -> Screen:
    st = await _state(c)
    if not st:
        return _expired(c.cid)
    kind, cid, target = st["kind"], c.cid, _target(st)
    lines = [f"🚀 <b>빠른 설정</b> 2/3 — {TYPES[kind][0]}", "딱 3가지만 골라주세요 (● = 지금 고른 것).", ""]
    rows: list[list[InlineKeyboardButton]] = []
    for i, (key, question, opts) in enumerate(QUESTIONS[kind]):
        lines.append(f"{i + 1}. {esc(question)}")
        rows.append([B(("● " if target[key] == val else "") + label, f"m:oba:{cid}:{i}:{code}")
                     for code, label, val in opts])
    rows += [[B("👀 바뀌는 것 미리보기", f"m:obp:{cid}")],
             [B("↩️ 종류 다시 고르기", f"m:ob:{cid}"), B("❌ 취소", f"m:obc:{cid}")]]
    return Screen("\n".join(lines), menu._kb(rows))


async def r_answer(c: PanelCtx) -> Screen:
    st = await _state(c)
    if not st:
        return _expired(c.cid)
    qs = QUESTIONS[st["kind"]]
    i = int(c.arg(0)) if c.arg(0).isdecimal() else -1
    if not 0 <= i < len(qs):
        return Screen(None)
    key, _, opts = qs[i]
    val = next((v for code, _, v in opts if code == c.arg(1)), None)
    if val is None:
        return Screen(None)
    if st["answers"].get(key, PRESETS[st["kind"]][key]) != val:
        st["answers"][key] = val
        await c.svc.db._write("UPDATE onboard_state SET answers=?, ver=ver+1 WHERE chat_id=? AND user_id=?",
                              (json.dumps(st["answers"], ensure_ascii=False), c.cid, c.uid))
    return await s_questions(c)


def diff(current: dict[str, Any], target: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    """(키, 지금 값, 바꿀 값) — 다른 것만, 프리셋 순서대로."""
    return [(k, current[k], v) for k, v in target.items() if current.get(k) != v]


async def s_preview(c: PanelCtx) -> Screen:
    st = await _state(c)
    if not st:
        return _expired(c.cid)
    cid, target = c.cid, _target(st)
    changes = diff(await c.svc.db.get_settings(cid), target)
    lines = [f"🚀 <b>빠른 설정</b> 3/3 — {TYPES[st['kind']][0]}", ""]
    if changes:
        lines.append(f"이렇게 바뀌어요 ({len(changes)}개, 지금 → 바꿀 값):")
        lines += [f"• {esc(LABELS[k])}: {esc(render(k, old))} → <b>{esc(render(k, new))}</b>" for k, old, new in changes]
    else:
        lines.append("지금 설정이 이미 추천과 같아요. 적용하면 '빠른 설정 완료'로만 표시돼요.")
    if len(target) > len(changes):
        lines.append(f"(이미 맞는 설정 {len(target) - len(changes)}개는 그대로)")
    lines += ["", "적용 뒤 10분 안에는 [↩️ 되돌리기] 로 원래대로 돌릴 수 있어요."]
    rows = [[B("✅ 적용", f"m:obx:{cid}:{st['ver']}")],
            [B("↩️ 뒤로", f"m:obq:{cid}"), B("❌ 취소", f"m:obc:{cid}")]]
    return Screen("\n".join(lines), menu._kb(rows))


async def r_apply(c: PanelCtx) -> Screen:
    cid, uid, now = c.cid, c.uid, int(time.time())
    ver = int(c.arg(0)) if c.arg(0).isdecimal() else -1
    st = await _state(c)
    if st and st["ver"] != ver:   # 미리보기 뒤 답을 바꿈 → 본 적 없는 내용은 적용 안 함
        screen = await s_preview(c)
        screen.toast, screen.alert = "그 사이 답이 바뀌었어요. 새 미리보기를 확인하고 눌러주세요.", True
        return screen

    def run(cn):
        row = cn.execute("SELECT kind, answers FROM onboard_state WHERE chat_id=? AND user_id=? AND ver=? AND expires>?",
                         (cid, uid, ver, now)).fetchone()
        if not row or row[0] not in PRESETS:
            return None
        # 한 번만: 초안 줄을 지운 쪽만 적용 (두 번 빨리 눌러도 1번)
        if cn.execute("DELETE FROM onboard_state WHERE chat_id=? AND user_id=? AND ver=?", (cid, uid, ver)).rowcount != 1:
            return None
        kind = row[0]
        target = {**PRESETS[kind], **json.loads(row[1])}
        cur = _read_settings(cn, cid)
        changes = diff(cur, target)
        prev = {k: old for k, old, _ in changes}
        new = {k: v for k, _, v in changes}
        if new:
            _write_settings(cn, cid, new)
        log_id = cn.execute("INSERT INTO onboard_log(chat_id, user_id, kind, prev, new, ts) VALUES(?,?,?,?,?,?)",
                            (cid, uid, kind, json.dumps(prev, ensure_ascii=False), json.dumps(new, ensure_ascii=False),
                             now)).lastrowid
        cn.execute("INSERT INTO mod_log(chat_id, actor_id, target_id, action, detail, ts) VALUES(?,?,?,?,?,?)",
                   (cid, uid, None, f"onboard:{kind}", f"{len(new)}개 변경: " + ", ".join(new)[:280], now))
        return kind, new, log_id

    done = await c.svc.db.atomic(run)
    _forget_cache(c.svc, cid)
    if not done:
        return Screen("이미 적용했거나 시간이 지난 빠른 설정이에요.",
                      menu._kb([[B("🚀 빠른 설정 다시", f"m:ob:{cid}")], menu._back(cid)]), toast="이미 처리됐어요.")
    kind, new, log_id = done
    lines = [f"✅ <b>빠른 설정 완료</b> — {TYPES[kind][0]} ({len(new)}개 바꿈)", "",
             "<b>이제 이렇게 써 보세요</b>",
             f"1️⃣ 방에서 <code>{esc(c.svc.cfg.call_names[0])} 오늘 요약해줘</code> — 오늘 대화 한눈에",
             "2️⃣ 📝 방 안내 — 규칙·자주 묻는 말을 적어 두면 소담이가 대신 안내해요",
             "3️⃣ 🗓️ 예약 — 매일 정해진 시각에 공지·알림 올리기"]
    rows = []
    if new:
        lines += ["", "마음에 안 들면 10분 안에 [↩️ 되돌리기] (한 번만)."]
        rows.append([B("↩️ 되돌리기 (10분)", f"m:obu:{cid}:{log_id}")])
    rows += [[B("📝 방 안내", f"m:rules:{cid}"), B("🗓️ 예약", f"m:sc:{cid}")], menu._back(cid)]
    return Screen("\n".join(lines), menu._kb(rows), toast="✅ 적용했어요")


async def r_undo(c: PanelCtx) -> Screen:
    cid, uid, now = c.cid, c.uid, int(time.time())
    log_id = int(c.arg(0)) if c.arg(0).isdecimal() else -1

    def run(cn):
        row = cn.execute("SELECT kind, prev, new FROM onboard_log WHERE id=? AND chat_id=?", (log_id, cid)).fetchone()
        if not row or cn.execute("UPDATE onboard_log SET undone=1 WHERE id=? AND chat_id=? AND undone=0 AND ts>=?",
                                 (log_id, cid, now - UNDO_TTL)).rowcount != 1:
            return None
        prev, new = json.loads(row[1]), json.loads(row[2])
        cur = _read_settings(cn, cid)
        back = {k: prev[k] for k in prev if k in new and cur.get(k) == new[k]}   # 그 사이 직접 바꾼 건 그대로
        if back:
            _write_settings(cn, cid, back)
        cn.execute("INSERT INTO mod_log(chat_id, actor_id, target_id, action, detail, ts) VALUES(?,?,?,?,?,?)",
                   (cid, uid, None, "onboard_undo", f"{len(back)}개 되돌림 ({row[0]})", now))
        return back, len(prev) - len(back)

    done = await c.svc.db.atomic(run)
    _forget_cache(c.svc, cid)
    if not done:
        return Screen("되돌리기 시간(10분)이 지났거나 이미 되돌렸어요. 설정은 관리 화면에서 하나씩 바꿀 수 있어요.",
                      menu._kb([menu._back(cid)]), toast="되돌릴 수 없어요.")
    back, kept = done
    text = f"↩️ 빠른 설정 전으로 {len(back)}개 되돌렸어요."
    if kept:
        text += f"\n(그 사이 직접 바꾼 {kept}개는 그대로 뒀어요.)"
    return Screen(text, menu._kb([[B("🚀 빠른 설정 다시", f"m:ob:{cid}")], menu._back(cid)]), toast="되돌렸어요")


async def r_cancel(c: PanelCtx) -> Screen:
    await c.svc.db._write("DELETE FROM onboard_state WHERE chat_id=? AND user_id=?", (c.cid, c.uid))
    screen = await menu.s_hub(c)
    screen.toast = "빠른 설정을 취소했어요. 아무것도 안 바뀌었어요."
    return screen


# ── 진입점 ────────────────────────────────────────────────
HINT = "🚀 처음이세요? <b>빠른 설정</b>으로 방 종류만 고르면 1분 만에 알맞게 켜 드려요."
_orig_hub = menu.s_hub


async def s_hub(c: PanelCtx) -> Screen:
    """그룹 허브 + 한 번도 빠른 설정을 안 한 방이면 한 줄 안내 (🚀 버튼이 보이는 사람에게만)."""
    screen = await _orig_hub(c)
    mine = f"m:ob:{c.cid}"
    if screen.text and screen.kb and any(b.callback_data == mine for r in screen.kb.inline_keyboard for b in r) \
            and not await onboarded(c.svc, c.cid):
        head, _, rest = screen.text.partition("\n")
        screen.text = f"{head}\n{HINT}\n{rest}" if rest else f"{head}\n{HINT}"
    return screen


async def _dm_rows(svc, chat_id: int, user_id: int) -> list[list[InlineKeyboardButton]]:
    """봇 초대 때 대표님 1:1 (subscription.send_panel_dm) — 아직 안 한 방이면 🚀 버튼."""
    return [] if await onboarded(svc, chat_id) else [[B("🚀 빠른 설정 (방 종류만 고르면 끝)", f"m:ob:{chat_id}")]]


async def _features_row(c: PanelCtx) -> list[list[InlineKeyboardButton]]:
    return [[B("🚀 빠른 설정 (방 종류별 추천)", f"m:ob:{c.cid}")]] \
        if await menu._allowed(c.svc, c.bot, c.cid, c.uid, TG_ADMIN) else []


_validate()
if not getattr(menu.s_hub, "_onboard", False):
    s_hub._onboard = True
    menu.s_hub = s_hub                 # group_panel(딥링크·.설정)·다른 화면이 부르는 허브도 안내가 붙게
    menu.register_screen("g", s_hub)   # 권한은 원래와 같음(ADMIN)
menu.register_hub(HubItem(1, "ob", "🚀 빠른 설정 (방 종류별 추천)", TG_ADMIN, wide=True))
menu.register_screen("ob", s_start, TG_ADMIN)
menu.register_route("obt", Route(r_type, TG_ADMIN))
menu.register_route("obq", Route(s_questions, TG_ADMIN))
menu.register_route("oba", Route(r_answer, TG_ADMIN))
menu.register_route("obp", Route(s_preview, TG_ADMIN))
menu.register_route("obx", Route(r_apply, TG_ADMIN, fresh=True))
menu.register_route("obu", Route(r_undo, TG_ADMIN, fresh=True))
menu.register_route("obc", Route(r_cancel, TG_ADMIN))
menu.register_screen_extra("f", _features_row)
if _dm_rows not in subscription.DM_EXTRA_ROWS:
    subscription.DM_EXTRA_ROWS.append(_dm_rows)
