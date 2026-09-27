"""B 탐지기용 임베딩 (text-embedding-3-small, 정규화). 결과는 저장소 밖 emb.json 에 캐시.

    SODAM_ENV=/path/.env python tools/spam_lab/embed.py
"""
from __future__ import annotations

import asyncio
import json
import math

from common import LAB, load_units, openai_client, usd

EMB = LAB / "emb.json"
MODEL = "text-embedding-3-small"


def texts_needed(data: dict) -> list[str]:
    s = {m["text"] for m in data["real_msgs"] if len(m["text"]) >= 12}
    for u in data["units"]:
        s |= {t for t in u["msgs"] if len(t) >= 12}
    return sorted(s)


def load() -> dict[str, list[float]]:
    return json.loads(EMB.read_text()) if EMB.exists() else {}


async def main() -> None:
    data = json.loads((LAB / "units.json").read_text())
    have = load()
    todo = [t for t in texts_needed(data) if t not in have]
    print("to embed:", len(todo))
    client = openai_client()
    tokens = 0
    for i in range(0, len(todo), 256):
        batch = [t[:2000] for t in todo[i:i + 256]]
        r = await client.embeddings.create(model=MODEL, input=batch)
        tokens += r.usage.total_tokens
        for t, d in zip(todo[i:i + 256], r.data):
            n = math.sqrt(sum(x * x for x in d.embedding)) or 1.0
            have[t] = [round(x / n, 5) for x in d.embedding]
    EMB.write_text(json.dumps(have))
    print(f"tokens {tokens} cost ${usd(MODEL, tokens, 0, 0):.4f}")


if __name__ == "__main__":
    _ = load_units
    asyncio.run(main())
