"""AI가 쓸 수 있는 도구.

- 호출한 사람(caller)과 권한(role)은 코드가 넣는다. AI 입력으로 받지 않는다.
- 권한별로 AI에게 보여줄 도구 목록 자체가 다르고, 실행 직전에 한 번 더 검사한다.
"""
from __future__ import annotations

import html
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, User
from telegram.error import TelegramError

from . import knowledge, memory, stats  # memory: AI 설정 키도 여기서 등록됨 (change_setting 목록에 들어가게)
from .permissions import Role
from .services import PendingAction, Services
from .settings import DEFAULTS, LABELS, coerce, render
from .sports import SPORTS_KO, SportsError
from .styles import STYLES, resolve_style
from .util import display_name, esc, fmt_time, human_minutes, mention, period_since

log = logging.getLogger(__name__)


@dataclass
class ToolCtx:
    svc: Services
    bot: Bot
    chat_id: int
    caller: User
    role: Role
    settings: dict
    mentions: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class Tool:
    name: str
    description: str
    params: dict
    required: list[str]
    fn: Callable[[ToolCtx, dict], Awaitable[str]]
    min_role: Role = Role.MEMBER
    setting: str | None = None  # 이 설정이 꺼져 있으면 도구를 숨김

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": self.params,
                               "required": self.required, "additionalProperties": False},
            },
        }


def _plain(text: str) -> str:
    return html.unescape(text)


PERIOD = {"type": "string", "enum": ["오늘", "어제", "주간", "월간", "전체"]}


async def _resolve(ctx: ToolCtx, name: str, *, for_sanction: bool = False):
    """이름/@username/ID → 방 멤버 1명. 실패하면 에러 문자열."""
    rows = await ctx.svc.db.find_members(ctx.chat_id, name)
    if not rows and not for_sanction:  # 인사·조회는 '우주대표님' 처럼 호칭 붙은 부분 이름으로도 (제재는 정확한 이름만)
        rows = await _fuzzy_members(ctx, name)
    if not rows:
        return None, f"'{name}' 멤버를 찾을 수 없어요. @username 이나 정확한 이름이 필요해요."
    if len(rows) > 1:
        names = ", ".join(f"{display_name(r['first_name'], r['last_name'], r['username'])}({r['user_id']})" for r in rows[:5])
        return None, f"같은 이름이 여러 명이에요: {names}. ID로 다시 지정해주세요."
    row = rows[0]
    if for_sanction and await ctx.svc.perms.protected(ctx.bot, ctx.chat_id, row["user_id"]):
        return None, "관리자나 봇은 제재할 수 없어요."
    return row, None


_HONORIFICS = ("대표님", "사장님", "실장님", "이사님", "회장님", "팀장님", "부장님", "형님", "누님", "선생님",
               "대표", "사장", "실장", "이사", "회장", "팀장", "부장", "님", "씨", "형", "누나", "언니", "오빠")


async def _fuzzy_members(ctx: ToolCtx, name: str):
    """'우주대표님' → '우주' 를 이름·@아이디에 포함한 이 방 멤버. 핵심이 2글자 미만이면 안 찾음."""
    core = name.strip().lstrip("@")
    for h in _HONORIFICS:
        if core.endswith(h) and len(core) > len(h):
            core = core[: -len(h)].strip()
            break
    if len(core) < 2:
        return []
    like = f"%{core}%"
    return await ctx.svc.db._all(
        "SELECT u.* FROM users u JOIN members m ON m.user_id=u.user_id WHERE m.chat_id=? AND u.is_bot=0 AND "
        "(u.first_name LIKE ? OR COALESCE(u.last_name,'') LIKE ? OR COALESCE(u.username,'') LIKE ?) LIMIT 6",
        (ctx.chat_id, like, like, like))


def _row_name(row) -> str:
    return display_name(row["first_name"], row["last_name"], row["username"])


