"""AI 비용 기록·계산: python tests/run_all.py costs"""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from fakes import make_db, runner
from test_fix_billing_ai import cfg

from sodam import costs
from sodam.llm import LLM

test, run_all = runner()
ROOT = Path(__file__).resolve().parent.parent


def fake_client(prompt=1000, cached=600, total=1100):
    usage = SimpleNamespace(total_tokens=total, prompt_tokens=prompt,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=cached))

    async def create(**kw):
        return SimpleNamespace(usage=usage, choices=[SimpleNamespace(message=SimpleNamespace(content="{}", tool_calls=None))])

    async def resp(**kw):
        return SimpleNamespace(usage=usage, output_text="요약")
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
                           responses=SimpleNamespace(create=resp))


@test
async def tokens_recorded_per_model():
    db = await make_db()
    llm = LLM(cfg(db.path), db)
    llm.client = fake_client()
    await llm.chat([{"role": "user", "content": "x"}], model="gpt-5.4", purpose="agent:member")
    await llm.json("s", "u", model="gpt-5.4-mini", purpose="digest")
    await llm.web_search("뉴스")
    day = llm._today()
    g = {r["key"]: r["n"] for r in await db._all("SELECT key, n FROM counters WHERE day=? AND chat_id=0", (day,))}
    assert g["m:gpt-5.4:in"] == 1000 and g["m:gpt-5.4:cached"] == 600 and g["m:gpt-5.4:out"] == 100, g
    assert g["m:gpt-5.4:calls"] == 1 and g["m:gpt-5.4-mini:calls"] == 2 and g["web_search_calls"] == 1, g
    m = costs.by_model(g)
    # gpt-5.4: 400×2.5 + 600×0.25 + 100×15 = 1000+150+1500 = 2650 / 1M
    assert abs(m["gpt-5.4"]["usd"] - 0.00265) < 1e-9, m


@test
def cost_math_and_unknown_model():
    assert costs.token_cost("gpt-5.4-mini", 1_000_000, 0, 0) == 0.75
    assert costs.token_cost("gpt-5.4-2026-03-05", 0, 0, 1_000_000) == 15.0     # 날짜 붙은 이름
    assert costs.token_cost("gpt-image-9", 10, 0, 10) is None
    lo, hi = costs.total_range(1_000_000, 500_000, 1_100_000, "gpt-5.4", "gpt-5.4-mini")
    assert lo < hi and abs(hi - (500_000 * 2.5 + 500_000 * 0.25 + 100_000 * 15) / 1e6) < 1e-9


@test
async def usage_report_runs_read_only():
    db = await make_db()
    await db.bump("2026-09-27", 0, "tokens", 2_100_000)
    await db.bump("2026-09-27", 0, "prompt_tokens", 2_000_000)
    await db.bump("2026-09-27", 0, "m:gpt-5.4:in", 500_000)
    await db.conn.commit()
    out = subprocess.run([sys.executable, str(ROOT / "tools" / "usage_report.py"), "--day", "2026-09-27", "--db", db.path],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "예산 다 씀" in out.stdout and "gpt-5.4" in out.stdout and "모델 기록 전" in out.stdout, out.stdout


if __name__ == "__main__":
    run_all()
