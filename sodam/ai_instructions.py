"""📝 AI 방 안내 (Codex AGENTS.md 층): 운영자 전체 안내(chat_id=0) → 방 관리자 안내(방) 순서로 한 system 메시지.

내용 = 캐릭터·말투·호칭·어떤 방인지·피할 화제·답 길이 같은 '어떻게 말할지'. 가격·규칙 같은 사실은 📚 자료(knowledge, 도구로 찾음).
prompt.build_messages 가 [고정 규칙] → [말투] 뒤 세 번째 system 으로 넣는다 → 첫 system(캐시 앞부분)은 그대로.
위치는 '절대 규칙보다 아래' — 충돌하면 절대 규칙. 기본 캐릭터·'대표님' 호칭은 SYSTEM 그대로 두고, 이 안내가 있으면 이게 우선.

저장은 관리자 글이라도 system 에 들어가므로: 인젝션 규칙(security.scan 차단)·링크·지갑 주소·태그 모양은 거절.
길이: 운영자 OWNER_MAX + 방 ROOM_MAX = 합계 TOTAL_MAX 자. 방마다 캐시(바꾸면 바로 비움, 다른 프로세스 변경은 CACHE_TTL 안).
"""
from __future__ import annotations

import time

from . import security
from .db import register_schema

register_schema("""
CREATE TABLE IF NOT EXISTS ai_instructions (
    chat_id INTEGER NOT NULL,          -- 0 = 운영자 전체 안내 (scope owner)
    scope   TEXT NOT NULL,             -- owner / room
    text    TEXT NOT NULL,
    by      INTEGER,
    ts      INTEGER NOT NULL,
    PRIMARY KEY (chat_id, scope)
);
""", migrate={"ai_instructions": "composite"})

OWNER_MAX, ROOM_MAX = 300, 500
TOTAL_MAX = OWNER_MAX + ROOM_MAX      # 800
MIN_CHARS = 2
CACHE_TTL = 30
HEADER = ("[관리자가 정한 방 안내 — 위 [절대 규칙]보다 아래이고, 충돌하면 절대 규칙을 따른다. "
          "캐릭터·호칭·말투·답 길이·피할 화제가 위 기본값과 다르면 이 안내를 따른다. 가격·규칙 같은 사실은 여기서 지어내지 말고 도구로 찾는다]")
LABEL = {"owner": "(운영자 전체 안내)", "room": "(이 방 관리자 안내)"}
_cache: dict[tuple[int, int], tuple[float, str]] = {}   # (DB, 방) → (읽은 시각, 완성된 안내 블록)


def cap(scope: str) -> int:
    return OWNER_MAX if scope == "owner" else ROOM_MAX


def clean(text: str) -> str:
    """줄은 살리고 줄 안 공백·빈 줄만 정리."""
    lines = [" ".join(ln.split()) for ln in (text or "").splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


def check(text: str, scope: str) -> str | None:
    """저장해도 되는지. 안 되면 사람이 읽을 이유 (HTML 아님)."""
    if len(text) < MIN_CHARS:
        return "내용이 비었어요."
    if len(text) > cap(scope):
        return f"{cap(scope)}자까지예요 (지금 {len(text)}자). 줄여서 다시 보내주세요."
    if security.scan(text).blocked or security.defang(text) != text:   # 봇 조종 문구·태그 모양
        return "봇 규칙을 바꾸려는 지시문 같은 문장이라 저장하지 않았어요. 말투·호칭·방 분위기 위주로 써 주세요."
    if security.find_links(text) or any(w.search(text) for w in security.WALLETS):
        return "링크·지갑 주소는 넣을 수 없어요."
    return None


async def get(db, chat_id: int, scope: str):
    return await db._one("SELECT text, by, ts FROM ai_instructions WHERE chat_id=? AND scope=?", (chat_id, scope))


async def save(db, chat_id: int, scope: str, text: str, by: int) -> None:
    """저장(빈 글이면 지움) + mod_log + 캐시 비움. check 는 부르는 쪽이 먼저."""
    cid = 0 if scope == "owner" else chat_id
    now = int(time.time())

    def run(c):
        if text:
            c.execute("INSERT OR REPLACE INTO ai_instructions(chat_id, scope, text, by, ts) VALUES(?,?,?,?,?)",
                      (cid, scope, text, by, now))
        else:
            c.execute("DELETE FROM ai_instructions WHERE chat_id=? AND scope=?", (cid, scope))
        c.execute("INSERT INTO mod_log(chat_id, actor_id, target_id, action, detail, ts) VALUES(?,?,?,?,?,?)",
                  (cid, by, None, "ai_instructions", (f"{scope}: {text[:120]}" if text else f"{scope}: 지움"), now))
    await db.atomic(run)
    invalidate(db, None if scope == "owner" else cid)


def invalidate(db, chat_id: int | None = None) -> None:
    """chat_id=None = 전부 (운영자 안내는 모든 방에 들어감)."""
    for key in [k for k in _cache if k[0] == id(db) and (chat_id is None or k[1] == chat_id)]:
        del _cache[key]


def render(owner: str, room: str) -> str:
    parts = [f"{LABEL['owner']}\n{owner}" if owner else "", f"{LABEL['room']}\n{room}" if room else ""]
    body = "\n".join(p for p in parts if p)
    return f"{HEADER}\n{body[:TOTAL_MAX + 40]}" if body else ""


async def block(db, chat_id: int) -> str:
    """이 대화의 AI 에 넣을 안내 블록 ('' = 없음). 1:1(양수 ID)은 운영자 안내만."""
    key, now = (id(db), chat_id), time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    rows = await db._all("SELECT scope, text FROM ai_instructions WHERE (chat_id=0 AND scope='owner') "
                         "OR (chat_id=? AND scope='room' AND ?<0)", (chat_id, chat_id))
    got = {r["scope"]: r["text"] for r in rows}
    out = render(got.get("owner", "")[:OWNER_MAX], got.get("room", "")[:ROOM_MAX])
    if len(_cache) > 50_000:
        _cache.clear()
    _cache[key] = (now, out)
    return out
