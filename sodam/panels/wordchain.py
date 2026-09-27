"""🧩 기능 화면에 끝말잇기 설정: AI 선수 켜기/끄기(wc_ai) · 소담이 난이도(wc_level). 게임은 sodam/games.py · sodam/wordbot.py."""
from __future__ import annotations

from .. import games, menu  # noqa: F401  (games: 설정 키 등록)
from ..menu import FEATURE_TOGGLES, PanelCtx

LEVELS = [("easy", "🟢 쉬움"), ("normal", "🟡 보통"), ("hard", "🔴 어려움")]
menu.register_toggle("wc_ai", "f")
if "wc_ai" not in FEATURE_TOGGLES:
    FEATURE_TOGGLES.append("wc_ai")
menu.register_preset("wc_level", LEVELS, "f")


async def _rows(c: PanelCtx) -> list:
    s = await c.svc.db.get_settings(c.cid)
    return [menu._preset_row(s, c.cid, "wc_level")]


menu.register_screen_extra("f", _rows)
