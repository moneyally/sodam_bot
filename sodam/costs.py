"""AI 요금 계산 (달러). 요금표는 OpenAI 공개 가격 (2026-09 확인, 1M 토큰당): 바뀌면 PRICES 만 고친다.

counters(chat_id=0) 의 모델별 키 m:<모델>:in/cached/out 으로 정확히 계산 (llm._record, 2026-09-27 부터 기록).
그 전 날짜(모델 구분 없음)는 합계 tokens/prompt_tokens/cached_tokens 로 '전부 기본 모델' ~ '전부 mini' 범위만.
"""
from __future__ import annotations

# 모델: (입력, 캐시 입력, 출력) $/1M 토큰
PRICES: dict[str, tuple[float, float, float]] = {
    "gpt-5.4": (2.50, 0.25, 15.00),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
}
WEB_SEARCH_PER_CALL = 0.01        # 웹 검색 도구 1번 ($10 / 1천 번)


def price_of(model: str) -> tuple[float, float, float] | None:
    if model in PRICES:
        return PRICES[model]
    base = max((m for m in PRICES if model.startswith(m + "-20")), key=len, default=None)   # 날짜 붙은 스냅샷 이름
    return PRICES.get(base) if base else None


def token_cost(model: str, inp: int, cached: int, out: int) -> float | None:
    p = price_of(model)
    if p is None:
        return None
    return ((inp - cached) * p[0] + cached * p[1] + out * p[2]) / 1_000_000


def by_model(counters: dict[str, int]) -> dict[str, dict]:
    """counters(key→n, chat_id=0 하루치) → 모델별 {in, cached, out, calls, usd(None=요금 모름)}."""
    out: dict[str, dict] = {}
    for k, n in counters.items():
        if k.startswith("m:") and k.count(":") >= 2:
            model, field = k[2:].rsplit(":", 1)
            out.setdefault(model, {"in": 0, "cached": 0, "out": 0, "calls": 0})[field] = n
    for m, d in out.items():
        d["usd"] = token_cost(m, d["in"], d["cached"], d["out"])
    return out


def total_range(prompt: int, cached: int, total: int, main: str, mini: str) -> tuple[float, float]:
    """모델 구분이 없는 합계로 (전부 mini, 전부 기본 모델) 요금 범위."""
    out = max(0, total - prompt)
    lo = token_cost(mini, prompt, cached, out) or 0.0
    hi = token_cost(main, prompt, cached, out) or 0.0
    return lo, hi
