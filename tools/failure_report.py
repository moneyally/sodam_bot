"""소담이 '못 했다'·'모른다' 로 끝난 요청 모아보기 (운영자·Claude 모니터링용, DB 읽기 전용).

python tools/failure_report.py [--since-last] [--hours 24] [--db data/sodam.db]
--since-last: data/failure_seen 에 적힌 마지막 agent_runs id 이후만 보고 끝에 새 id 를 적음 (정기 점검용).
신호: 실행 상태(error/empty/budget) · 도구 결과의 거절·형식 오류 · 도구 없이 '못 해요/없어요/모르' 로 답한 경우.
대화 원문은 요청 60자·답 80자만 (운영자 본인 확인용 — 저장소·외부로 보내지 말 것).
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL_FAIL = re.compile(r"모름|못 보냄|형식이 안 맞음|사용할 수 없음|특정하지 못|꺼져 있|지어내지|실패|오류|없음 —|표시돼 있지 않음")
ANSWER_FAIL = re.compile(r"못 (해|합니다|했|틀|드려|보내|넣)|할 수 없|기능은 없|기능이 없|모르겠|몰라서|직접 넣으|직접 입력")


def report(db: Path, since_id: int | None, hours: int) -> tuple[str, int]:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    where, args = ("id > ?", (since_id,)) if since_id is not None else ("ts >= ?", (int(time.time()) - hours * 3600,))
    runs = c.execute(f"SELECT id, chat_id, user_id, ts, purpose, status, steps FROM agent_runs WHERE {where} ORDER BY id",
                     args).fetchall()
    last = max([r["id"] for r in runs], default=since_id or 0)
    items, kinds = [], Counter()
    for r in runs:
        steps = json.loads(r["steps"] or "[]")
        turn = c.execute("SELECT request, answer FROM ai_turns WHERE chat_id=? AND user_id=? AND ts BETWEEN ? AND ? "
                         "ORDER BY ts LIMIT 1", (r["chat_id"], r["user_id"], r["ts"] - 5, r["ts"] + 120)).fetchone()
        ans = (turn["answer"] if turn else "") or ""
        why = []
        if r["status"] in ("error", "empty", "budget"):
            why.append(f"상태 {r['status']}")
        why += [f"도구 {s['tool']}: {s.get('result', '')[:90]}" for s in steps if TOOL_FAIL.search(s.get("result", "") or "")]
        if not steps and ANSWER_FAIL.search(ans):
            why.append("도구 없이 거절")
        if why:
            kinds.update(w.split(":")[0] for w in why)
            items.append(f"#{r['id']} {time.strftime('%m-%d %H:%M', time.localtime(r['ts']))} chat={r['chat_id']} {r['purpose']}\n"
                         f"  요청: {(turn['request'] if turn else '')[:60]!r}\n  답: {ans[:80]!r}\n  " + "\n  ".join(why))
    head = f"실행 {len(runs)}개 중 실패 신호 {len(items)}개" + (f" · 종류 {dict(kinds.most_common(6))}" if kinds else "")
    return "\n".join([head, *items]), last


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-last", action="store_true")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--db", default=str(ROOT / "data" / "sodam.db"))
    a = ap.parse_args()
    mark = Path(a.db).parent / "failure_seen"
    since = int(mark.read_text()) if a.since_last and mark.exists() else None
    text, last = report(Path(a.db), since, a.hours)
    print(text)
    if a.since_last:
        mark.write_text(str(last))


if __name__ == "__main__":
    main()
