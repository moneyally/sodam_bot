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


# ── AI 도구: 게임 상태 보기·끝내기·다시 시작 ('고장났어? 고쳐줘' 에 답할 수 있게) ──────────
from ..permissions import Role  # noqa: E402
from ..tools import Tool, ToolCtx, register_tool  # noqa: E402


async def t_game_control(ctx: ToolCtx, a: dict) -> str:
    mgr, cid = ctx.svc.games, ctx.chat_id
    action = str(a.get("action", "status"))
    game = mgr.active.get(cid)
    if action == "status":
        return mgr.status(cid) + " — 상태를 사실대로 짧게 알려줄 것. 멈춘 것처럼 보이면 이유(시간 초과·규칙에 안 맞는 말)를 설명."
    can_stop = bool(game) and (game.starter_id == ctx.caller.id or ctx.role >= Role.ADMIN)
    if action == "stop":
        if not game or game.finished:
            return "진행 중인 게임이 없음. " + mgr.status(cid)
        if not can_stop:
            return "게임을 시작한 사람이나 관리자만 끝낼 수 있음 (그대로 안내)."
        await mgr.stop(cid)
        ctx.quiet = True
        return "게임을 끝냈음 (종료 안내는 게임이 이미 올림)."
    if action == "restart":
        if game and not game.finished:
            if not can_stop:
                return "지금 게임이 진행 중이고, 시작한 사람이나 관리자만 끝내고 다시 시작할 수 있음. " + game.status()
            await mgr.stop(cid)
        _, key, _ = mgr.recent.get(cid, (0.0, "", ""))
        key = str(a.get("game") or key or "끝말잇기")
        result = await mgr.start(ctx.bot, cid, ctx.caller.id, key)
        if result.endswith("시작했어요!"):
            ctx.quiet = True
            return result + " 새 게임 안내는 이미 방에 올라갔으니 따로 답하지 않는다."
        return result
    return "action 은 status / stop / restart."


register_tool(Tool(
    "game_control",
    "방 끝말잇기 게임 상태 확인·끝내기·다시 시작. '게임 멈췄어?' '고장났어?' '왜 안 받아줘?' → status 로 먼저 확인하고 이유를 설명, "
    "'게임 다시 해' '고쳐줘'(멈춘 게임) → restart, '게임 그만' → stop (시작한 사람·관리자만).",
    {"action": {"type": "string", "enum": ["status", "stop", "restart"]},
     "game": {"type": "string", "enum": ["끝말잇기", "끝말잇기 차례"], "description": "restart 때 종류 (없으면 방금 하던 것)"}},
    ["action"], t_game_control, setting="games_enabled", where="room"))
