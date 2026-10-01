"""📞 음성 통화 요금 = 실제 토큰 (2026-10-01 점검: response.done usage 를 받기만 하고 버리고 분당 0.08 추정만 썼음).
python tests/run_all.py voice_cost"""
from types import SimpleNamespace as N

from fakes import make_db, runner

from sodam.voice import store
from sodam.voice.bridge import add_usage

test, run_all = runner()


def usage(in_a=0, in_t=0, c_a=0, c_t=0, out_a=0, out_t=0):
    """SDK RealtimeResponseUsage 모양 (input_tokens = 글자+음성, cached 는 그 안의 일부)."""
    return N(input_tokens=in_a + in_t, output_tokens=out_a + out_t,
             input_token_details=N(audio_tokens=in_a, text_tokens=in_t, cached_tokens=c_a + c_t,
                                   cached_tokens_details=N(audio_tokens=c_a, text_tokens=c_t)),
             output_token_details=N(audio_tokens=out_a, text_tokens=out_t))


@test
async def usage_is_summed_by_kind():
    acc = {}
    add_usage(acc, usage(in_a=1000, in_t=2000, c_a=400, c_t=1500, out_a=3000, out_t=100))
    add_usage(acc, usage(in_a=500, out_a=1000))
    add_usage(acc, None)                                    # usage 없는 response.done
    assert (acc["in_audio"], acc["in_text"], acc["cached_audio"], acc["cached_text"], acc["out_audio"], acc["out_text"]) \
        == (1500, 2000, 400, 1500, 4000, 100), acc
    assert acc["input_tokens"] == 3500 and acc["cached_tokens"] == 1900 and acc["output_tokens"] == 4100


@test
async def mini_price_matches_openai_table():
    acc = {}
    add_usage(acc, usage(in_a=1_000_000, in_t=1_000_000, c_a=0, c_t=0, out_a=1_000_000, out_t=1_000_000))
    micro, how = store.cost_micro("gpt-realtime-2.1-mini", acc, 0)
    assert how == "tokens" and micro == int((0.60 + 2.40 + 10.00 + 20.00) * 1e6), micro
    acc = {}
    add_usage(acc, usage(in_a=1_000_000, in_t=1_000_000, c_a=1_000_000, c_t=1_000_000))   # 전부 캐시
    micro, _ = store.cost_micro("gpt-realtime-2.1-mini", acc, 0)
    assert micro == int((0.06 + 0.30) * 1e6), micro
    acc = {}
    add_usage(acc, usage(out_a=1_000_000))
    assert store.cost_micro("gpt-realtime-2.1", acc, 60)[0] == int((64.00 + store.TRANSCRIBE_USD_PER_MIN) * 1e6)


@test
async def unknown_model_or_no_usage_falls_back_to_per_minute():
    assert store.cost_micro("gpt-realtime-2.1-mini", {}, 120) == (int(2 * store.USD_PER_MIN * 1e6), "per_min")
    acc = {}
    add_usage(acc, usage(in_a=100, out_a=100))
    assert store.cost_micro("some-future-model", acc, 120)[1] == "per_min", "요금표에 없는 모델은 넉넉한 추정"


@test
async def record_cost_charges_real_tokens_to_budget():
    db = await make_db()
    acc = {}
    add_usage(acc, usage(in_a=20_000, in_t=5_000, c_t=4_000, out_a=8_000, out_t=500))
    micro = await store.record_cost(db, None, -100123, 300, "gpt-realtime-2.1-mini", acc)
    want = (1_000 * 0.60 + 4_000 * 0.06 + 500 * 2.40 + 20_000 * 10.00 + 8_000 * 20.00) / 1e6 + 5 * 0.003
    assert micro == int(want * 1e6), (micro, want)
    rows = {(r["chat_id"], r["key"]): r["n"] for r in await db._all("SELECT chat_id, key, n FROM counters")}
    assert rows[(0, "usd_micro")] == micro and rows[(-100123, "room_usd_micro")] == micro and rows[(0, "voice_seconds")] == 300


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_all())