# ── 멤버 도구 ─────────────────────────────────────────────
async def t_my_requests(ctx: ToolCtx, a: dict) -> str:
    since, label = period_since(a.get("period", "오늘"), ctx.svc.cfg.tz)
    rows = await ctx.svc.db.user_requests(ctx.chat_id, ctx.caller.id, since)
    if not rows:
        return f"{label} 이 사람이 봇에게 요청한 기록이 없음."
    lines = [f"{label} 요청 {len(rows)}건 (이 개수가 전부이며 더 만들어내지 말 것):"]
    lines += [f"{i + 1}. [{fmt_time(r['ts'], ctx.svc.cfg.tz)}] {r['text'][:120]}" for i, r in enumerate(rows)]
    return "\n".join(lines)


async def t_chat_stats(ctx: ToolCtx, a: dict) -> str:
    period = a.get("period", "오늘")
    summary = await stats.summary_text(ctx.svc.db, ctx.chat_id, ctx.svc.cfg.tz, period)
    ranking = await stats.ranking_text(ctx.svc.db, ctx.chat_id, ctx.svc.cfg.tz, period, 5)
    return _plain(summary + "\n" + ranking)


async def t_search_chat(ctx: ToolCtx, a: dict) -> str:
    keyword = str(a.get("keyword", "")).strip()[:30]
    if len(keyword) < 2:
        return "검색어는 2글자 이상이어야 함."
    days = max(1, min(int(a.get("days", 7)), 60))
    return _plain(await stats.search_text(ctx.svc.db, ctx.chat_id, ctx.svc.cfg.tz, keyword, days, 10))


async def t_read_chat(ctx: ToolCtx, a: dict) -> str:
    hours = max(1, min(int(a.get("hours", 3)), 24))
    since = int(datetime.now(ctx.svc.cfg.tz).timestamp()) - hours * 3600
    rows = await ctx.svc.db.recent_messages(ctx.chat_id, limit=150, since=since)
    lines = []
    for r in rows:
        who = "봇" if r["is_bot"] else f"{r['first_name'] or r['username'] or '?'}({r['user_id']})"
        lines.append(f"[{fmt_time(r['ts'], ctx.svc.cfg.tz, '%H:%M')}] {who}: {r['text'][:150]}")
    text = "\n".join(lines) or "해당 시간에 대화 없음."
    return text[-6000:]


async def t_member_info(ctx: ToolCtx, a: dict) -> str:
    row, err = await _resolve(ctx, str(a.get("name", "")))
    if err:
        return err
    m = await ctx.svc.db.get_member(ctx.chat_id, row["user_id"])
    count = await ctx.svc.db.user_message_count(ctx.chat_id, row["user_id"])
    tz = ctx.svc.cfg.tz
    parts = [f"이름: {_row_name(row)}", f"메시지 수: {count}", f"포인트: {m['points']}"]
    if m["joined_at"]:
        parts.append(f"입장: {fmt_time(m['joined_at'], tz, '%Y-%m-%d')}")
    if m["last_seen"]:
        parts.append(f"마지막 활동: {fmt_time(m['last_seen'], tz)}")
    if ctx.role >= Role.ADMIN:
        parts.append(f"경고: {await ctx.svc.db.warning_count(ctx.chat_id, row['user_id'])}회")
    return " / ".join(parts)


async def t_room_rules(ctx: ToolCtx, a: dict) -> str:
    return ctx.settings["rules"] or "등록된 방 규칙이 없음."


async def t_web_search(ctx: ToolCtx, a: dict) -> str:
    query = str(a.get("query", "")).strip()
    if not query:
        return "검색어가 비어 있음."
    day = datetime.now(ctx.svc.cfg.tz).strftime("%Y-%m-%d")
    used = await ctx.svc.db.bump(day, ctx.chat_id, "web_search")
    if used > ctx.settings["web_search_daily"]:
        return "오늘 이 방의 웹검색 한도를 다 썼음. 내일 다시 가능하다고 안내할 것."
    result = await ctx.svc.llm.web_search(query)
    return result or "검색 결과 없음."


