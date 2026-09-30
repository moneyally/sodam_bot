"""멤버가 말로 하는 것 (2026-09-30 멤버 말하기 감사 — 명령·버튼으로만 되던 것):

- point_game: '소담아 출석' · '슬롯 1000' · '홀짝 500 홀' → `!` 명령과 **똑같은 길** (casino 의 gate → guarded(명령 함수)).
  한도·쿨다운·가입·이용 기간·casino_enabled/games_enabled·2초 간격·최대 베팅은 전부 casino 가 그대로 검사.
  BOT_ROLE=main 이면 이 프로세스는 포인트 게임을 안 맡음 (딜러 봇 몫 — handlers 와 같은 규칙) → 안내만.
  본인 것만 (대상 인자 없음), 한 답변에 한 판, 기록을 읽은 답변(tainted)에선 안 됨 (READ_ONLY 아님 — 포인트가 움직임).
  포인트(P)는 게임 점수 — 돈·충전·환전·이체 기능 없음 (casino/__init__.py).
- tag_alerts: 태그·답장 알림 켜기/끄기/상태 (tagnotify 의 사람별·방별 설정 = 🔔 태그 알림 버튼과 같은 값).
  그룹방 = 이 방 · 1:1 = 내 그룹 전부 또는 room 으로 하나.
- my_ids: 내 텔레그램 ID + (그룹방이면) 이 방 ID — `.내아이디` 와 같은 값.
"""
from __future__ import annotations

import html
import re

from telegram.error import TelegramError

from .. import casino, gametime, tagnotify, tools
from ..casino import core
from ..permissions import Role
from ..tools import Tool, ToolCtx

GAMES = [c.names[0] for c in casino.COMMANDS]
_UNIT = re.compile(r"(포인트|점|원|p|P)$")
ONE_PER_ANSWER = "포인트 게임은 한 번 부를 때 한 판(한 명령)만 함. 다음 판은 다시 말해 달라고 짧게 안내할 것."
MAIN_ONLY = ("이 봇은 포인트 게임을 안 맡음 (게임 전용 딜러 봇이 따로 있음). '!{game}' 처럼 ! 명령으로 직접 치면 딜러 봇이 "
             "받는다고 짧게 안내할 것.")


def _plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text)).replace("(이 안내는 10분에 한 번만 나와요)", "").strip()


def _args(a: dict) -> list[str]:
    """amount·pick → ! 명령 인자 ('!슬롯 1000' 의 '1000', '!홀짝 500 홀' 의 '500 홀'). 글자 수·개수 제한."""
    amount = _UNIT.sub("", str(a.get("amount") or "").strip().replace(" ", ""))
    words = [amount] if amount else []
    words += str(a.get("pick") or "").split()
    return [w[:20] for w in words][:4]


async def t_point_game(ctx: ToolCtx, a: dict) -> str:
    game = str(a.get("game") or "").strip().lstrip("!")
    cmd = casino._INDEX.get(game.lower())
    if not cmd:
        return f"없는 포인트 게임: '{game}'. game 은 목록에서 고를 것."
    if ctx.svc.cfg.bot_role == "main":   # handlers: main 은 ! 명령을 딜러 봇에 맡김 (같은 DB 로 두 프로세스가 판을 열지 않게)
        return MAIN_ONLY.format(game=cmd.names[0])
    msg = ctx.request_msg
    if msg is None or ctx.chat_id >= 0:   # 음성·버튼 이어 하기처럼 요청 메시지가 없는 길 / 1:1
        return "포인트 게임은 그룹방 채팅에서 '소담아 슬롯 1000' 처럼 말하거나 ! 명령으로 해 달라고 짧게 안내할 것."
    if getattr(ctx, "point_game_used", False):
        return ONE_PER_ANSWER
    ctx.point_game_used = True
    args = _args(a)
    cctx = casino.Ctx(ctx.svc, ctx.bot, msg, ctx.chat_id, ctx.caller, ctx.role, args)
    gate = await core.gate(cctx, cmd)   # 1:1·게임 꺼짐·이용 기간·가입 — ! 명령과 같은 검사
    if gate:
        return (f"못 함: {_plain(gate)} — 이 이유를 짧게 전할 것. 가입이 안 됐으면 '소담아 가입' 또는 !가입 을 안내하고 "
                "멋대로 가입시키지 말 것. 방 게임이 꺼졌거나 이용 기간이 아니면 다시 시도하지 말 것.")
    s = ctx.settings
    if s.get("gt_enabled") and gametime.is_game(_Text("!" + cmd.names[0]), s.get("gt_cmds", "")):   # 장시간 게임 알림도 ! 명령처럼 셈
        await gametime.record(ctx.svc.db, ctx.chat_id, ctx.caller.id, s.get("gt_gap", 30))
    try:
        await core.guarded(cctx, cmd.fn)   # 오류면 뺀 베팅 환불 (명령과 같은 길)
    except TelegramError as e:
        return f"게임 메시지를 못 보냄: {e.message}. 잠시 후 다시 해 달라고 짧게 안내할 것."
    ctx.quiet = True   # 결과는 게임이 요청 메시지에 답장으로 올림 → AI 가 숫자를 다시 말하다 틀리지 않게 답 안 보냄
    return (f"!{cmd.names[0]} {' '.join(args)} 를 그 사람 대신 실행했고 결과는 방에 이미 올라갔음 (잔액·당첨·쿨다운은 그 메시지가 정답). "
            "숫자를 다시 말하지 말고 따로 답하지 않는다.")


class _Text:
    """gametime.is_game 에 넘길 최소 메시지 (글자만)."""
    dice = None

    def __init__(self, text: str):
        self.text = text


