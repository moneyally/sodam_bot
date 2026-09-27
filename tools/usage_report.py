"""하루 사용량·비용 점검 (운영자용, DB 읽기 전용): python tools/usage_report.py [--day 2026-09-27] [--db data/sodam.db]

대화(메시지·방·사람) · AI(답변·호출·토큰·캐시·예산) · 비용(모델별 정확, 모델 기록 전은 범위) · 방별·기능별 토큰 · DB 크기.
돌아가는 봇에 영향 없음 (SQLite mode=ro).
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sodam import costs  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOM_TOKENS = "room_tokens"


def env(key: str, default: str) -> str:
    v = os.getenv(key)
    if v:
        return v
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip() or default
    return default


def usd(x: float | None) -> str:
    return "요금표 없음" if x is None else f"${x:,.3f} (약 {x * 1400:,.0f}원)"


def report(db_path: Path, day: str) -> str:
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    d0 = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=KST).timestamp())
    d1 = d0 + 86400
    main, mini = env("OPENAI_MODEL", "gpt-5.4"), env("OPENAI_GUARD_MODEL", "gpt-5.4-mini")
    budget = int(env("DAILY_TOKEN_BUDGET", "2000000"))
    g = {r["key"]: r["n"] for r in c.execute("SELECT key, n FROM counters WHERE day=? AND chat_id=0", (day,))}
    title = {r["chat_id"]: r["title"] for r in c.execute("SELECT chat_id, title FROM chats")}
    name = lambda cid: title.get(cid) or ("1:1 " + str(cid) if cid > 0 else str(cid))   # noqa: E731
    L = [f"📊 소담 사용량 · {day} (한국시간)", ""]

    # 대화
    m = c.execute("SELECT COUNT(*) n, COUNT(DISTINCT chat_id) rooms, COUNT(DISTINCT user_id) people, "
                  "SUM(is_bot) bot FROM messages WHERE ts>=? AND ts<?", (d0, d1)).fetchone()
    L.append(f"💬 대화 {m['n']:,}개 (봇 글 {m['bot'] or 0:,}) · 방 {m['rooms']} · 말한 사람 {m['people']}")
    for r in c.execute("SELECT chat_id, COUNT(*) n FROM messages WHERE ts>=? AND ts<? GROUP BY chat_id "
                       "ORDER BY n DESC LIMIT 8", (d0, d1)):
        L.append(f"   {name(r['chat_id'])[:18]:18s} {r['n']:>6,}")

    # AI
    turns = c.execute("SELECT COUNT(*) FROM ai_turns WHERE ts>=? AND ts<?", (d0, d1)).fetchone()[0]
    tot, pr, ca = g.get("tokens", 0), g.get("prompt_tokens", 0), g.get("cached_tokens", 0)
    L += ["", f"🤖 AI 답변 {turns:,}번 · 토큰 {tot:,} / 하루 예산 {budget:,} ({tot * 100 // max(budget, 1)}%)"
          + ("  ⛔ 예산 다 씀 → 자정까지 AI 멈춤" if tot >= budget else ""),
          f"   입력 {pr:,} (캐시 {ca:,} = {ca * 100 // max(pr, 1)}%) · 출력 {max(0, tot - pr):,}"]
    imgs = c.execute("SELECT COALESCE(SUM(n),0) FROM counters WHERE day=? AND key='image'", (day,)).fetchone()[0]
    webs = g.get("web_search_calls") or c.execute(
        "SELECT COALESCE(SUM(n),0) FROM counters WHERE day=? AND key='web_search'", (day,)).fetchone()[0]
    L.append(f"   그림 {imgs}장 · 웹 검색 {webs}번")

    # 비용
    L += ["", "💵 비용"]
    models = costs.by_model(g)
    if models:
        known = 0.0
        for mdl, d in sorted(models.items(), key=lambda kv: -(kv[1]["usd"] or 0)):
            L.append(f"   {mdl:22s} 호출 {d['calls']:>5} · 입력 {d['in']:>9,} (캐시 {d['cached']:,}) · 출력 {d['out']:>8,} → {usd(d['usd'])}")
            known += d["usd"] or 0
        rec_in = sum(d["in"] for d in models.values())
        if pr - rec_in > 1000:                 # 모델별 기록 시작 전 몫
            p_old = pr - rec_in
            c_old = max(0, ca - sum(d["cached"] for d in models.values()))
            t_old = max(0, tot - rec_in - sum(d["out"] for d in models.values()))
            lo, hi = costs.total_range(p_old, c_old, p_old + t_old, main, mini)
            L.append(f"   (모델 기록 전 {p_old:,} 토큰: {usd(lo)} ~ {usd(hi)})")
            known_lo, known_hi = known + lo, known + hi
            L.append(f"   합계 약 {usd(known_lo)} ~ {usd(known_hi)}")
        else:
            L.append(f"   합계 {usd(known)}")
    else:
        lo, hi = costs.total_range(pr, ca, tot, main, mini)
        L.append(f"   모델별 기록 없음 → 전부 {mini} {usd(lo)} ~ 전부 {main} {usd(hi)}")
    if webs:
        L.append(f"   웹 검색 요금 {usd(webs * costs.WEB_SEARCH_PER_CALL)}")
    if imgs:
        L.append("   그림 요금은 이미지 모델 요금표가 없어 토큰에 포함 안 됨 (위 모델별에 이미지 모델이 있으면 참고)")

    # 방별 토큰
    L += ["", "🏠 방별 AI 토큰 (방 하루 한도 600,000)"]
    for r in c.execute("SELECT chat_id, n FROM counters WHERE day=? AND key=? ORDER BY n DESC LIMIT 12", (day, ROOM_TOKENS)):
        pct = r["n"] * 100 // max(tot, 1)
        flag = " ⚠️ 한도 근접" if r["n"] >= 500_000 else ""
        L.append(f"   {name(r['chat_id'])[:18]:18s} {r['n']:>9,} ({pct}%){flag}")

    # 기능별
    feats = sorted(((g[k], k[7:], g.get("cached:" + k[7:], 0)) for k in g if k.startswith("prompt:")), reverse=True)
    if feats:
        L += ["", "🧩 기능별 입력 토큰 (캐시 적중)"]
        for n, p, cc in feats:
            L.append(f"   {p:16s} {n:>9,} ({cc * 100 // max(n, 1)}%)")

    # DB
    L += ["", "🗄️ DB"]
    size = sum(f.stat().st_size for f in db_path.parent.glob(db_path.name + "*") if f.is_file())
    L.append(f"   파일 {size / 1_048_576:,.1f} MB (WAL 포함)")
    rows = []
    for t in ("messages", "members", "users", "ai_turns", "mod_log", "casino_ledger", "counters", "knowledge_chunks"):
        try:
            rows.append(f"{t} {c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:,}")
        except sqlite3.Error:
            pass
    L.append("   " + " · ".join(rows))
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=datetime.now(KST).strftime("%Y-%m-%d"))
    ap.add_argument("--db", default=str(ROOT / "data" / "sodam.db"))
    a = ap.parse_args()
    print(report(Path(a.db), a.day))


if __name__ == "__main__":
    main()
