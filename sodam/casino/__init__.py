"""포인트 카지노 (! 명령). 진짜 돈과는 절대 연결하지 않는다.

- 포인트(P)는 가입·채굴·출석·게임으로만 생긴다. 충전·환전·선물(이체) 기능 없음 (게임산업법 32조: 환전 금지).
- 방마다 따로 (members.points). 게임이 꺼진 방·이용 기간 끝난 방에선 안 됨.
- 새 게임 = 이 폴더에 모듈 추가 후 `register(("이름", "별칭"), fn, help=..., group=...)`.

흐름: handlers.on_group_message → 텍스트가 '!' 로 시작 → casino.dispatch
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

from telegram.error import TelegramError

if TYPE_CHECKING:
    from telegram import Bot, Message, User

    from ..permissions import Role
    from ..services import Services

log = logging.getLogger(__name__)


@dataclass
class Ctx:
    svc: Services
    bot: Bot
    msg: Message
    chat_id: int
    user: User
    role: Role
    args: list[str]
    staked: int = 0              # 이 명령·버튼에서 차감했지만 아직 정산 안 된 베팅 (core.guarded 가 오류 때 환불)
    game: str = ""

    async def reply(self, text: str, **kw):
        return await self.msg.reply_text(text, parse_mode="HTML", **kw)


@dataclass(frozen=True)
class CasinoCmd:
    names: tuple[str, ...]
    fn: Callable[[Ctx], Awaitable[None]]
    help: str = ""
    usage: str = ""
    group: str = "게임"
    needs_account: bool = True   # !가입 안 한 사람은 안내만


COMMANDS: list[CasinoCmd] = []
SHUTDOWN_HOOKS: list = []   # async fn(svc) → 봇 종료 때 진행 중인 판 환불 등
_INDEX: dict[str, CasinoCmd] = {}


def register(names: tuple[str, ...], fn, *, help: str = "", usage: str = "", group: str = "게임",
             needs_account: bool = True) -> None:
    cmd = CasinoCmd(names, fn, help, usage, group, needs_account)
    for n in names:
        if n in _INDEX and _INDEX[n] is not cmd:
            raise ValueError(f"카지노 명령 이름 겹침: {n}")
        _INDEX[n] = cmd
    COMMANDS.append(cmd)


def parse(text: str) -> tuple[CasinoCmd, list[str]] | None:
    if not text.startswith("!") or len(text) < 2:
        return None
    parts = text[1:].split()
    if not parts:                                 # '! ' 처럼 이름 없이 공백만
        return None
    head, args = parts[0], parts[1:]
    cmd = _INDEX.get(head.lower())
    return (cmd, args) if cmd else None


async def dispatch(svc, bot, msg, chat_id: int, user, role, text: str) -> bool:
    """'!' 명령이면 처리하고 True. 게임이 꺼진 방이면 조용히 안내."""
    parsed = parse(text)
    if not parsed:
        return False
    cmd, args = parsed
    from . import core
    ctx = Ctx(svc, bot, msg, chat_id, user, role, args)
    gate = await core.gate(ctx, cmd)
    if gate:
        if core.room_notice_due(chat_id, gate):
            await ctx.reply(gate)
        else:                                   # 같은 방 안내는 10분에 한 번: 그 사이 명령은 조용히 지우기만
            try:
                await msg.delete()
            except TelegramError:
                pass
        return True
    try:
        await core.guarded(ctx, cmd.fn)
    except TelegramError as e:
        log.warning("casino %s failed: %s", cmd.names[0], e)
    return True


# ── 버튼 콜백 (접두어 cs:<게임>:...) ─────────────────────
CALLBACKS: dict[str, Callable[..., Awaitable[None]]] = {}


def register_callback(game: str, fn) -> None:
    """버튼 콜백 등록: 'cs:<game>:...' 을 누르면 fn(svc, bot, q, 나머지 parts). fn 은 q.answer() 를 한 번 부른다."""
    if game in CALLBACKS and CALLBACKS[game] is not fn:
        raise ValueError(f"카지노 콜백 이름 겹침: {game}")
    CALLBACKS[game] = fn


async def on_callback(svc, bot, q, parts: list[str]) -> None:
    fn = CALLBACKS.get(parts[0]) if parts else None
    if not fn:
        await q.answer("지난 버튼이에요.")
        return
    try:
        await fn(svc, bot, q, parts[1:])
    except TelegramError as e:
        log.warning("casino callback %s failed: %s", parts[0], e)


async def startup(svc) -> int:
    """봇 시작 때 (게임을 맡는 프로세스만, 판이 열리기 전): 지난 실행에서 정산 못 한 베팅 환불 (강제 종료 대비)."""
    from .core import recover_open
    from .multi import recover_stale
    return await recover_open(svc.db) + await recover_stale(svc.db)   # 옛 버전이 남긴 멀티 판 표까지


async def shutdown(svc) -> None:
    for fn in SHUTDOWN_HOOKS:
        try:
            n = await fn(svc)
            if n:
                log.info("카지노 종료 정리 %s: %s건 환불", getattr(fn, "__name__", fn), n)
        except Exception:
            log.exception("casino shutdown hook failed")


def help_text() -> str:
    groups: dict[str, list[str]] = {}
    for c in COMMANDS:
        groups.setdefault(c.group, []).append(f"<code>!{c.names[0]}{(' ' + c.usage) if c.usage else ''}</code> {c.help}")
    parts = ["🎰 <b>소담 포인트 게임</b> (P는 게임 포인트 · 돈으로 바꿀 수 없어요)"]
    for g, lines in groups.items():
        parts.append(f"\n<b>[{g}]</b>\n" + "\n".join(lines))
    return "\n".join(parts)


def _load() -> None:
    for m in pkgutil.iter_modules(__path__):
        importlib.import_module(f"{__name__}.{m.name}")


_load()
if "도움" in _INDEX:  # '.도움말' 과 헷갈려 '!도움말' 로 치는 경우도 게임 목록
    _INDEX.setdefault("도움말", _INDEX["도움"])
