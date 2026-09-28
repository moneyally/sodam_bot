"""스팸 실험실 공통: 경로·가격·분할·도우미.

실제 DB 스냅샷·실제 글이 든 units.json·LLM 결과 캐시는 전부 SPAMLAB_DIR(저장소 밖)에만 둔다.
저장소에 들어가는 건 스크립트·synthetic.jsonl(직접 쓴 가짜 글)·RESULTS.md(집계 숫자만) 뿐.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

LAB = Path(os.getenv("SPAMLAB_DIR", "/tmp/claude-0/-home-user/75158940-10f4-5812-ab02-a1c2dcc02f30/scratchpad/spamlab"))
SNAPSHOT = LAB / "lab.db"          # 읽기 전용 스냅샷 (sqlite backup)
UNITS = LAB / "units.json"         # 실제 글 포함 → 저장소 밖
SYNTH = HERE / "synthetic.jsonl"   # 가짜 글만 → 저장소 안
CACHE = LAB / "llm_cache.jsonl"

# gpt-5.4-mini 요금 (sodam/costs.py 와 같음, $/1M 토큰: 입력, 캐시 입력, 출력)
PRICE = {"gpt-5.4-mini": (0.75, 0.075, 4.50), "text-embedding-3-small": (0.02, 0.02, 0.0)}


def usd(model: str, prompt: int, cached: int, out: int) -> float:
    p, c, o = PRICE[model]
    return ((prompt - cached) * p + cached * c + out * o) / 1e6


def split_of(key: str) -> str:
    """dev/test 고정 분할 (해시). 스팸은 family, 정상은 user_id 로."""
    return "dev" if int(hashlib.sha1(key.encode()).hexdigest(), 16) % 2 == 0 else "test"


def load_units() -> list[dict]:
    return json.loads(UNITS.read_text())


def openai_client():
    from dotenv import load_dotenv
    from openai import AsyncOpenAI
    load_dotenv(os.getenv("SODAM_ENV", str(ROOT.parents[2] / ".env") if "worktrees" in str(ROOT) else ".env"))
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise SystemExit("OPENAI_API_KEY 없음 (SODAM_ENV=/path/.env)")
    return AsyncOpenAI(api_key=key)


def guard_model() -> str:
    return os.getenv("OPENAI_GUARD_MODEL", "gpt-5.4-mini").strip() or "gpt-5.4-mini"


class Cache:
    """(key → 결과) JSONL 캐시. 같은 입력으로 다시 돌려도 API 비용 0."""

    def __init__(self, path: Path = CACHE):
        self.path = path
        self.d: dict[str, dict] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    r = json.loads(line)
                    self.d[r["k"]] = r["v"]

    @staticmethod
    def key(*parts: str) -> str:
        return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()

    def get(self, k: str):
        return self.d.get(k)

    def put(self, k: str, v: dict) -> None:
        self.d[k] = v
        with self.path.open("a") as f:
            f.write(json.dumps({"k": k, "v": v}, ensure_ascii=False) + "\n")
