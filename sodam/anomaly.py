"""🧭 이상징후 자동 감지 — 관리자에게 '알림·제안'만, 자동 제재는 하지 않는다 (AI 비용 0).

방마다 최근 WINDOW(10분) 동안의 신호를 메모리에서 센다 (방당 크기 제한, 알림을 보낼 때만 DB 에 저장):
  ① 입장 몰림     : 10분 입장 수 ≥ 민감도의 N명 이고 평소(최근 7일 10분 평균)의 k배 이상
  ② 비슷한 이름   : 들어온 사람 이름에서 숫자·이모지·띄어쓰기·기호를 빼고 소문자로 같은 무리 / @아이디 앞부분(끝 숫자·_ 제외)이 같은 무리
  ③ 같은 링크 반복: 같은 도메인(또는 같은 t.me 초대링크)이 M번 이상, 2명 이상에게서. 링크 규칙에 지워진 메시지도 셈
                   (handlers 가 관리 검사 전에 messages 표에 적어 두니까 평가 때 거기서 읽음. 허용 도메인은 제외)
  ④ 새 계정 비율 : 들어온 사람 중 accountage.is_recent 비율 (5명 이상일 때만, 혼자로는 알림 못 넘김)
  ⑤ 신규 멤버 도배: 들어온 지 NEWBIE_MINUTES(30분) 안 된 사람들의 메시지 수 (3명 이상)
관리자·봇관리자·오너·자유 멤버·봇은 신호에서 뺀다 (입장 훅은 handlers 가 관리자·자유 멤버를 이미 거름).
점수(신호별 점수 합) ≥ SCORE_MIN 이면 알림. 방마다 COOLDOWN(30분)에 1번·하루 DAILY_CAP(5)번 — 한 번의 db.atomic 으로 차지.
대량 입장 방어(raid.py)가 켜져 있으면 입장 신호(①②④)는 점수에서 빼고 '방어 중'이라고만 적는다 (이중 알림 방지 — raid 가 이미 알림).

알림 = 그 방 텔레그램 관리자 중 '사용자 차단' 권한(permissions.may)이 있는 사람에게 1:1 (1:1 막힘은 건너뜀) + 버튼
  [🔍 상세 보기] [🛡️ 보안 강화] [🙈 무시] (panels/anomaly.py, m:anmx). 누를 때 권한을 다시 확인.
보안 강화 = anomaly_harden_hours 시간 동안: 대량 입장 방어 모드(새 입장자 전원 캡차 또는 방 설정대로 내보내기, raid.py 가
  chat_state 로 재시작 뒤에도 이어서 끝냄) + 신규 링크 금지 24시간 이상 + 신규 전달 막기. 원래 값·끝나는 시각은
  chat_state(anomaly_harden)에 저장 → 시간이 되면 자동으로 되돌림 (tick / 이 방의 다음 메시지·입장 / 화면 열기 / 프로세스 안 타이머).
  관리자가 그 사이 직접 바꾼 설정은 건드리지 않는다.
평가는 방마다 EVAL_GAP 초에 한 번 (몰린 뒤 조용해져도 한 번 더 평가하도록 지연 평가 1개 예약) → 메시지당 O(1).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from . import accountage, free, hooks, incidents, raid
from . import db as dbmod
from .permissions import Role, may
from .security import _TLD
from .settings import register_setting
from .util import esc, user_name

log = logging.getLogger(__name__)

WINDOW = 600              # 보는 구간 (초)
EVAL_GAP = 5.0            # 방마다 평가 최소 간격 (초)
FOLLOW_UP = 60.0          # 입장이 몰려 있는 동안 다시 평가하는 간격 (초)
COOLDOWN = 1800           # 방마다 알림 간격
DAILY_CAP = 5             # 방마다 하루 알림 수
SCORE_MIN = 50            # 이 점수 이상이면 알림
NEWBIE_MINUTES = 30       # '신규 멤버' 기준
MAXQ = 500                # 방마다 기억하는 입장·링크·메시지 수 상한
MAX_ROOMS = 5000
BASELINE_DAYS = 7
BASELINE_TTL = 3600       # 평소 입장 수 캐시
RECENT_MIN_JOINS = 5      # 새 계정 비율은 이만큼 들어왔을 때만
HARDEN_KEY = "anomaly_harden"
NEWBIE_LINK_HOURS = 24    # 보안 강화 때 신규 링크 금지 최소 시간
DETAIL_USERS = 30

# 신호 점수 (합이 SCORE_MIN 이상이면 알림). 입장 몰림·링크·신규 도배는 아주 크면 혼자서도 넘고, 이름·새 계정은 보조 신호
PTS_SURGE, PTS_SURGE_BIG = 35, 15
PTS_NAMES = 30
PTS_LINKS, PTS_LINKS_BIG = 40, 10
PTS_FLOOD, PTS_FLOOD_BIG = 30, 10
PTS_RECENT = 20


@dataclass(frozen=True)
class Level:
    label: str
    joins: int        # 입장 몰림 최소 인원 (10분)
    factor: float     # 평소 10분 평균의 몇 배
    names: int        # 비슷한 이름 무리 크기
    links: int        # 같은 링크 횟수
    flood: int        # 신규 멤버 메시지 수
    recent: float     # 새 계정 비율


LEVELS = {
    "sensitive": Level("민감", 8, 3, 4, 4, 8, 0.6),
    "normal": Level("보통", 15, 5, 6, 6, 15, 0.7),
    "relaxed": Level("둔감", 30, 8, 10, 10, 30, 0.8),
}
MODES = {"off": "끔", "notify": "관리자 알림"}

register_setting("anomaly_mode", "notify", "이상징후 감지",
                 choices={"off": "off", "끔": "off", "끄기": "off", "notify": "notify", "알림": "notify", "켬": "notify"},
                 choice_labels=MODES)
register_setting("anomaly_level", "normal", "이상징후 민감도",
                 choices={**{k: k for k in LEVELS}, **{v.label: k for k, v in LEVELS.items()}},
                 choice_labels={k: v.label for k, v in LEVELS.items()})
register_setting("anomaly_harden_hours", 3, "보안 강화 시간(시간)", range_=(1, 24))

dbmod.register_schema("""
CREATE TABLE IF NOT EXISTS anomaly_alerts (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    ts      INTEGER NOT NULL,
    day     TEXT NOT NULL,
    score   INTEGER NOT NULL,
    summary TEXT NOT NULL,
    detail  TEXT NOT NULL,
    sent    INTEGER NOT NULL DEFAULT 0,
    status  TEXT,
    by_id   INTEGER,
    done_ts INTEGER
);
CREATE INDEX IF NOT EXISTS anomaly_alerts_chat ON anomaly_alerts(chat_id, ts);
""", migrate={"anomaly_alerts": "plain"})


def _now() -> float:
    return time.time()


# ── 방마다 메모리 상태 ────────────────────────────────────
@dataclass
class RoomState:
    joins: deque = field(default_factory=lambda: deque(maxlen=MAXQ))   # (ts, uid, 이름, username, 새 계정?)
    newbies: dict = field(default_factory=dict)                        # uid → 입장 시각 (NEWBIE_MINUTES 안)
    links: deque = field(default_factory=lambda: deque(maxlen=MAXQ))   # (ts, uid, msg_id, 링크 키) 훅이 본 것(숨은 링크 포함)
    msgs: deque = field(default_factory=lambda: deque(maxlen=MAXQ))    # (ts, uid, msg_id) 신규 멤버 메시지
    last_eval: float = 0.0
    last_seen: float = 0.0
    pending: bool = False
    baseline: tuple[float, float] | None = None                        # (계산 시각, 10분 평균 입장)
    db: object = None                                                  # 이 상태가 속한 DB (테스트는 방 ID 가 같고 DB 만 다름)


_rooms: dict[int, RoomState] = {}
_due: dict[int, tuple[object, float]] = {}   # 방 → (DB, 보안 강화 끝 시각 · 0 = 없음). 프로세스에서 처음 볼 때 DB 에서 한 번 읽음
_reverting: set[int] = set()
_tasks: set = set()


def room(svc, chat_id: int) -> RoomState:
    st = _rooms.get(chat_id)
    if st is None or st.db is not svc.db:
        if len(_rooms) >= MAX_ROOMS:  # 오래 조용한 방부터 잊음
            cut = _now() - WINDOW
            for k in [k for k, v in _rooms.items() if v.last_seen < cut and not v.pending]:
                del _rooms[k]
        st = _rooms[chat_id] = RoomState(db=svc.db)
    st.last_seen = _now()
    return st


def _prune(st: RoomState, now: float) -> None:
    cut = now - WINDOW
    for q in (st.joins, st.links, st.msgs):
        while q and q[0][0] < cut:
            q.popleft()
    ncut = now - NEWBIE_MINUTES * 60
    for uid in [u for u, t in st.newbies.items() if t < ncut]:
        del st.newbies[uid]


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# ── 정규화 ────────────────────────────────────────────────
def name_key(name: str) -> str:
    """'Crypto 💰 Bot 12' → 'cryptobot'. 글자(한글·영문 등)만 남기고 소문자. 2글자 미만이면 ''."""
    s = unicodedata.normalize("NFKC", name or "").lower()
    key = "".join(ch for ch in s if unicodedata.category(ch).startswith("L"))
    return key if len(key) >= 2 else ""


def username_stem(username: str | None) -> str:
    """'airdrop_bot_123' → 'airdrop_bot'. 4글자 미만이면 ''."""
    stem = re.sub(r"[\d_]+$", "", (username or "").lower())
    return stem if len(stem) >= 4 else ""


_LINK = re.compile(r"(?:https?://|tg://)[^\s<>\"']+|(?<![@\w.-])(?:www\.)?(?:[a-z0-9-]+\.)+(?:" + _TLD + r")\b(?:/[^\s<>\"']*)?",
                   re.I | re.A)
_TG_HOSTS = {"t.me", "telegram.me", "telegram.dog"}


def link_key(url: str) -> str | None:
    """링크 → 셀 키: 도메인 (www 뺌) / t.me 는 '초대링크·채널' 까지 (t.me/+코드, t.me/joinchat/코드, t.me/채널)."""
    u = url.strip()
    if u.lower().startswith("tg://"):
        return "tg://" + u[5:45]
    u = re.sub(r"^https?://", "", u, flags=re.I)
    host, _, path = u.partition("/")
    host = host.split("@")[-1].split(":")[0].lower().rstrip(".").removeprefix("www.")
    if not host or "." not in host:
        return None
    if host in _TG_HOSTS:
        segs = [p for p in re.split(r"[/?#]", path) if p]
        if not segs:
            return "t.me"
        if segs[0].lower() == "joinchat" and len(segs) > 1:
            return "t.me/joinchat/" + segs[1][:40]
        return "t.me/" + (segs[0][:40] if segs[0].startswith("+") else segs[0][:40].lower())
    return host


def extract_links(text: str, entities=()) -> list[str]:
    keys = [k for k in (link_key(m.group(0)) for m in _LINK.finditer(text or "")) if k]
    for ent in entities or ():
        if getattr(ent, "type", None) == "text_link" and getattr(ent, "url", None):
            k = link_key(ent.url)
            if k:
                keys.append(k)
    return list(dict.fromkeys(keys))   # 한 메시지 안 같은 링크는 1번


def _whitelisted(key: str, whitelist: list[str]) -> bool:
    host = key.split("/")[0]
    return any(host == w or host.endswith("." + w) for w in whitelist or ())


def defang(key: str) -> str:
    """눌리지 않는 표시용: 'evil.xyz' → 'evil[.]xyz' (텔레그램이 링크로 안 만듦). HTML 이스케이프 전 글자."""
    return key.replace("://", "[:]//").replace(".", "[.]")


# ── 훅 ────────────────────────────────────────────────────
async def on_member_join(svc, bot, chat_id: int, user) -> bool:
    """입장 기록 (관리자·자유 멤버·권한 없는 방은 handlers 가 이미 거름). 절대 막지 않는다(False)."""
    try:
        s = await svc.db.get_settings(chat_id)
        await maybe_revert(svc, bot, chat_id)
        if s.get("anomaly_mode", "notify") == "off" or getattr(user, "is_bot", False):
            return False
        now = _now()
        st = room(svc, chat_id)
        st.joins.append((now, user.id, user_name(user)[:64], getattr(user, "username", None),
                         accountage.is_recent(user.id)))
        st.newbies.pop(user.id, None)
        st.newbies[user.id] = now
        while len(st.newbies) > MAXQ:
            del st.newbies[next(iter(st.newbies))]
        await _schedule(svc, bot, chat_id, st)
    except Exception:  # 감지가 실패해도 입장 처리는 계속
        log.exception("anomaly join hook failed in %s", chat_id)
    return False


async def on_message(svc, bot, msg, role) -> None:
    try:
        chat_id, user = msg.chat_id, msg.from_user
        if chat_id >= 0 or not user or user.is_bot or getattr(msg, "sender_chat", None):
            return
        await maybe_revert(svc, bot, chat_id)   # 방마다 처음 한 번만 DB, 그 뒤로는 메모리
        if role >= Role.ADMIN:
            return
        s = await svc.db.get_settings(chat_id)
        if s.get("anomaly_mode", "notify") == "off":
            return
        now = _now()
        st = _rooms.get(chat_id)
        st = st if st is not None and st.db is svc.db else None
        entities = [*(getattr(msg, "entities", None) or ()), *(getattr(msg, "caption_entities", None) or ())]
        keys = [k for k in extract_links(msg.text or msg.caption or "", entities)
                if not _whitelisted(k, s.get("whitelist_domains", []))]
        newbie = st is not None and now - st.newbies.get(user.id, -1e18) < NEWBIE_MINUTES * 60
        if not keys and not newbie:
            return   # 대부분의 메시지: DB 조회 없이 끝
        if await free.is_free(svc.db, chat_id, user.id):
            return
        st = room(svc, chat_id)
        for k in keys:
            st.links.append((now, user.id, msg.message_id, k))
        if newbie:
            st.msgs.append((now, user.id, msg.message_id))
        await _schedule(svc, bot, chat_id, st)
    except Exception:  # 감지가 실패해도 메시지 처리는 계속
        log.exception("anomaly message hook failed")


def _hot(st: RoomState) -> bool:
    """다시 평가할 만한 방: 입장 3명↑ 또는 링크 2번↑ 또는 신규 메시지가 창 안에 남아 있음."""
    return len(st.joins) >= 3 or len(st.links) >= 2 or bool(st.msgs)


async def _schedule(svc, bot, chat_id: int, st: RoomState) -> None:
    """EVAL_GAP 안에 또 오면 지연 평가 1개만 예약 (몰린 뒤 조용해져도 마지막 상태를 평가).
    '뜨거운' 방(_hot)은 FOLLOW_UP 초마다 다시 평가 — 뒤이은 링크가 링크·반복 규칙에 지워지면 그룹 메시지 훅이 안 불려서
    (handlers 가 훅 전에 끝냄) 평가할 계기가 없기 때문. 창이 비면 멈춤 (방마다 10분에 최대 ~10번)."""
    now = _now()
    wait = st.last_eval + EVAL_GAP - now
    if wait <= 0:
        st.last_eval = now   # await 전에 표시 (동시에 온 훅이 같이 평가하지 않게)
        await evaluate(svc, bot, chat_id)
        wait = FOLLOW_UP if _hot(st) else None
    if wait is not None and not st.pending:
        st.pending = True
        _spawn(_later(svc, bot, chat_id, st, wait))


async def _later(svc, bot, chat_id: int, st: RoomState, wait: float) -> None:
    try:
        await asyncio.sleep(wait)
        if _rooms.get(chat_id) is not st:
            return   # 그 사이 잊힌 상태 (방이 오래 조용했거나 다른 DB)
        st.pending = False
        st.last_seen = _now()
        await _schedule(svc, bot, chat_id, st)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("anomaly delayed eval failed in %s", chat_id)


# ── 평가 ──────────────────────────────────────────────────
@dataclass
class Finding:
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    summary: str = ""
    detail: dict = field(default_factory=dict)
    raid: bool = False


async def _baseline(svc, chat_id: int, st: RoomState, now: float) -> float:
    if st.baseline and now - st.baseline[0] < BASELINE_TTL:
        return st.baseline[1]
    since = int(now - BASELINE_DAYS * 86400)
    n = await svc.db.joined_count(chat_id, since, int(now - WINDOW))
    avg = n / (BASELINE_DAYS * 86400 / WINDOW)
    st.baseline = (now, avg)
    return avg


async def _excluded(svc, bot, chat_id: int, uids: set[int]) -> set[int]:
    """관리자·봇관리자·오너·자유 멤버 (DB 에서 읽은 메시지용)."""
    out = set()
    for uid in list(uids)[:200]:
        try:
            if await svc.perms.is_admin(bot, chat_id, uid) or await free.is_free(svc.db, chat_id, uid):
                out.add(uid)
        except TelegramError:
            pass
    return out


async def collect(svc, bot, chat_id: int, now: float | None = None) -> Finding:
    """지금 창의 신호를 모아 점수·이유·상세를 만든다 (알림은 안 보냄)."""
    now = _now() if now is None else now
    st = room(svc, chat_id)
    _prune(st, now)
    s = await svc.db.get_settings(chat_id)
    lv = LEVELS.get(s.get("anomaly_level"), LEVELS["normal"])
    f = Finding()
    f.raid = await raid.active(svc, chat_id)
    joins = list(st.joins)
    n = len(joins)

    # ③⑤ 메시지 신호: 훅이 본 것 + messages 표 (관리 검사에서 지워진 링크 시도 포함). 뭔가 있을 때만 DB 를 읽음
    link_hits: dict[tuple[int, int], set[str]] = {}
    for ts, uid, mid, k in st.links:
        link_hits.setdefault((uid, mid), set()).add(k)
    newbie_msgs: set[tuple[int, int]] = {(uid, mid) for _, uid, mid in st.msgs}
    if n >= 3 or st.links or st.msgs:
        rows = await svc.db._all("SELECT user_id, msg_id, text, ts FROM messages WHERE chat_id=? AND ts>=? AND is_bot=0 "
                                 "ORDER BY id DESC LIMIT ?", (chat_id, int(now - WINDOW), MAXQ))
        wl = s.get("whitelist_domains", [])
        cand = []
        for r in rows:
            keys = [k for k in extract_links(r["text"]) if not _whitelisted(k, wl)]
            joined = st.newbies.get(r["user_id"])
            is_new = joined is not None and r["ts"] - joined < NEWBIE_MINUTES * 60 and r["ts"] >= joined - 5
            if keys or is_new:
                cand.append((r, keys, is_new))
        skip = await _excluded(svc, bot, chat_id, {r["user_id"] for r, _, _ in cand}) if cand else set()
        for r, keys, is_new in cand:
            if r["user_id"] in skip:
                continue
            ident = (r["user_id"], r["msg_id"] if r["msg_id"] is not None else -r["ts"])
            if keys:
                link_hits.setdefault(ident, set()).update(keys)
            if is_new:
                newbie_msgs.add(ident)

    # ① 입장 몰림
    base = await _baseline(svc, chat_id, st, now) if n >= lv.joins else 0.0
    surge = n >= lv.joins and n >= lv.factor * max(base, 1.0)
    # ② 비슷한 이름
    by_name: dict[str, set[int]] = {}
    by_user: dict[str, set[int]] = {}
    for _, uid, name, username, _ in joins:
        if (k := name_key(name)):
            by_name.setdefault(k, set()).add(uid)
        if (k := username_stem(username)):
            by_user.setdefault(k, set()).add(uid)
    clusters = sorted([("이름", k, v) for k, v in by_name.items()] + [("@아이디", k, v) for k, v in by_user.items()],
                      key=lambda c: -len(c[2]))
    top = clusters[0] if clusters and len(clusters[0][2]) >= 2 else None
    same_name = len(top[2]) if top else 0
    names_hit = same_name >= lv.names
    # ④ 새 계정 비율
    recent = sum(1 for j in joins if j[4])
    pct = round(recent * 100 / n) if n else 0
    recent_hit = n >= RECENT_MIN_JOINS and recent / n >= lv.recent
    # ③ 같은 링크
    per_key: dict[str, list[int]] = {}
    for (uid, _), keys in link_hits.items():
        for k in keys:
            per_key.setdefault(k, []).append(uid)
    links = sorted(((k, len(v), len(set(v))) for k, v in per_key.items()), key=lambda x: (-x[1], x[0]))
    best = next((x for x in links if x[2] >= 2), None)
    link_n = best[1] if best else 0
    links_hit = link_n >= lv.links
    # ⑤ 신규 멤버 도배
    flood_n, flood_users = len(newbie_msgs), len({u for u, _ in newbie_msgs})
    flood_hit = flood_n >= lv.flood and flood_users >= 3

    joins_count = not f.raid   # 대량 입장 방어 중이면 입장 신호는 raid 가 이미 처리·알림
    if surge:
        pts = (PTS_SURGE + (PTS_SURGE_BIG if n >= 2 * lv.joins else 0)) if joins_count else 0
        f.score += pts
        f.reasons.append(f"입장 몰림: 10분에 {n}명 (평소 10분 평균 {base:.1f}명)")
    if names_hit:
        f.score += PTS_NAMES if joins_count else 0
        f.reasons.append(f"비슷한 {top[0]}: {same_name}명")
    if recent_hit:
        f.score += PTS_RECENT if joins_count else 0
        f.reasons.append(f"새 계정 비율: 들어온 {n}명 중 {pct}%")
    if links_hit:
        f.score += PTS_LINKS + (PTS_LINKS_BIG if link_n >= 2 * lv.links else 0)
        f.reasons.append(f"같은 링크 반복: {defang(best[0])} {link_n}회 · {best[2]}명")
    if flood_hit:
        f.score += PTS_FLOOD + (PTS_FLOOD_BIG if flood_n >= 2 * lv.flood else 0)
        f.reasons.append(f"신규 멤버 메시지 몰림: {flood_users}명이 {flood_n}개")

    parts = [f"입장 +{n}"]
    if same_name >= 2:
        parts.append(f"비슷한 이름 {same_name}명")
    if link_n >= 2:
        parts.append(f"같은 링크 {link_n}회")
    if n:
        parts.append(f"새 계정 {pct}%")
    if flood_n:
        parts.append(f"신규 메시지 {flood_n}개")
    f.summary = " · ".join(parts)

    cluster_ids = top[2] if top else set()
    ordered = sorted(joins, key=lambda j: (j[1] not in cluster_ids, not j[4], -j[0]))
    users = [[uid, name, username, bool(rec), uid in cluster_ids] for _, uid, name, username, rec in ordered[:DETAIL_USERS]]
    buckets: dict[int, list[int]] = {}
    base_ts = int(now - WINDOW)
    for ts, *_ in joins:
        buckets.setdefault(int(ts - base_ts) // 60, [0, 0, 0])[0] += 1
    for ts, _, _, _ in st.links:
        buckets.setdefault(int(ts - base_ts) // 60, [0, 0, 0])[1] += 1
    for ts, _, _ in st.msgs:
        buckets.setdefault(int(ts - base_ts) // 60, [0, 0, 0])[2] += 1
    f.detail = {"joins": n, "base": round(base, 2), "same_name": same_name, "cluster": top[1] if top else "",
                "recent_pct": pct, "links": [list(x) for x in links[:5]], "flood": [flood_n, flood_users],
                "raid": f.raid, "users": users, "more": max(0, n - len(users)),
                "timeline": [[base_ts + m * 60, *c] for m, c in sorted(buckets.items()) if 0 <= m < WINDOW // 60 + 1],
                "reasons": f.reasons}
    return f


async def evaluate(svc, bot, chat_id: int) -> int | None:
    """점수가 넘으면 알림을 보내고 알림 ID 를 돌려준다."""
    s = await svc.db.get_settings(chat_id)
    if s.get("anomaly_mode", "notify") == "off":
        return None
    f = await collect(svc, bot, chat_id)
    if f.score < SCORE_MIN:
        return None
    return await alert(svc, bot, chat_id, f)


# ── 알림 ──────────────────────────────────────────────────
def _day(svc, ts: float) -> str:
    return datetime.fromtimestamp(ts, svc.cfg.tz).strftime("%Y-%m-%d")


async def claim(svc, chat_id: int, f: Finding, now: float) -> int | None:
    """쿨다운·하루 상한 확인과 기록을 한 번에 (동시에 평가돼도 알림 1번). 못 차지하면 None."""
    day = _day(svc, now)
    detail = json.dumps(f.detail, ensure_ascii=False)

    def run(c):
        if c.execute("SELECT 1 FROM anomaly_alerts WHERE chat_id=? AND ts>?", (chat_id, int(now - COOLDOWN))).fetchone():
            return None
        if c.execute("SELECT COUNT(*) FROM anomaly_alerts WHERE chat_id=? AND day=?", (chat_id, day)).fetchone()[0] \
                >= DAILY_CAP:
            return None
        return c.execute("INSERT INTO anomaly_alerts(chat_id, ts, day, score, summary, detail) VALUES(?,?,?,?,?,?)",
                         (chat_id, int(now), day, f.score, f.summary, detail)).lastrowid
    return await svc.db.atomic(run)


def alert_text(title: str, f: Finding) -> str:
    lines = [f"🚨 <b>방 이상징후</b> · <b>{esc(title)}</b>",
             f"최근 {WINDOW // 60}분: {esc(f.summary)}"]
    lines += [f"• {esc(r)}" for r in f.reasons]
    if f.raid:
        lines.append("🚨 대량 입장 방어가 이미 켜져 있어요 — 입장 몰림은 방어 모드가 막는 중이에요.")
    lines.append("위험 신호가 감지됐어요. 자동 제재는 하지 않았어요.")
    return "\n".join(lines)


def alert_kb(chat_id: int, alert_id: int) -> InlineKeyboardMarkup:
    cb = f"m:anmx:{chat_id}:{alert_id}:"
    b = InlineKeyboardButton
    return InlineKeyboardMarkup([[b("🔍 상세 보기", callback_data=cb + "d")],
                                 [b("🛡️ 보안 강화", callback_data=cb + "h"), b("🙈 무시", callback_data=cb + "i")]])


async def recipients(svc, bot, chat_id: int) -> list:
    """'사용자 차단' 권한이 있는 텔레그램 관리자 (봇 제외)."""
    out = []
    for a in await svc.perms.admin_users(bot, chat_id):
        if not getattr(a, "is_bot", False) and await may(svc.perms, bot, chat_id, a.id, "restrict"):
            out.append(a)
    return out


async def alert(svc, bot, chat_id: int, f: Finding) -> int | None:
    aid = await claim(svc, chat_id, f, _now())
    if aid is None:
        return None
    await svc.db.log_mod(chat_id, None, None, "anomaly", f"{f.summary} (점수 {f.score})")
    from .subscription import chat_title  # 늦게 import (순환 방지)
    body, kb = alert_text(await chat_title(svc, chat_id), f), alert_kb(chat_id, aid)
    # 쿨다운·하루 상한은 위 claim 그대로, 보내기는 사건 묶기로 (10분 안이면 같은 1:1 메시지를 고침 — sodam/incidents.py).
    # 1:1 막힌 관리자(Forbidden)는 건너뜀
    res = await incidents.open_or_bump(svc, bot, chat_id, "anomaly", "room", body, kb,
                                       await recipients(svc, bot, chat_id))
    await svc.db._write("UPDATE anomaly_alerts SET sent=? WHERE id=?", (res.delivered, aid))
    return aid


async def get_alert(db, chat_id: int, aid: int):
    return await db._one("SELECT * FROM anomaly_alerts WHERE id=? AND chat_id=?", (aid, chat_id))


async def recent_alerts(db, chat_id: int, limit: int = 5) -> list:
    return await db._all("SELECT * FROM anomaly_alerts WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit))


async def mark(db, aid: int, status: str, by_id: int) -> bool:
    """확인 전 알림만 처리 표시 (한 문장 → 두 관리자가 동시에 눌러도 한 번)."""
    cur = await db.conn.execute("UPDATE anomaly_alerts SET status=?, by_id=?, done_ts=? WHERE id=? AND status IS NULL",
                                (status, by_id, dbmod.now(), aid))
    await db.conn.commit()
    return cur.rowcount > 0


# ── 보안 강화 · 자동 되돌리기 ─────────────────────────────
def planned_changes(s: dict) -> dict:
    """보안 강화 때 바꿀 설정 (지금보다 약한 것만)."""
    out = {}
    if int(s.get("newbie_link_hours", 0) or 0) < NEWBIE_LINK_HOURS:
        out["newbie_link_hours"] = NEWBIE_LINK_HOURS
    if s.get("forward_filter", "off") == "off":
        out["forward_filter"] = "newbie"
    return out


async def harden(svc, bot, chat_id: int, actor_id: int, hours: int) -> int:
    """보안 강화 켜기 (이미 켜져 있으면 더 늦은 시각으로 연장). 끝나는 시각을 돌려준다."""
    now = int(_now())
    end = now + hours * 3600
    s = await svc.db.get_settings(chat_id)
    st = await svc.db.get_state(chat_id, HARDEN_KEY) or {"since": now, "until": 0, "set": {}, "restore": {}, "raid": False}
    for k, v in planned_changes(s).items():
        st["restore"].setdefault(k, s[k])
        st["set"][k] = v
        await svc.db.set_setting(chat_id, k, v)
    st["until"] = max(st["until"], end)
    st["by"] = actor_id
    rs = await svc.db.get_state(chat_id, raid.STATE)
    if rs and rs.get("until", 0) > now:   # 이미 방어 중: 끝나는 시각만 늦춤
        if rs["until"] < st["until"]:
            rs["until"] = st["until"]
            await svc.db.set_state(chat_id, raid.STATE, rs)
    else:
        await raid.start(svc, bot, chat_id, (st["until"] - now + 59) // 60, actor_id=actor_id)
        st["raid"] = True
    await svc.db.set_state(chat_id, HARDEN_KEY, st)
    _due[chat_id] = (svc.db, st["until"])
    await svc.db.log_mod(chat_id, actor_id, None, "anomaly_harden",
                         f"{hours}시간 · 대량 입장 방어 + " + (", ".join(f"{k}={v}" for k, v in st["set"].items()) or "설정 그대로"))
    _spawn(_revert_later(svc, bot, chat_id, st["until"]))
    return st["until"]


async def _revert_later(svc, bot, chat_id: int, until: float) -> None:
    try:
        await asyncio.sleep(max(0.0, until - _now()) + 1)
        await maybe_revert(svc, bot, chat_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("anomaly revert timer failed in %s", chat_id)


async def revert(svc, bot, chat_id: int, actor_id: int | None = None) -> bool:
    """보안 강화 끝: 우리가 바꾼 값 그대로인 설정만 원래대로 (관리자가 그 사이 바꾼 건 둠)."""
    if chat_id in _reverting:
        return False
    _reverting.add(chat_id)
    try:
        st = await svc.db.get_state(chat_id, HARDEN_KEY)
        _due[chat_id] = (svc.db, 0)
        if not st:
            return False
        s = await svc.db.get_settings(chat_id)
        back = []
        for k, v in st.get("set", {}).items():
            if s.get(k) == v and k in st.get("restore", {}):
                await svc.db.set_setting(chat_id, k, st["restore"][k])
                back.append(f"{k}={st['restore'][k]}")
        await svc.db.set_state(chat_id, HARDEN_KEY, None)
        if actor_id is not None and st.get("raid") and await raid.active(svc, chat_id):
            await raid.stop(svc, bot, chat_id, actor_id=actor_id)
        await svc.db.log_mod(chat_id, actor_id, None, "anomaly_harden_off",
                             ("관리자가 끔" if actor_id else "시간 끝남") + (" · " + ", ".join(back) if back else ""))
        return True
    finally:
        _reverting.discard(chat_id)


async def hardened_until(svc, chat_id: int) -> int:
    st = await svc.db.get_state(chat_id, HARDEN_KEY)
    return int(st["until"]) if st and st.get("until", 0) > _now() else 0


async def maybe_revert(svc, bot, chat_id: int) -> None:
    """끝난 보안 강화가 있으면 되돌림. 방마다 프로세스에서 처음 한 번만 DB 를 읽고 그 뒤로는 메모리(_due)로."""
    hit = _due.get(chat_id)
    if hit is None or hit[0] is not svc.db:
        st = await svc.db.get_state(chat_id, HARDEN_KEY)
        hit = _due[chat_id] = (svc.db, st["until"] if st else 0)
    due = hit[1]
    if due and due <= _now():
        await revert(svc, bot, chat_id)


async def tick(svc, bot) -> None:
    """주기 작업용 (handlers.job_tick 에 한 줄 넣으면 조용한 방도 제시간에 되돌림)."""
    for row in await svc.db._all("SELECT chat_id FROM chat_state WHERE key=?", (HARDEN_KEY,)):
        try:
            _due.pop(row["chat_id"], None)
            await maybe_revert(svc, bot, row["chat_id"])
        except Exception:
            log.exception("anomaly tick failed in %s", row["chat_id"])


hooks.add_member_join_hook(on_member_join)
hooks.add_group_message_hook(on_message)