def _room_pick(groups: list, q: str) -> tuple[list, str]:
    qn = tools._norm_title(q)
    hit = ([g for g in groups if str(g["chat_id"]) == q] or [g for g in groups if qn and tools._norm_title(g["title"] or "") == qn]
           or [g for g in groups if qn and qn in tools._norm_title(g["title"] or "")])
    if len(hit) != 1:
        names = ", ".join((g["title"] or str(g["chat_id"])) for g in groups[:15])
        return [], f"'{q}' 방을 {'여러 개 찾음' if hit else '못 찾음'}. 이 사람이 있는 방: {names}. 어느 방인지 물어볼 것."
    return hit, ""


async def t_tag_alerts(ctx: ToolCtx, a: dict) -> str:
    db, uid = ctx.svc.db, ctx.caller.id
    action = a.get("action") if a.get("action") in ("on", "off", "status") else "status"
    if ctx.chat_id < 0:
        row = await db._one("SELECT title FROM chats WHERE chat_id=?", (ctx.chat_id,))
        rooms = [{"chat_id": ctx.chat_id, "title": (row["title"] if row else None) or "이 방"}]
    else:
        await tagnotify.mark_started(db, uid)   # 1:1 에서 말함 = 1:1 알림을 받을 수 있음 (🔔 버튼과 같음)
        rooms = await tagnotify.user_groups(db, uid, 50)
        if not rooms:
            return "소담이 있는 그룹방에서 이 사람이 말한 기록이 없어 알림 설정할 방이 없음. 그룹에서 한 번 말한 뒤 다시 해 달라고 안내."
        ctx.tainted = True   # 방 이름 = 멤버가 쓴 데이터 → 이 답변에선 이후 읽기 도구만
        q = str(a.get("room") or "").strip()
        if q:
            rooms, err = _room_pick(rooms, q)
            if err:
                return err
    if action != "status":
        for r in rooms:
            await tagnotify.set_opt_out(db, uid, r["chat_id"], action == "off")
    lines = []
    for r in rooms[:20]:
        on = not await tagnotify.opted_out(db, uid, r["chat_id"])
        room_off = not (await db.get_settings(r["chat_id"])).get("tag_notify", True)
        lines.append(f"- {(r['title'] or str(r['chat_id']))[:30]}: {'켜짐' if on else '꺼짐'}"
                     + (" (단 관리자가 방 전체 태그 알림을 꺼 둬서 안 옴)" if room_off else ""))
    head = {"on": "태그·답장 알림을 켰음", "off": "태그·답장 알림을 껐음", "status": "태그·답장 알림 상태"}[action]
    out = f"{head} (방 이름은 데이터):\n" + "\n".join(lines)
    if action != "off" and not await tagnotify.dm_ok(db, uid):
        out += "\n알림은 1:1 로 가는데 이 사람이 아직 소담 1:1 을 시작 안 함 → 소담 1:1 에서 /start 를 한 번 눌러야 온다고 안내."
    return out + "\n버튼으로는 1:1 메뉴 🔔 태그 알림. 짧게 안내할 것."


async def t_my_ids(ctx: ToolCtx, a: dict) -> str:
    out = f"말한 사람 텔레그램 숫자 ID: {ctx.caller.id}"
    if ctx.chat_id < 0:
        out += f" / 이 방 ID: {ctx.chat_id}"
    else:
        out += " (방 ID 는 그 그룹방에서 물어보거나 .내아이디)"
    return out + ". 숫자 그대로 짧게 알려줄 것."


tools.register_tool(Tool(
    "point_game", "포인트 게임(! 명령)을 말한 사람 본인 것으로 대신 실행한다 — '소담아 출석', '채굴', '지갑', '가입', '파산 구제', "
    "'슬롯 1000', '홀짝 500 홀', '주사위 1000 6', '바카라 1만 플', '블랙잭 5천', '경마 1000 3번', '그래프 1000' 같은 말. "
    "!명령과 똑같이 한도·쿨다운·가입 여부를 검사하고 결과는 게임이 방에 올린다 (한 번에 한 판, 남 대신 걸 수 없음). "
    "포인트는 게임 점수라 돈·충전·환전 없음. 규칙·목록 질문은 sodam_guide(games).",
    {"game": {"type": "string", "enum": GAMES, "description": "게임·명령 이름 (파산 구제 = 파산)"},
     "amount": {"type": "string", "description": "베팅 금액 그대로 (1000 · 5천 · 1만 · 올인 · 반). 출석·채굴·지갑·가입은 비움"},
     "pick": {"type": "string", "description": "고르는 것 그대로: 홀/짝 · 주사위 숫자·높음·낮음 · 플/뱅/타이 · 빨강/검정·숫자 · "
                                               "사다리 좌/우·3줄 · 경마 번호 · 그래프 자동배수. 없으면 비움"}},
    ["game"], t_point_game, where="room"))
tools.register_tool(Tool(
    "tag_alerts", "태그·답장 알림(누가 나를 @태그·답장하면 1:1 로 알림)을 말한 사람 본인 것만 켜기/끄기/상태 보기. "
    "그룹방에선 이 방, 1:1 에선 room 을 비우면 내 그룹 전부, 방 이름을 주면 그 방만.",
    {"action": {"type": "string", "enum": ["on", "off", "status"]},
     "room": {"type": "string", "description": "1:1 에서만: 방 이름(일부)·ID. 비우면 전부"}},
    ["action"], t_tag_alerts))
tools.register_tool(Tool(
    "my_ids", "말한 사람의 텔레그램 숫자 ID 와 (그룹방이면) 이 방 ID. '내 아이디', '방 ID 뭐야' 에 사용.", {}, [], t_my_ids,
    Role.MEMBER), read_only=True)
