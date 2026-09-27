"""대표님들과 봇이 하는 말 게임 (끝말잇기). 포인트 걸고 하는 게임은 sodam/casino/ (! 명령).

원칙: 정답·점수 판정은 코드가 한다. AI는 문제를 만들거나 예/아니요 판정만 한다.
포인트는 순위용이며 현금·코인으로 바꾸는 기능은 넣지 않는다 (넣는 순간 도박 규제 대상).
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING

from openai import OpenAIError
from telegram import Bot, CallbackQuery, Message
from telegram.error import TelegramError

from .casino.core import credit
from .llm import BudgetExceeded
from .util import esc, mention, user_name

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)

# ── 한글 도우미 ───────────────────────────────────────────
def is_hangul_word(word: str) -> bool:
    return bool(word) and all("가" <= c <= "힣" for c in word)


def dueum(ch: str) -> str:
    """두음법칙: 력→역, 녀→여, 라→나 ..."""
    if not "가" <= ch <= "힣":
        return ch
    code = ord(ch) - 0xAC00
    cho, jung, jong = code // 588, (code % 588) // 28, code % 28
    if cho == 5:  # ㄹ
        cho = 11 if jung in (2, 6, 7, 12, 17, 20) else 2
    elif cho == 2 and jung in (6, 12, 17, 20):  # ㄴ + ㅕㅛㅠㅣ
        cho = 11
    else:
        return ch
    return chr(0xAC00 + cho * 588 + jung * 28 + jong)


def starts_for(word: str) -> set[str]:
    last = word[-1]
    return {last, dueum(last)}


def norm(text: str) -> str:
    return "".join(text.split()).lower()


class GameSetupError(Exception):
    pass


# ── 공통 ──────────────────────────────────────────────────
class Game:
    title = ""
    instant = False  # 밸런스게임처럼 한 번 보내고 끝나는 게임

    def __init__(self, mgr: GameManager, bot: Bot, chat_id: int, starter_id: int):
        self.mgr = mgr
        self.svc = mgr.svc
        self.bot = bot
        self.chat_id = chat_id
        self.starter_id = starter_id
        self.finished = False
        self.scores: dict[int, list] = {}  # user_id -> [이름, 점수]
        self._timer: asyncio.Task | None = None

    async def say(self, text: str, **kw) -> Message:
        return await self.bot.send_message(self.chat_id, text, parse_mode="HTML", **kw)

    def set_timer(self, seconds: float, fn) -> None:
        self.cancel_timer()
        self._timer = asyncio.create_task(self._run_timer(seconds, fn))

    def cancel_timer(self) -> None:
        if self._timer and self._timer is not asyncio.current_task() and not self._timer.done():
            self._timer.cancel()

    async def _run_timer(self, seconds: float, fn) -> None:
        await asyncio.sleep(seconds)
        if self.finished:
            return
        try:
            await fn()
        except Exception:
            log.exception("game timer failed")
            await self.finish("게임 진행 중 문제가 생겨서 종료할게요 🙏")

    async def award(self, user, points: int) -> None:
        entry = self.scores.setdefault(user.id, [user_name(user), 0])
        entry[1] += points
        await credit(self.svc.db, self.chat_id, user.id, points, f"game:{self.title}")   # 원장(casino_ledger)에 남김

    def scoreboard(self) -> str:
        if not self.scores:
            return ""
        ranked = sorted(self.scores.items(), key=lambda kv: -kv[1][1])
        return "\n🏆 " + ", ".join(f"{esc(n)} {p}점" for _, (n, p) in ranked[:5])

    async def finish(self, text: str | None = None) -> None:
        if self.finished:
            return
        self.finished = True
        self.cancel_timer()
        if self.mgr.active.get(self.chat_id) is self:
            del self.mgr.active[self.chat_id]
        if text:
            try:
                await self.say(text + self.scoreboard())
            except TelegramError as e:
                log.warning("game finish send failed: %s", e)

    async def ai_json(self, system: str, user: str) -> dict:
        return await self.svc.llm.json(system, user, model=self.svc.cfg.guard_model, max_tokens=2500, chat_id=self.chat_id)

    # 하위 클래스가 구현
    async def begin(self) -> None: ...

    async def on_text(self, msg: Message, text: str) -> bool:
        return False

    async def on_callback(self, query: CallbackQuery, parts: list[str]) -> None:
        await query.answer()

    def ai_hint(self) -> str:
        """게임 중 AI 에게 주는 단서: 진행은 게임이 하니 AI 는 끼어들지 않게."""
        return (f"지금 이 방에서 '{self.title}' 게임이 진행 중이다. 진행·판정은 게임이 따로 한다 — 너는 게임 답을 내거나 "
                "대신 진행하지 않는다.")


CATEGORIES = ["동물", "음식", "과일", "물건", "직업", "장소", "스포츠", "나라", "가전제품", "탈것"]


# ── 끝말잇기 ──────────────────────────────────────────────
class WordChain(Game):
    title = "끝말잇기"
    TURN_SECONDS = 40
    STARTERS = ["기차", "바다", "나무", "구름", "사과", "커피", "여름", "우산", "모자", "가방"]

    async def begin(self) -> None:
        self.last = random.choice(self.STARTERS)
        self.used = {self.last}
        self.busy = False
        await self.say(f"🔗 <b>끝말잇기</b> 시작! 제가 먼저 할게요: <b>{self.last}</b>\n"
                       f"'{self._starts_text()}'(으)로 시작하는 단어를 한 단어만 쳐주세요. 두음법칙 OK!\n"
                       f"{self.TURN_SECONDS}초 안에 아무도 못 이으면 제가 이겨요 😎")
        self.set_timer(self.TURN_SECONDS, self._timeout)

    def _starts_text(self) -> str:
        return "/".join(sorted(starts_for(self.last)))

    def ai_hint(self) -> str:
        return (super().ai_hint() + f" 끝말잇기 마지막 단어는 '{self.last}' 이고 다음은 '{self._starts_text()}'(으)로 시작하는 "
                "단어를 한 단어만 쳐야 한다. 너는 단어를 내지 말고, 필요하면 그렇게 안내만 한다.")

    async def _timeout(self) -> None:
        await self.finish(f"⏰ 시간 초과! 제가 이겼어요 😎 (총 {len(self.used)}단어)")

    async def on_text(self, msg: Message, text: str) -> bool:
        word = text.strip()
        if self.busy or not is_hangul_word(word) or not 2 <= len(word) <= 8:
            return False
        if word[0] not in starts_for(self.last):
            return False  # 끝말잇기 답이 아닌 평범한 채팅일 수 있으니 무시
        if word in self.used:
            await msg.reply_text("이미 나온 단어예요!")
            return True
        self.busy = True
        try:
            await self._play(msg, word)
        finally:
            self.busy = False
        return True

    async def _play(self, msg: Message, word: str) -> None:
        # word 는 한글 음절만 통과했으므로 지시문을 담을 수 없다
        data = await self.ai_json(
            "너는 끝말잇기 심판 겸 선수다. 사용자 단어가 표준국어대사전에 있는 한국어 명사인지 판정하라 "
            "(고유명사·신조어·줄임말 제외). 맞으면 그 단어의 마지막 글자로 시작하는 명사 하나를 이어라 "
            "(두음법칙 허용, 사용한 단어 금지). 이을 단어가 없으면 next 를 빈 문자열로. "
            'JSON: {"valid": true, "reason": "틀렸을 때 짧은 이유", "next": "단어"}',
            f"사용자 단어: {word}\n이어야 할 첫 글자: {'/'.join(sorted(starts_for(word)))}\n"
            f"이미 사용한 단어: {', '.join(list(self.used)[-40:])}")
        if not data.get("valid"):
            reason = str(data.get("reason", ""))[:40]  # reply_text 는 일반 텍스트라 escape 하지 않음
            await msg.reply_text(f"❌ '{word}'는 사전에 없는 단어 같아요 {('(' + reason + ')') if reason else ''}")
            return
        self.used.add(word)
        await self.award(msg.from_user, 1)
        nxt = norm(str(data.get("next", "")))
        if not (is_hangul_word(nxt) and 2 <= len(nxt) <= 8 and nxt[0] in starts_for(word) and nxt not in self.used):
            await self.award(msg.from_user, 5)
            await self.finish(f"😵 제가 졌어요! {mention(msg.from_user.id, user_name(msg.from_user))} 대표님 승리 +5점")
            return
        self.used.add(nxt)
        self.last = nxt
        await msg.reply_text(f"✅ {word} → 🤖 {nxt}\n'{self._starts_text()}'(으)로 이어주세요!")
        self.set_timer(self.TURN_SECONDS, self._timeout)


GAMES: dict[str, type[Game]] = {
    "끝말잇기": WordChain, "끝말": WordChain,
}
GAME_LIST = "끝말잇기"


class GameManager:
    def __init__(self, svc: Services):
        self.svc = svc
        self.active: dict[int, Game] = {}

    def is_active(self, chat_id: int) -> bool:
        game = self.active.get(chat_id)
        return bool(game and not game.finished)

    async def start(self, bot: Bot, chat_id: int, starter_id: int, key: str) -> str:
        settings = await self.svc.db.get_settings(chat_id)
        if not settings["games_enabled"]:
            return "이 방은 게임이 꺼져 있어요."
        if not await self.svc.paid_features(chat_id):
            return "게임은 이용 기간 중인 방에서 쓸 수 있어요. 관리자님은 .구독 으로 확인해주세요."
        cls = GAMES.get(key.strip())
        if not cls:
            return f"게임 종류: {GAME_LIST}"
        if self.is_active(chat_id):
            return f"이미 {self.active[chat_id].title} 진행 중이에요. 끝나면 새로 시작할 수 있어요."
        game = cls(self, bot, chat_id, starter_id)
        if not cls.instant:
            self.active[chat_id] = game
        try:
            await game.begin()
        except (GameSetupError, OpenAIError, BudgetExceeded, TelegramError) as e:
            log.warning("game setup failed: %r", e)
            game.finished = True
            self.active.pop(chat_id, None)
            return "게임 준비가 잘 안 됐어요. 잠시 후 다시 해주세요 🙏"
        return f"{cls.title} 시작했어요!"

    async def stop(self, chat_id: int) -> bool:
        game = self.active.get(chat_id)
        if not game:
            return False
        await game.finish("🛑 게임을 종료했어요.")
        return True

    async def on_text(self, msg: Message, text: str) -> bool:
        game = self.active.get(msg.chat_id)
        if not game or game.finished:
            return False
        try:
            return await game.on_text(msg, text)
        except (OpenAIError, BudgetExceeded) as e:
            log.warning("game AI failed: %s", e)
            await msg.reply_text("AI 연결이 불안정해요. 잠시 후 다시 해주세요.")
            return True

    async def on_callback(self, query: CallbackQuery, parts: list[str]) -> None:
        game = self.active.get(query.message.chat_id) if query.message else None
        if not game or game.finished:
            await query.answer("끝난 게임이에요.")
            return
        await game.on_callback(query, parts)