async def t_sports(ctx: ToolCtx, a: dict) -> str:
    action = a.get("action", "today")
    try:
        if action == "today":
            return _plain(await ctx.svc.sports.today_text(a.get("sport") or "축구"))
        team = str(a.get("team", "")).strip()
        if not team:
            return "팀 이름(영어)이 필요함."
        return _plain(await ctx.svc.sports.team_text(team, "next" if action == "team_next" else "last"))
    except SportsError as e:
        return str(e)


NOTE_KEYS = ["호칭", "업종", "관심사", "소개"]


async def t_save_my_note(ctx: ToolCtx, a: dict) -> str:
    key = a.get("key")
    if key not in NOTE_KEYS:
        return "저장할 수 없는 항목."
    value = str(a.get("value", "")).replace("\n", " ").strip()[:50]
    await ctx.svc.db.set_member_note(ctx.chat_id, ctx.caller.id, key, value)
    return f"저장함: {key} = {value or '(삭제)'}"


async def t_forget_my_memory(ctx: ToolCtx, a: dict) -> str:
    """본인 기억만 지운다 (다른 사람 것은 지울 수 없음)."""
    what = str(a.get("what", "")).strip()[:30]
    n = await memory.clear_facts(ctx.svc.db, ctx.chat_id, ctx.caller.id, what)
    if not what:
        for key in NOTE_KEYS:
            await ctx.svc.db.set_member_note(ctx.chat_id, ctx.caller.id, key, "")
        n += 1
    # 지운 뒤 예전 메시지에서 다시 뽑지 않게 정리 기준 시각을 지금으로
    await memory.mark_done(ctx.svc.db, ctx.chat_id, ctx.caller.id)
    if not n:
        return f"'{what}' 에 해당하는 기억이 없음."
    return ("이 사람에 대한 기억(메모 포함)을 전부 지웠음." if not what else f"'{what}' 관련 기억 {n}개를 지웠음.") + \
        " 지웠다고 짧게 안내할 것."


async def t_set_my_style(ctx: ToolCtx, a: dict) -> str:
    style = resolve_style(str(a.get("style", "")))
    if not style:
        return "없는 말투."
    await ctx.svc.db.set_member_style(ctx.chat_id, ctx.caller.id, style)
    return f"이 사람의 말투를 '{STYLES[style].label}'(으)로 바꿈. 다음 답변부터 적용."


async def t_greet(ctx: ToolCtx, a: dict) -> str:
    names = [str(n) for n in (a.get("names") or [])][:10]
    found, missing, new = [], [], []
    day_ago = int(datetime.now().timestamp()) - 86400
    for n in names:
        row, err = await _resolve(ctx, n)
        if err:
            missing.append(n)
            continue
        ctx.mentions.append((row["user_id"], _row_name(row)))
        found.append(_row_name(row))
        m = await ctx.svc.db.get_member(ctx.chat_id, row["user_id"])
        if m and m["joined_at"] and m["joined_at"] > day_ago:
            new.append(_row_name(row))
    old = [f for f in found if f not in new]
    result = f"인사 대상 확인: {', '.join(found) or '없음'}. 답변 맨 앞에 멘션이 자동으로 붙으니 이름은 다시 쓰지 말고 인사말만 쓸 것."
    if new:
        result += f" 오늘 새로 들어온 사람: {', '.join(new)} → 환영 인사."
    if old:
        result += f" 원래 있던 멤버: {', '.join(old)} → '환영' 말고 반가운 안부 인사 (예: 대표님 반갑습니다, 오늘도 좋은 하루 보내세요)."
    if missing:
        result += f" 못 찾은 이름: {', '.join(missing)}"
    return result


async def t_start_game(ctx: ToolCtx, a: dict) -> str:
    return await ctx.svc.games.start(ctx.bot, ctx.chat_id, ctx.caller.id, str(a.get("game", "")))


