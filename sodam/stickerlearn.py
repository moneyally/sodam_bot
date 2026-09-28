"""🧠 스티커·움프 실시간 학습 — 코드는 안 바뀌고 데이터만 는다.

- sticker_log: 만든 것마다 한 줄 (방·사람·요청 200자·원본 종류·sanitize 된 spec·결과·보낸 메시지 id·점수).
- 반응 신호로 점수: 👍❤️🔥 반응이나 '좋다·예쁘다·완벽' 답장 = +1, '별로·다른 느낌' 답장이나 10분 안 같은 사람의 재요청 = -1.
  이미 오는 반응·답장 업데이트만 본다 (AI 호출 없음).
- sticker_recipes: 같은 조합이 +1 을 2번 넘게 받으면 이름을 붙여 저장 (방마다 50개, 90일 안 쓰면 삭제).
  sticker_catalog 는 이 방·이 사람이 좋아한 레시피를 먼저, 별로였던 건 뒤로, 최근 3번과 다른 계열을 섞는다.
- 목록에 없는 효과(눈에서 레이저 등)는 가장 가까운 조합으로 만들고 feature_request 로 접수 → 새 부품은 코드로만 는다.
"""
from __future__ import annotations

import json
import re
import time

from .db import register_schema
from .stickerforge import recipes

register_schema("""
CREATE TABLE IF NOT EXISTS sticker_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    chat_id  INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    product  TEXT NOT NULL DEFAULT 'sticker',   -- sticker | ump
    request  TEXT NOT NULL DEFAULT '',
    kind     TEXT NOT NULL DEFAULT '',          -- cutout | photo | glow ...
    spec     TEXT NOT NULL,                     -- sanitize 된 spec JSON
    family   TEXT NOT NULL DEFAULT '',
    outcome  TEXT NOT NULL DEFAULT 'ok',        -- ok | retry | fail
    msg_id   INTEGER NOT NULL DEFAULT 0,        -- 방에 보낸 스티커/파일 메시지
    score    INTEGER NOT NULL DEFAULT 0         -- +1 좋음 · -1 별로 · 0 모름
);
CREATE INDEX IF NOT EXISTS sticker_log_chat ON sticker_log(chat_id, ts);
CREATE INDEX IF NOT EXISTS sticker_log_msg ON sticker_log(chat_id, msg_id);
CREATE TABLE IF NOT EXISTS sticker_recipes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id   INTEGER NOT NULL,
    name      TEXT NOT NULL,
    product   TEXT NOT NULL DEFAULT 'sticker',
    family    TEXT NOT NULL DEFAULT '',
    signature TEXT NOT NULL,                    -- 움직임+효과 이름 열 (같은 조합 판별)
    spec      TEXT NOT NULL,
    good      INTEGER NOT NULL DEFAULT 0,
    bad       INTEGER NOT NULL DEFAULT 0,
    uses      INTEGER NOT NULL DEFAULT 0,
    last_used INTEGER NOT NULL,
    UNIQUE(chat_id, product, signature)
);
""", migrate={"sticker_log": "plain", "sticker_recipes": "plain"})

REQUEST_CHARS = 200
GOOD_EMOJI = {"👍", "❤", "❤️", "🔥", "😍", "🥰", "👏", "💯", "❤‍🔥", "🎉"}
GOOD_WORDS = re.compile(r"좋[다아네]|예쁘|이쁘|완벽|최고|굿|멋지|귀엽|대박|찢었|ㅋㅋㅋ|👍|❤|🔥", re.I)
BAD_WORDS = re.compile(r"별로|별론|다른\s*느낌|다르게|다시\s*(해|만들)|구려|이상해|아쉽|맘에\s*안|마음에\s*안|노잼", re.I)
PROMOTE_AT = 2            # +1 이 이 수를 넘으면 레시피로
REDO_WINDOW = 600         # 초: 이 안에 같은 사람이 또 부탁하면 앞 것은 별로
MAX_PER_CHAT = 50
EXPIRE_DAYS = 90
RECENT_FAMILIES = 3


def signature(spec: dict) -> str:
    return "+".join([m["type"] for m in spec.get("motion", [])] + [f["type"] for f in spec.get("fx", [])])


def auto_name(request: str, spec: dict) -> str:
    """요청 글의 첫 두 단어 + 계열 (예: '출근완료_강렬_impact')."""
    words = [w for w in re.findall(r"[가-힣A-Za-z0-9]+", request or "") if len(w) >= 2][:2]
    return "_".join(words + [recipes.family(spec)]) if words else f"{recipes.family(spec)}_{signature(spec)[:24]}"


