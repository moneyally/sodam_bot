"""💱 실시간 시세 화면 (방 관리자 1:1). 동작은 sodam/rates.py.

m:rt:<방ID>   켜기/끄기 (방에서 숫자+단위 치면 자동 변환). 명령 .시세·AI 도구는 설정과 무관하게 항상 됨.
"""
from __future__ import annotations

from .. import menu, rates, tools
from ..menu import ADMIN, B, HubItem, PanelCtx, Screen
from ..permissions import Role
from ..subscription import chat_title
from ..util import esc

menu.register_toggle("rate_enabled", "rt")


async def s_rt(c: PanelCtx) -> Screen:
    s = await c.svc.db.get_settings(c.cid)
    on = bool(s.get("rate_enabled"))
    lines = [f"💱 <b>{esc(await chat_title(c.svc, c.cid))}</b> 실시간 시세",
             f"상태: {'✅ 켜짐' if on else '❌ 꺼짐'}",
             "",
             "켜면 방에서 <code>50테더</code>·<code>100만원</code>·<code>249 usdt</code> 처럼 치면 "
             "소담이 USDT↔원 시세를 바로 알려줘요.",
             "<code>.시세</code>(현재가) · <code>.시세 50테더</code>(변환) 명령과 "
             "“소담아 500테더 얼마야”는 <b>이 설정과 무관하게 항상</b> 돼요.",
             "",
             "업비트 기준·참고용이에요."]
    rows = menu._toggle_rows(s, c.cid, ["rate_enabled"])
    rows.append(menu._back(c.cid))
    return Screen("\n".join(lines), menu._kb(rows))


menu.register_screen("rt", s_rt)
menu.register_hub(HubItem(61, "rt", lambda s: f"💱 시세 조회 {'✅' if s.get('rate_enabled') else '❌'}"))


# ── AI 도구 ───────────────────────────────────────────────
async def t_rate(ctx: tools.ToolCtx, a: dict) -> str:
    raw = str(a.get("amount", "")).replace(",", "").strip()
    try:
        amount = float(raw)
    except ValueError:
        return "amount(숫자)가 필요함."
    unit = str(a.get("unit", "usdt")).lower()
    direction = "k2u" if unit in ("krw", "원", "won") else "u2k"
    rate, stale = await rates.get_rate()
    if rate is None:
        return rates.FAIL
    return rates.answer(amount, direction, rate, stale)


tools.register_tool(
    tools.Tool("rate_convert",
               "USDT↔원 실시간 시세 변환 (업비트 기준). amount=금액, unit='usdt'(테더→원) 또는 'krw'(원→테더). "
               "'500테더 얼마' '백만원이면 몇 테더' 같은 환율 질문에.",
               {"amount": {"type": "number", "description": "금액"},
                "unit": {"type": "string", "enum": ["usdt", "krw"], "description": "usdt=테더 기준, krw=원 기준"}},
               ["amount"], t_rate, Role.MEMBER, where="any"),
    read_only=True)