async def t_search_knowledge(ctx: ToolCtx, a: dict) -> str:
    query = str(a.get("query", "")).strip()[:200]
    if not query:
        return "검색어가 비어 있음."
    return knowledge.format_results(await knowledge.search(ctx.svc.db, ctx.chat_id, query))


REPORTS_PER_DAY = 5


async def t_report_to_admin(ctx: ToolCtx, a: dict) -> str:
    message = str(a.get("message", "")).strip()[:500]
    if not message:
        return "전달할 내용이 비어 있음."
    day = datetime.now(ctx.svc.cfg.tz).strftime("%Y-%m-%d")
    used = await ctx.svc.db.bump(day, ctx.caller.id, "admin_report")
    if used > REPORTS_PER_DAY:
        return f"이 사람은 오늘 관리자 전달을 {REPORTS_PER_DAY}번 다 썼음. 내일 다시 가능하다고 안내할 것."
    where = "1:1 채팅" if ctx.chat_id > 0 else f"방 {ctx.chat_id}"
    await ctx.svc.mod.report(
        ctx.bot,
        f"[멤버 전달] {where} / {mention(ctx.caller.id, display_name(ctx.caller.first_name, ctx.caller.last_name, ctx.caller.username))}"
        f"(<code>{ctx.caller.id}</code>)\n{esc(message)}")
    return "관리자 개인 텔레그램으로 전달함. 전달했다고 짧게 안내할 것."


async def t_points_ranking(ctx: ToolCtx, a: dict) -> str:
    rows = await ctx.svc.db.top_points(ctx.chat_id, 10)
    if not rows:
        return "아직 포인트를 받은 사람이 없음."
    return "\n".join(f"{i + 1}. {display_name(r['first_name'], None, r['username'])} {r['points']}점"
                     for i, r in enumerate(rows))


# ── 관리자 도구 ───────────────────────────────────────────
async def t_warn(ctx: ToolCtx, a: dict) -> str:
    row, err = await _resolve(ctx, str(a.get("name", "")), for_sanction=True)
    if err:
        return err
    text = await ctx.svc.mod.warn(ctx.bot, ctx.chat_id, row["user_id"], _row_name(row),
                                  ctx.caller.id, str(a.get("reason", "관리자 판단"))[:100])
    await ctx.bot.send_message(ctx.chat_id, text, parse_mode="HTML")
    return "경고 처리 완료 (안내 메시지는 이미 보냄)."


async def t_mute(ctx: ToolCtx, a: dict) -> str:
    row, err = await _resolve(ctx, str(a.get("name", "")), for_sanction=True)
    if err:
        return err
    minutes = max(1, min(int(a.get("minutes", 30)), 7 * 1440))
    try:
        await ctx.svc.mod.mute(ctx.bot, ctx.chat_id, row["user_id"], minutes, ctx.caller.id,
                               str(a.get("reason", "관리자 판단"))[:100])
    except TelegramError as e:
        return f"실패: {e.message}"
    return f"{_row_name(row)} {human_minutes(minutes)} 채팅 금지 완료."


async def t_unmute(ctx: ToolCtx, a: dict) -> str:
    row, err = await _resolve(ctx, str(a.get("name", "")))
    if err:
        return err
    try:
        await ctx.svc.mod.unmute(ctx.bot, ctx.chat_id, row["user_id"], ctx.caller.id)
    except TelegramError as e:
        return f"실패: {e.message}"
    return f"{_row_name(row)} 채팅 금지 해제 완료."