# ── 기록 ────────────────────────────────────────────────────
async def log(db, *, chat_id: int, user_id: int, request: str, kind: str, spec: dict, outcome: str, msg_id: int = 0,
              product: str = "sticker", now: int | None = None) -> int:
    """만든 것 한 줄. 같은 사람이 REDO_WINDOW 안에 또 만들면 앞 것을 -1 (다시 부탁 = 별로)."""
    now = now or int(time.time())
    req = " ".join((request or "").split())[:REQUEST_CHARS]

    def run(c):
        prev = c.execute("SELECT id FROM sticker_log WHERE chat_id=? AND user_id=? AND product=? AND ts>? AND outcome='ok' "
                         "ORDER BY id DESC LIMIT 1", (chat_id, user_id, product, now - REDO_WINDOW)).fetchone()
        if prev:
            c.execute("UPDATE sticker_log SET score=-1 WHERE id=? AND score=0", (prev[0],))
        cur = c.execute("INSERT INTO sticker_log(ts, chat_id, user_id, product, request, kind, spec, family, outcome, msg_id) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (now, chat_id, user_id, product, req, kind, json.dumps(spec, ensure_ascii=False), recipes.family(spec), outcome, msg_id))
        return cur.lastrowid
    return await db.atomic(run)


async def mark(db, log_id: int, score: int, now: int | None = None) -> bool:
    """점수 기록. +1 이 PROMOTE_AT 을 넘긴 조합은 레시피로 올린다. → 바뀌었으면 True."""
    now = now or int(time.time())

    def run(c):
        row = c.execute("SELECT chat_id, product, request, spec, family, score FROM sticker_log WHERE id=?", (log_id,)).fetchone()
        if not row or row[5] == score:
            return False
        c.execute("UPDATE sticker_log SET score=? WHERE id=?", (score, log_id))
        spec = json.loads(row[3]); sig = signature(spec)
        if score > 0:
            goods = c.execute("SELECT COUNT(*) FROM sticker_log WHERE chat_id=? AND product=? AND score>0 AND spec LIKE ?",
                              (row[0], row[1], f'%"motion": {json.dumps(spec.get("motion"), ensure_ascii=False)}%')).fetchone()[0]
            same = [r for r in c.execute("SELECT spec FROM sticker_log WHERE chat_id=? AND product=? AND score>0", (row[0], row[1]))
                    if signature(json.loads(r[0])) == sig]
            if len(same) >= PROMOTE_AT:
                c.execute("INSERT INTO sticker_recipes(chat_id, name, product, family, signature, spec, good, last_used) "
                          "VALUES(?,?,?,?,?,?,1,?) ON CONFLICT(chat_id, product, signature) DO UPDATE SET good=good+1, last_used=excluded.last_used",
                          (row[0], auto_name(row[2], spec), row[1], row[4], sig, row[3], now))
                _trim(c, row[0], now)
        else:
            c.execute("UPDATE sticker_recipes SET bad=bad+1 WHERE chat_id=? AND product=? AND signature=?", (row[0], row[1], sig))
        return True
    return await db.atomic(run)


def _trim(c, chat_id: int, now: int) -> None:
    c.execute("DELETE FROM sticker_recipes WHERE chat_id=? AND last_used<?", (chat_id, now - EXPIRE_DAYS * 86400))
    extra = c.execute("SELECT id FROM sticker_recipes WHERE chat_id=? ORDER BY (good-bad) DESC, last_used DESC LIMIT -1 OFFSET ?",
                      (chat_id, MAX_PER_CHAT)).fetchall()
    if extra:
        c.execute(f"DELETE FROM sticker_recipes WHERE id IN ({','.join('?' * len(extra))})", [r[0] for r in extra])


_last_tick = 0


async def tick(svc, bot) -> None:
    """30초 틱 → 하루 한 번만 정리."""
    global _last_tick
    if time.time() - _last_tick < 86400:
        return
    _last_tick = time.time()
    await cleanup(svc.db)


async def cleanup(db, now: int | None = None) -> None:
    """90일 안 쓴 레시피 삭제 (틱 훅에서 가끔)."""
    now = now or int(time.time())
    await db.conn.execute("DELETE FROM sticker_recipes WHERE last_used<?", (now - EXPIRE_DAYS * 86400,))
    await db.conn.commit()


# ── 반응 신호 (이미 오는 업데이트만) ─────────────────────────────
async def on_reaction(svc, bot, reaction) -> None:
    """message_reaction 업데이트: 우리가 보낸 스티커/파일에 👍❤️🔥 → +1."""
    emojis = {getattr(r, "emoji", None) for r in (getattr(reaction, "new_reaction", None) or ())}
    if not emojis & GOOD_EMOJI:
        return
    chat_id = reaction.chat.id
    row = await svc.db._one("SELECT id FROM sticker_log WHERE chat_id=? AND msg_id=? ORDER BY id DESC LIMIT 1",
                            (chat_id, reaction.message_id))
    if row:
        await mark(svc.db, row["id"], 1)


async def on_group_message(svc, bot, msg, role) -> None:
    """우리 스티커에 답장한 글: '좋다·예쁘다·완벽' = +1, '별로·다른 느낌' = -1.
    답장이 아니어도 만든 사람이 REDO_WINDOW 안에 '별로·다르게' 라고 하면 그 사람의 마지막 것 -1."""
    text = (getattr(msg, "text", None) or getattr(msg, "caption", None) or "")
    reply = getattr(msg, "reply_to_message", None)
    user = getattr(msg, "from_user", None)
    if reply is not None and getattr(reply, "message_id", None):
        row = await svc.db._one("SELECT id FROM sticker_log WHERE chat_id=? AND msg_id=? ORDER BY id DESC LIMIT 1",
                                (msg.chat_id, reply.message_id))
        if row:
            if BAD_WORDS.search(text):
                await mark(svc.db, row["id"], -1)
            elif GOOD_WORDS.search(text):
                await mark(svc.db, row["id"], 1)
            return
    if user is not None and BAD_WORDS.search(text):
        row = await svc.db._one("SELECT id FROM sticker_log WHERE chat_id=? AND user_id=? AND ts>? AND outcome='ok' ORDER BY id DESC LIMIT 1",
                                (msg.chat_id, user.id, int(time.time()) - REDO_WINDOW))
        if row:
            await mark(svc.db, row["id"], -1)


# ── 카탈로그에 쓸 취향 ──────────────────────────────────────────
async def preferences(db, chat_id: int, user_id: int, product: str = "sticker") -> dict:
    """{liked: [레시피 행…], disliked_sigs: {…}, recent_families: [최근 3번 계열]}."""
    liked = await db._all("SELECT name, family, signature, spec, good, bad FROM sticker_recipes WHERE chat_id=? AND product=? "
                          "AND good>bad ORDER BY (good-bad) DESC, last_used DESC LIMIT 6", (chat_id, product))
    bad_rows = await db._all("SELECT spec FROM sticker_log WHERE chat_id=? AND product=? AND score<0 ORDER BY id DESC LIMIT 30",
                             (chat_id, product))
    recent = await db._all("SELECT family FROM sticker_log WHERE chat_id=? AND user_id=? AND product=? ORDER BY id DESC LIMIT ?",
                           (chat_id, user_id, product, RECENT_FAMILIES))
    user_liked = await db._all("SELECT spec FROM sticker_log WHERE chat_id=? AND user_id=? AND product=? AND score>0 ORDER BY id DESC LIMIT 10",
                               (chat_id, user_id, product))
    return {"liked": [dict(r) for r in liked], "disliked_sigs": {signature(json.loads(r["spec"])) for r in bad_rows},
            "recent_families": [r["family"] for r in recent], "user_liked_sigs": {signature(json.loads(r["spec"])) for r in user_liked}}


def order_candidates(cands: list, prefs: dict) -> list:
    """정적 레시피 후보를 취향으로 다시 정렬: 이 사람이 좋아한 조합 → 방이 좋아한 조합 → 나머지, 별로였던 건 맨 뒤,
    최근 3번 계열은 뒤로 미룬다 (같은 사람에게 같은 계열만 반복하지 않게)."""
    def key(r):
        sig = signature(r); fam = recipes.family(r)
        return (sig in prefs.get("disliked_sigs", ()), fam in prefs.get("recent_families", ()),
                -(2 if sig in prefs.get("user_liked_sigs", ()) else 0) - (1 if any(l["signature"] == sig for l in prefs.get("liked", [])) else 0))
    return sorted(cands, key=key)


async def touch_recipe(db, chat_id: int, name: str, product: str = "sticker") -> None:
    await db.conn.execute("UPDATE sticker_recipes SET uses=uses+1, last_used=? WHERE chat_id=? AND product=? AND name=?",
                          (int(time.time()), chat_id, product, name))
    await db.conn.commit()


async def learned_recipe(db, chat_id: int, name: str, product: str = "sticker") -> dict | None:
    row = await db._one("SELECT spec FROM sticker_recipes WHERE chat_id=? AND product=? AND name=?", (chat_id, product, name))
    return json.loads(row["spec"]) if row else None
