"""🎧 자동선곡(라디오) 2.0 — 이 방이 실제로 이어 튼 기록으로 다음 곡을 고름.

세션 추천 학계(Jannach·Ludewig 벤치마크)의 교훈: 동시출현·순차규칙(SR) 같은 단순 카운트가
GRU4Rec·GNN 같은 신경망을 대부분 이기고, CPU로 즉시(마이크로초) 된다. GPU·외부 API 불필요.

소담 설계: `music_queue`(이미 쌓이는 재생 기록)에서 **그때그때 계산** — 별도 학습 표·훅이 없어서
버그 표면이 작다. 신호 세 개를 섞어 점수화:
  ① 방 순차규칙(SR): 이 방에서 '씨드 곡' 다음에 자주/가까이 온 곡 (거리가중 1/d, 앞 LOOKBACK 곡)
  ② 방 인기도: 이 방에서 많이 튼 곡
  ③ 같은 가수 보너스: 씨드와 같은 가수면 가점 (단 2연속이면 빼서 다양성)
최근 튼 곡은 빼서 바로 반복 안 되게. 후보가 없으면 None → 호출부가 유튜브 믹스로 폴백.
설계 근거: docs/AUTODJ_DESIGN.md.
"""
from __future__ import annotations

from . import musicmatch as mm

LOOKBACK = 3            # SR 거리가중: 앞 몇 곡까지 보는지 (가중 1/d)
HISTORY_MAX = 2000     # 계산에 쓸 최근 재생 기록 수 (방 90일 기록도 이 안이면 전부)
MIN_HISTORY = 4        # 기록이 이보다 적으면 자동선곡 안 함 (유튜브 폴백)
W_SR = 1.0             # 방 순차규칙 가중
W_POP = 0.4            # 방 인기도 가중
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


async def pick(db, chat_id: int, seed_key: str, seed_title: str,
               recent_keys: set[str], exclude: set[str] | None = None) -> dict | None:
    """이 방 재생 기록으로 씨드 곡 다음에 틀 곡 하나를 고름 (외부 호출 0). 없으면 None.

    recent_keys: 최근 튼 곡의 song_key (바로 반복 방지로 후보에서 뺌).
    exclude: 빼고 싶은 재생 ID(vid/orig_vid) 집합.
    """
    exclude = exclude or set()
    rows = await db._all(
        "SELECT title, url, vid, orig_vid, duration FROM music_queue "
        "WHERE chat_id=? AND started_ts IS NOT NULL AND state!='removed' "
        "ORDER BY id DESC LIMIT ?", (chat_id, HISTORY_MAX))
    if len(rows) < MIN_HISTORY:
        return None
    seq = [dict(r) for r in reversed(rows)]      # 오래된 → 최신 순서

    keys = [mm.song_key(r.get("title") or "") for r in seq]
    rep: dict[str, dict] = {}                    # song_key → 재생할 대표 행 (최근 것)
    pop: dict[str, int] = {}                     # song_key → 튼 횟수
    for r, k in zip(seq, keys):
        if not k:
            continue
        pop[k] = pop.get(k, 0) + 1
        if _playable(r):
            rep[k] = r                           # 최신(뒤쪽)이 덮어씀

    # ① 방 순차규칙(SR): 씨드 곡 다음에(거리 d 이내) 온 곡을 1/d 로 가중
    sr: dict[str, float] = {}
    for j, k in enumerate(keys):
        if not k or k == seed_key:
            continue
        for d in range(1, LOOKBACK + 1):
            if j - d < 0:
                break
            if keys[j - d] == seed_key:
                sr[k] = sr.get(k, 0.0) + 1.0 / d

    cands = (set(sr) | set(pop)) - {seed_key} - set(recent_keys)
    cands = {k for k in cands if k in rep and _playable(rep[k]) not in exclude}
    if not cands:
        return None

    seed_art = artist_key(seed_title)
    # 같은 가수 2연속 방지: 최근 ARTIST_RUN 곡이 모두 씨드와 같은 가수면 보너스 끔
    tail_arts = [artist_key(r.get("title") or "") for r in seq[-ARTIST_RUN:]]
    same_run = sum(1 for a in tail_arts if a and a == seed_art)
    artist_bonus = W_ARTIST if (seed_art and same_run < ARTIST_RUN) else 0.0

    sr_max = max(sr.values()) if sr else 1.0
    pop_max = max(pop.values()) if pop else 1.0

    def score(k: str) -> float:
        s = W_SR * (sr.get(k, 0.0) / sr_max) + W_POP * (pop.get(k, 0) / pop_max)
        if artist_bonus and artist_key(rep[k].get("title") or "") == seed_art:
            s += artist_bonus
        return s

    best = max(cands, key=lambda k: (score(k), sr.get(k, 0.0), pop.get(k, 0)))
    r = rep[best]
    return {"title": r.get("title") or "", "url": r.get("url") or "",
            "vid": _playable(r), "duration": r.get("duration") or 0}