async def t_ban(ctx: ToolCtx, a: dict) -> str:
    row, err = await _resolve(ctx, str(a.get("name", "")), for_sanction=True)
    if err:
        return err
    reason = str(a.get("reason", "관리자 판단"))[:100]
    key = ctx.svc.add_pending(PendingAction(ctx.chat_id, "ban", row["user_id"], _row_name(row), reason, ctx.caller.id))
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 내보내기", callback_data=f"act:{key}:y"),
        InlineKeyboardButton("❌ 취소", callback_data=f"act:{key}:n"),
    ]])
    await ctx.bot.send_message(
        ctx.chat_id,
        f"🚫 {mention(row['user_id'], _row_name(row))}님을 내보낼까요?\n사유: {esc(reason)}\n(관리자만 누를 수 있고 2분 뒤 만료돼요)",
        parse_mode="HTML", reply_markup=kb)
    return "확인 버튼을 보냈음. 관리자가 눌러야 실행된다고 짧게 안내할 것."


async def t_change_setting(ctx: ToolCtx, a: dict) -> str:
    key, value = str(a.get("key", "")), str(a.get("value", ""))
    try:
        parsed = coerce(key, value)
    except ValueError as e:
        return f"실패: {e}"
    await ctx.svc.db.set_setting(ctx.chat_id, key, parsed)
    await ctx.svc.db.log_mod(ctx.chat_id, ctx.caller.id, None, "setting", f"{key}={parsed}")
    return f"설정 변경: {LABELS.get(key, key)} = {render(key, parsed)}"


