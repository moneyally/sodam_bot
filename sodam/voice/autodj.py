"""🎧 자동선곡(라디오) 2.0 — 재생 기록으로 다음 곡을 고름 (이 방 + 다른 방).

세션 추천 학계(Jannach·Ludewig 벤치마크)의 교훈: 동시출현·순차규칙(SR) 같은 단순 카운트가
GRU4Rec·GNN 같은 신경망을 대부분 이기고, CPU로 즉시(마이크로초) 된다. GPU·외부 API 불필요.

소담 설계: `music_queue`(이미 쌓이는 재생 기록)에서 **그때그때 계산** — 별도 학습 표·훅이 없어서
버그 표면이 작다. 신호를 섞어 점수화:
  ① 방 순차규칙(SR): 이 방에서 '씨드 곡' 다음에 자주/가까이 온 곡 (거리가중 1/d, 앞 LOOKBACK 곡)
  ② 방 인기도: 이 방에서 많이 튼 곡
  ③ 전역 순차규칙(다른 방 SR): 다른 방들에서 씨드 곡 다음에 온 곡 — 낮은 가중 (새·얇은 방 콜드스타트)
  ④ 전역 인기도: 다른 방들에서 많이 튼 곡
  ⑤ 같은 가수 보너스: 씨드와 같은 가수면 가점 (단 2연속이면 빼서 다양성)
이 방 신호가 우선(가중 큼), 다른 방은 보조. 최근 튼 곡은 빼서 바로 반복 안 되게.
방+전역 기록 합이 MIN_HISTORY 미만이거나 후보가 없으면 None → 호출부가 유튜브 믹스로 폴백.
설계 근거: docs/AUTODJ_DESIGN.md.
"""
from __future__ import annotations

from collections import defaultdict

from . import musicmatch as mm

LOOKBACK = 3            # SR 거리가중: 앞 몇 곡까지 보는지 (가중 1/d)
HISTORY_MAX = 2000     # 이 방 계산에 쓸 최근 재생 기록 수
GLOBAL_MAX = 5000      # 다른 방(전역) 계산에 쓸 최근 재생 기록 수 (방 경계로 끊어 세므로 섞이지 않음)
MIN_HISTORY = 4        # 방+전역 기록 합이 이보다 적으면 자동선곡 안 함 (유튜브 폴백)
W_SR = 1.0             # 방 순차규칙 가중 (제일 큼 — 이 방 취향 우선)
W_POP = 0.4            # 방 인기도 가중
W_GSR = 0.5            # 다른 방 순차규칙 가중 (보조)
W_GPOP = 0.25          # 다른 방 인기도 가중 (보조)
W_ARTIST = 0.5         # 같은 가수 보너스
ARTIST_RUN = 2         # 최근 이 수만큼 같은 가수가 연속이면 같은-가수 보너스 끔 (다양성)

_SEPS = (" - ", " – ", " — ", " / ", "-", "–", "—")


def artist_key(title: str) -> str:
    """제목에서 가수 부분만 정규화. 유튜브 흔한 포맷 '가수 - 곡' 기준, 없으면 ''(가수 모름)."""
    t = (title or "").strip()
    for sep in _SEPS:
        if sep in t:
            return mm.norm(t.split(sep, 1)[0])
    return ""


def _playable(row: dict) -> str | None:
    """재생에 쓸 ID (대체 음원 전 원래 ID 우선) — 없으면 None."""
    return row.get("orig_vid") or row.get("vid") or None


