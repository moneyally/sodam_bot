"""🕸️ 사기 무리 탐지: 같은 지갑·같은 초대링크·같은 긴 글을 쓴 계정들을 한 무리로 묶는다 (AI 비용 0).

참고: 대규모 사기 무리 탐지(arXiv 2512.19061) — 강한 연결(같은 카드·전화)로 묶고 약한 연결(같은 기기·IP)로 잇기.
우리 규모(수천 명)엔 임베딩·HDBSCAN 없이 union-find 한 번이면 충분.
- 재료: messages(90일 보관, 관리 검사 전에 기록 → 링크 필터가 지운 글도 있음)의 최근 DAYS 일 사람 글.
  강한 연결 = 지갑 주소 · 비공개 초대링크(t.me/+… · joinchat) / 약한 연결 = 같은 긴 글(MIN_TEXT자↑, 공백·대소문자 무시).
  한 값을 너무 많은 사람이 쓰면(COMMON 명↑) 공지·유명 링크로 보고 안 씀. 봇·어느 방이든 관리자·오너는 빼고 셈.
- 무리 = 2명↑. '강함' = 강한 연결이 하나라도 있음. 알림은 강함이거나 증거 2가지↑일 때만 (같은 글 1개로는 안 함).
- 밴(mod_log 'ban') 이 생기면 그 사람 무리 중 아직 방에 있는 사람을 방마다 '사용자 차단' 권한 관리자에게 알림(incidents).
  **자동 제재 없음** — 알림의 [🚫 이 방에서 N명 밴] 은 누를 때 권한을 다시 확인 (panels/scamring.py).
- 다른 방 이름은 보여 주지 않음 (방 관리자에겐 '다른 방 N곳' 숫자만).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .security import WALLETS

DAYS = 30
MIN_TEXT = 40
COMMON = 30          # 이보다 많은 사람이 쓴 값은 공지·유명 링크로 보고 연결에 안 씀
CACHE_SEC = 300
STRONG = ("wal", "inv")
KIND_LABEL = {"wal": "같은 지갑 주소", "inv": "같은 초대링크", "text": "똑같은 글"}
_INVITE = re.compile(r"(?:t|telegram)\.me/(?:\+|joinchat/)([A-Za-z0-9_-]{8,})", re.I)
_SPACE = re.compile(r"\s+")


@dataclass
class Ring:
    root: int
    members: set[int]
    evidence: list[tuple[str, str, frozenset[int]]] = field(default_factory=list)   # (종류, 가린 값, 쓴 사람들)
    flagged: set[int] = field(default_factory=set)                                  # 밴·공동 차단 기록 있는 사람

    @property
    def strong(self) -> bool:
        return any(k in STRONG for k, _, _ in self.evidence)

    @property
    def alertable(self) -> bool:
        return self.strong or len(self.evidence) >= 2

    def kinds(self) -> str:
        seen = dict.fromkeys(KIND_LABEL[k] for k, _, _ in self.evidence)
        return " · ".join(seen)


def keys_of(text: str) -> list[tuple[str, str]]:
    """글 하나에서 연결 값들."""
    out = [("wal", m.group(0)) for w in WALLETS for m in w.finditer(text)]
    out += [("inv", m.group(1)) for m in _INVITE.finditer(text)]
    norm = _SPACE.sub(" ", text).strip().lower()
    if len(norm) >= MIN_TEXT:
        out.append(("text", norm))
    return out


def mask(kind: str, val: str) -> str:
    if kind == "wal":
        return f"{val[:6]}…{val[-4:]}"
    if kind == "inv":
        return f"t.me/+{val[:3]}…"
    return f"'{val[:24]}…'"


def build(rows, exclude: set[int], flagged: set[int]) -> dict[int, Ring]:
    """rows = (user_id, text). 사람 → 그 사람이 속한 무리 (2명↑만)."""
    users: dict[tuple[str, str], set[int]] = {}
    for uid, text in rows:
        if uid in exclude:
            continue
        for k in keys_of(text or ""):
            users.setdefault(k, set()).add(uid)
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    links = [(k, us) for k, us in users.items() if 2 <= len(us) <= COMMON]
    for _, us in links:
        first, *rest = sorted(us)
        for u in rest:
            a, b = find(first), find(u)
            if a != b:
                parent[max(a, b)] = min(a, b)
    rings: dict[int, Ring] = {}
    for u in parent:
        r = find(u)
        rings.setdefault(r, Ring(r, set())).members.add(u)
    for (kind, val), us in links:
        ring = rings[find(next(iter(us)))]
        ring.evidence.append((kind, mask(kind, val), frozenset(us)))
    out = {}
    for ring in rings.values():
        ring.evidence.sort(key=lambda e: (e[0] not in STRONG, e[0]))
        ring.flagged = ring.members & flagged
        for u in ring.members:
            out[u] = ring
    return out


_CACHE: dict[int, tuple[float, dict[int, Ring]]] = {}


def invalidate(db) -> None:
    _CACHE.pop(id(db), None)


async def graph(db, owners: set[int] = frozenset(), *, fresh: bool = False) -> dict[int, Ring]:
    hit = _CACHE.get(id(db))
    if hit and not fresh and time.time() - hit[0] < CACHE_SEC:
        return hit[1]
    since = int(time.time()) - DAYS * 86400
    rows = await db._all("SELECT user_id, text FROM messages WHERE is_bot=0 AND ts>=? AND length(text)>=20", (since,))
    admins = {r["user_id"] for r in await db._all("SELECT DISTINCT user_id FROM chat_admins")}
    flagged = {r["target_id"] for r in await db._all(
        "SELECT DISTINCT target_id FROM mod_log WHERE action='ban' AND target_id IS NOT NULL AND ts>=?", (since,))}
    flagged |= {r["user_id"] for r in await db._all("SELECT user_id FROM fedban_entries")}
    g = build([(r["user_id"], r["text"]) for r in rows], admins | set(owners), flagged)
    _CACHE[id(db)] = (time.time(), g)
    return g


async def present(db, chat_id: int, uids) -> list[int]:
    """지금 이 방에 있을 사람 (나간 기록·밴 기록이 마지막 모습보다 늦으면 뺌)."""
    out = []
    for uid in sorted(uids):
        row = await db._one(
            "SELECT m.user_id FROM members m WHERE m.chat_id=? AND m.user_id=? "
            "AND NOT EXISTS (SELECT 1 FROM member_left l WHERE l.chat_id=m.chat_id AND l.user_id=m.user_id "
            "                AND l.ts >= COALESCE(m.last_seen, m.joined_at, 0)) "
            "AND NOT EXISTS (SELECT 1 FROM mod_log b WHERE b.chat_id=m.chat_id AND b.target_id=m.user_id AND b.action='ban' "
            "                AND b.ts >= COALESCE(m.last_seen, m.joined_at, 0))", (chat_id, uid))
        if row:
            out.append(uid)
    return out


async def rooms_of(db, uids) -> dict[int, list[int]]:
    """무리 사람들이 있는 방 → 그 방에 있는 사람들."""
    chats = {r["chat_id"] for u in uids for r in await db._all(
        "SELECT chat_id FROM members WHERE user_id=? AND chat_id<0", (u,))}
    out = {}
    for cid in sorted(chats):
        here = await present(db, cid, uids)
        if here:
            out[cid] = here
    return out


async def rings_in(db, chat_id: int, owners: set[int] = frozenset()) -> list[Ring]:
    """이 방 사람이 끼어 있는 무리들 (강한 것·큰 것 먼저)."""
    g = await graph(db, owners)
    seen, out = set(), []
    for ring in g.values():
        if ring.root in seen:
            continue
        seen.add(ring.root)
        if await present(db, chat_id, ring.members):
            out.append(ring)
    return sorted(out, key=lambda r: (not r.strong, -len(r.members)))


# ── 밴 → 무리 알림 (hooks.add_tick_hook) ─────────────────
CURSOR = "scamring_ban_cursor"


async def new_bans(db) -> list:
    """지난번 이후 새 밴 기록. 처음엔 지금까지를 건너뜀 (옛 밴으로 알림 폭탄 X).
    읽기+커서 옮기기를 한 트랜잭션으로 → 메인·딜러 두 프로세스가 같은 밴을 두 번 알리지 않음."""
    def work(c):
        top = c.execute("SELECT COALESCE(MAX(id), 0) FROM mod_log").fetchone()[0]
        cur = c.execute("SELECT value FROM chat_state WHERE chat_id=0 AND key=?", (CURSOR,)).fetchone()
        c.execute("INSERT INTO chat_state(chat_id, key, value) VALUES(0, ?, ?) "
                  "ON CONFLICT(chat_id, key) DO UPDATE SET value=excluded.value", (CURSOR, str(top)))
        if cur is None:
            return []
        return c.execute("SELECT id, chat_id, target_id FROM mod_log WHERE id>? AND id<=? AND action='ban' "
                         "AND target_id IS NOT NULL ORDER BY id", (int(cur[0]), top)).fetchall()
    return [{"id": r[0], "chat_id": r[1], "target_id": r[2]} for r in await db.atomic(work)]