TOOLS: list[Tool] = [
    Tool("get_my_requests", "지금 말한 사람이 봇에게 요청했던 기록을 조회한다. '내가 뭐 요청했지' 같은 질문에 반드시 사용.",
         {"period": PERIOD}, [], t_my_requests),
    Tool("chat_stats", "방 채팅 통계와 수다 랭킹을 조회한다.", {"period": PERIOD}, [], t_chat_stats),
    Tool("search_chat", "방 대화 기록에서 키워드를 검색한다.",
         {"keyword": {"type": "string"}, "days": {"type": "integer", "description": "최근 며칠 (1~60)"}},
         ["keyword"], t_search_chat),
    Tool("read_chat", "최근 N시간 방 대화를 읽는다. '요약해줘', '무슨 얘기 했어' 같은 요청에 사용.",
         {"hours": {"type": "integer", "description": "1~24"}}, [], t_read_chat),
    Tool("member_info", "방 멤버 정보(메시지 수, 입장일, 포인트)를 조회한다.",
         {"name": {"type": "string", "description": "@username, 이름, 또는 숫자 ID"}}, ["name"], t_member_info),
    Tool("room_rules", "이 방의 규칙/공지를 확인한다.", {}, [], t_room_rules),
    Tool("web_search", "최신 뉴스·사실 확인이 필요할 때 웹을 검색한다. 방 기록 질문에는 쓰지 않는다.",
         {"query": {"type": "string"}}, ["query"], t_web_search),
    Tool("sports", "스포츠 경기 일정/결과를 조회한다 (배당·베팅 정보 없음). 팀 이름은 영어로.",
         {"action": {"type": "string", "enum": ["today", "team_next", "team_last"]},
          "sport": {"type": "string", "enum": list(SPORTS_KO)},
          "team": {"type": "string", "description": "영어 팀명 예: Tottenham, LA Dodgers"}},
         ["action"], t_sports, setting="sports_enabled"),
    Tool("save_my_note", "말한 사람 본인의 정보(호칭, 업종, 관심사, 소개)를 기억한다. 다른 사람 정보는 저장하지 않는다.",
         {"key": {"type": "string", "enum": NOTE_KEYS}, "value": {"type": "string", "description": "50자 이내, 빈 값이면 삭제"}},
         ["key", "value"], t_save_my_note),
    Tool("forget_my_memory", "말한 사람 본인에 대해 소담이 기억하는 내용을 지운다. '내 기억 지워줘', '그건 잊어줘' 같은 요청에 사용.",
         {"what": {"type": "string", "description": "지울 기억의 핵심 단어. 비우면 전부 지움"}}, [], t_forget_my_memory),
    Tool("set_my_style", "말한 사람 본인에게 쓸 봇 말투를 바꾼다.",
         {"style": {"type": "string", "enum": [s.label for s in STYLES.values()]}}, ["style"], t_set_my_style),
    Tool("greet_members", "특정 멤버들에게 인사할 때 사용. 멘션을 붙여준다.",
         {"names": {"type": "array", "items": {"type": "string"}, "description": "@username 또는 이름"}},
         ["names"], t_greet),
    Tool("start_game", "방에서 봇과 하는 끝말잇기를 시작한다. 포인트 게임(홀짝·바카라 등)은 도구가 아니라 멤버가 직접 ! 명령으로 한다.",
         {"game": {"type": "string", "enum": ["끝말잇기"]}},
         ["game"], t_start_game, setting="games_enabled"),
    Tool("points_ranking", "게임 포인트 랭킹을 조회한다.", {}, [], t_points_ranking),
    Tool("search_knowledge", "관리자가 등록한 방 자료(규칙·공지·상품·가격·운영 안내 문서)에서 관련 내용을 찾는다. "
         "이 방에 관한 사실 질문엔 먼저 이걸 쓴다.",
         {"query": {"type": "string", "description": "찾을 내용 (핵심 단어 위주)"}}, ["query"], t_search_knowledge),
    Tool("report_to_admin", "멤버가 관리자에게 전하고 싶은 말·신고·건의를 관리자 개인 텔레그램으로 전달한다 (1인 하루 5회).",
         {"message": {"type": "string", "description": "전달할 내용 요약 (500자 이내)"}}, ["message"], t_report_to_admin),
    # 관리자 전용
    Tool("warn_member", "[관리자] 멤버에게 경고를 준다. 누적되면 자동 뮤트/밴.",
         {"name": {"type": "string"}, "reason": {"type": "string"}}, ["name", "reason"], t_warn, Role.ADMIN),
    Tool("mute_member", "[관리자] 멤버를 일정 시간 채팅 금지한다.",
         {"name": {"type": "string"}, "minutes": {"type": "integer", "description": "1~10080"},
          "reason": {"type": "string"}}, ["name", "minutes"], t_mute, Role.ADMIN),
    Tool("unmute_member", "[관리자] 채팅 금지를 해제한다.", {"name": {"type": "string"}}, ["name"], t_unmute, Role.ADMIN),
    Tool("ban_member", "[관리자] 멤버를 내보낸다. 실제 실행 전 확인 버튼이 뜬다.",
         {"name": {"type": "string"}, "reason": {"type": "string"}}, ["name", "reason"], t_ban, Role.ADMIN),
    Tool("change_setting", "[관리자] 방 설정을 바꾼다.",
         {"key": {"type": "string", "enum": list(DEFAULTS)}, "value": {"type": "string"}},
         ["key", "value"], t_change_setting, Role.ADMIN),
]
_BY_NAME = {t.name: t for t in TOOLS}


def available(role: Role, settings: dict) -> list[Tool]:
    return [t for t in TOOLS if role >= t.min_role and (not t.setting or settings.get(t.setting))]


async def execute(name: str, raw_args: str, ctx: ToolCtx) -> str:
    tool = _BY_NAME.get(name)
    # 2중 검사: 목록에서 숨겼더라도 실행 직전에 다시 확인
    if not tool or tool not in available(ctx.role, ctx.settings):
        return "이 도구는 지금 사용할 수 없음 (권한 없음)."
    try:
        args = json.loads(raw_args or "{}")
        if not isinstance(args, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        return "도구 입력 형식 오류."
    try:
        return await tool.fn(ctx, args)
    except (TypeError, ValueError) as e:
        return f"도구 입력 오류: {e}"
    except Exception:  # 도구 하나 실패로 답변 전체가 죽지 않게
        log.exception("tool %s failed", name)
        return "도구 실행 중 오류가 났음. 잠시 후 다시 해달라고 안내할 것."