def _signals(rows: list[dict], seed_key: str):
    """재생 기록(chat_id 섞여 있을 수 있음)에서 SR·인기도·대표행 계산. 전이는 같은 방 안에서만 센다.

    rows 는 오래된→최신 순서. 반환: (sr, pop, rep).
    """
    by_room: dict[int, list[tuple[str, dict]]] = defaultdict(list)
    for r in rows:
        k = mm.song_key(r.get("title") or "")
        by_room[r.get("chat_id")].append((k, r))
    sr: dict[str, float] = {}
    pop: dict[str, int] = {}
    rep: dict[str, dict] = {}
    for seq in by_room.values():
        keys = [k for k, _ in seq]
        for k, r in seq:
            if not k:
                continue
            pop[k] = pop.get(k, 0) + 1
            if _playable(r):
                rep[k] = r                       # 최신(뒤쪽)이 덮어씀
        for j, k in enumerate(keys):             # 순차규칙: 씨드 다음에(거리 d) 온 곡
            if not k or k == seed_key:
                continue
            for d in range(1, LOOKBACK + 1):
                if j - d < 0:
                    break
                if keys[j - d] == seed_key:
                    sr[k] = sr.get(k, 0.0) + 1.0 / d
    return sr, pop, rep


async def _load(db, where: str, args: tuple, limit: int) -> list[dict]:
    rows = await db._all(
        "SELECT chat_id, title, url, vid, orig_vid, duration FROM music_queue "
        "WHERE started_ts IS NOT NULL AND state!='removed' AND " + where +
        " ORDER BY id DESC LIMIT ?", (*args, limit))
    return [dict(r) for r in reversed(rows)]      # 오래된 → 최신


async def pick(db, chat_id: int, seed_key: str, seed_title: str,
               recent_keys: set[str], exclude: set[str] | None = None) -> dict | None:
    """씨드 곡 다음에 틀 곡 하나를 고름 (이 방 + 다른 방 기록, 외부 호출 0). 없으면 None.

    recent_keys: 최근 튼 곡의 song_key (바로 반복 방지로 후보에서 뺌).
    exclude: 빼고 싶은 재생 ID(vid/orig_vid) 집합.
    """
    exclude = exclude or set()
    room_rows = await _load(db, "chat_id=?", (chat_id,), HISTORY_MAX)
    other_rows = await _load(db, "chat_id!=?", (chat_id,), GLOBAL_MAX)
    if len(room_rows) + len(other_rows) < MIN_HISTORY:
        return None

    r_sr, r_pop, r_rep = _signals(room_rows, seed_key)      # 이 방
    g_sr, g_pop, g_rep = _signals(other_rows, seed_key)     # 다른 방들
    rep = dict(g_rep)
    rep.update(r_rep)                                        # 이 방 대표가 우선

    cands = (set(r_sr) | set(r_pop) | set(g_sr) | set(g_pop)) - {seed_key} - set(recent_keys)
    cands = {k for k in cands if k in rep and _playable(rep[k]) not in exclude}
    if not cands:
        return None

    seed_art = artist_key(seed_title)
    # 같은 가수 2연속 방지: 이 방 최근 ARTIST_RUN 곡이 모두 씨드와 같은 가수면 보너스 끔
    tail_arts = [artist_key(r.get("title") or "") for r in room_rows[-ARTIST_RUN:]]
    same_run = sum(1 for a in tail_arts if a and a == seed_art)
    artist_bonus = W_ARTIST if (seed_art and same_run < ARTIST_RUN) else 0.0

    rs_max = max(r_sr.values()) if r_sr else 1.0
    rp_max = max(r_pop.values()) if r_pop else 1.0
    gs_max = max(g_sr.values()) if g_sr else 1.0
    gp_max = max(g_pop.values()) if g_pop else 1.0

    def score(k: str) -> float:
        s = W_SR * (r_sr.get(k, 0.0) / rs_max) + W_POP * (r_pop.get(k, 0) / rp_max)
        s += W_GSR * (g_sr.get(k, 0.0) / gs_max) + W_GPOP * (g_pop.get(k, 0) / gp_max)
        if artist_bonus and artist_key(rep[k].get("title") or "") == seed_art:
            s += artist_bonus
        return s

    best = max(cands, key=lambda k: (score(k), r_sr.get(k, 0.0), g_sr.get(k, 0.0),
                                     r_pop.get(k, 0), g_pop.get(k, 0)))
    r = rep[best]
    return {"title": r.get("title") or "", "url": r.get("url") or "",
            "vid": _playable(r), "duration": r.get("duration") or 0}
