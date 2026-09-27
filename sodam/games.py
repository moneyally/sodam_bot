"""대표님들과 봇이 하는 말 게임 (끝말잇기). 포인트 걸고 하는 게임은 sodam/casino/ (! 명령).

원칙: 정답·점수 판정은 코드가 한다. AI는 문제를 만들거나 예/아니요 판정만 한다.
포인트는 순위용이며 현금·코인으로 바꾸는 기능은 넣지 않는다 (넣는 순간 도박 규제 대상).
"""
from __future__ import annotations

import asyncio
import gzip
import logging
import random
import time
from pathlib import Path
from typing import TYPE_CHECKING

from openai import OpenAIError
from telegram import Bot, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
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
# 판정은 표준국어대사전 명사 목록(sodam/data_files/words_ko.txt.gz)으로 — AI 호출 0, 즉시, 사람이 몰려도 과부하 없음.
WORDS_PATH = Path(__file__).parent / "data_files" / "words_ko.txt.gz"
_WORDS: set[str] = set()
_COMMON: dict[str, list[str]] = {}      # 첫 글자 → 흔한 낱말 (봇이 이을 말)
_FIRSTS: set[str] = set()                # 사전 낱말의 첫 글자들 (이을 말이 있는지 = 한방 단어 아님)
GAP_SECONDS = 3.0                        # 게임이 방에 올리는 글 사이 최소 간격 (텔레그램: 한 그룹에 분당 20개)


def load_words(lines: list[str] | None = None) -> None:
    """사전 읽기 ('*' = 흔한 낱말). 테스트는 lines 로 작은 목록을 넣는다."""
    global _WORDS, _COMMON, _FIRSTS
    if lines is None:
        lines = gzip.decompress(WORDS_PATH.read_bytes()).decode().split("\n")
    words, common = set(), {}
    for line in lines:
        w = line.lstrip("*")
        if w:
            words.add(w)
            if line.startswith("*"):
                common.setdefault(w[0], []).append(w)
    _WORDS, _COMMON, _FIRSTS = words, common, {w[0] for w in words}


def is_word(word: str) -> bool:
    return word in _WORDS


def can_follow(word: str) -> bool:
    """이을 말이 사전에 있는지 (없으면 '한방 단어')."""
    return any(ch in _FIRSTS for ch in starts_for(word))


def pick_next(word: str, used: set[str]) -> str | None:
    """봇이 이을 말: 흔한 낱말 중 안 쓴 것·사람이 이을 수 있는 것 (봇이 한방 단어로 이기지 않게), 없으면 None (봇 패배)."""
    pool = [w for ch in starts_for(word) for w in _COMMON.get(ch, ()) if w not in used and can_follow(w)]
    return random.choice(pool) if pool else None


REACT = {"late": "🙈", "used": "🤨", "unknown": "🤔"}


class WordChain(Game):
    """자유 모드: 아무나 먼저 맞게 치면 그 사람 차례로 치고 봇이 바로 잇는다 (선착순).
    같은 글자로 늦게 친 사람·사전에 없는 말·이미 나온 말엔 글 대신 반응(🙈🤔🤨)만 — 방이 도배되지 않고 무시당한 느낌도 없게."""
    title = "끝말잇기"
    TURN_SECONDS = 40
    STARTERS = ["기차", "바다", "나무", "사과", "커피", "우산", "모자", "가방", "사탕", "하늘"]

    def first_word(self) -> str:
        """첫 낱말은 봇도 이을 수 있는 것만 (한방 단어로 시작하지 않게)."""
        return random.choice([w for w in self.STARTERS if pick_next(w, {w})] or self.STARTERS)

    async def begin(self) -> None:
        if not _WORDS:
            await asyncio.to_thread(load_words)
        self.last, self.stale = self.first_word(), ""
        self.used = {self.last}
        self._said = 0.0
        await self.say(f"🔗 <b>끝말잇기</b> 시작! 제가 먼저 할게요: <b>{self.last}</b>\n"
                       f"'{self._starts_text()}'(으)로 시작하는 낱말을 먼저 치는 사람이 이어요. 두음법칙 OK!\n"
                       f"{self.TURN_SECONDS}초 안에 아무도 못 이으면 제가 이겨요 😎")
        self.set_timer(self.TURN_SECONDS, self._timeout)

    def _starts_text(self, word: str | None = None) -> str:
        return "/".join(sorted(starts_for(word or self.last)))

    def ai_hint(self) -> str:
        return (super().ai_hint() + f" 끝말잇기 마지막 단어는 '{self.last}' 이고 다음은 '{self._starts_text()}'(으)로 시작하는 "
                "단어를 한 단어만 쳐야 한다. 너는 단어를 내지 말고, 필요하면 그렇게 안내만 한다.")

    async def _timeout(self) -> None:
        await self.finish(f"⏰ 시간 초과! 제가 이겼어요 😎 (총 {len(self.used)}단어)")

    async def react(self, msg: Message, kind: str) -> bool:
        try:
            await self.bot.set_message_reaction(self.chat_id, msg.message_id, REACT[kind])
        except TelegramError:
            pass   # 반응을 못 달아도 게임은 계속
        return True

    async def pace(self) -> None:
        """게임 글 사이 GAP_SECONDS (그룹 전송 한도 안에서 도배 없이)."""
        wait = self._said + GAP_SECONDS - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._said = time.monotonic()

    def check(self, word: str) -> str | None:
        """이을 수 있으면 None, 아니면 반응 종류. 'no' = 끝말잇기 답이 아님 (평범한 채팅)."""
        if not is_hangul_word(word) or not 2 <= len(word) <= 12:
            return "no"
        if word[0] not in starts_for(self.last):
            return "late" if self.stale and word[0] in starts_for(self.stale) and is_word(word) else "no"
        if word in self.used:
            return "used"
        return None if is_word(word) else "unknown"

    async def on_text(self, msg: Message, text: str) -> bool:
        word = text.strip()
        why = self.check(word)
        if why == "no":
            return False
        if why:
            return await self.react(msg, why)
        nxt = pick_next(word, self.used | {word})     # await 전에 상태를 바꿔 동시에 온 답은 '늦음'이 된다
        self.used |= {word, nxt} - {None}
        self.stale, self.last = self.last, nxt or word
        self.cancel_timer()
        await self.award(msg.from_user, 1)
        who = mention(msg.from_user.id, user_name(msg.from_user))
        if not nxt:
            await self.award(msg.from_user, 5)
            await self.finish(f"😵 '{esc(word)}' 다음 말이 없어요, 제가 졌어요! {who} 대표님 승리 +6점")
            return True
        await self.pace()
        await self.say(f"✅ {who} {esc(word)} → 🤖 <b>{nxt}</b>\n'{self._starts_text()}'(으)로 이어주세요!")
        self.set_timer(self.TURN_SECONDS, self._timeout)
        return True


class WordChainTurn(WordChain):
    """차례 모드 (여럿이·이벤트): [🙋 참가] → 순서대로 차례 → 시간 안에 못 이으면 탈락 → 마지막 1명 우승.
    차례가 아닌 사람의 말은 평범한 채팅으로 흘려보낸다 (구경·응원 가능). 봇은 심판만."""
    title = "끝말잇기 차례"
    JOIN_SECONDS = 40
    MIN_PLAYERS, MAX_PLAYERS = 2, 50
    TURN_START, TURN_MIN = 20, 8

    async def begin(self) -> None:
        if not _WORDS:
            await asyncio.to_thread(load_words)
        self.last, self.stale, self.used, self._said = self.first_word(), "", set(), 0.0
        self.used.add(self.last)
        self.players: list[tuple[int, str]] = []
        self.joined = 0
        self.joining, self.turns, self._edited = True, 0, 0.0
        self.join_msg = await self.say(self._join_text(), reply_markup=self._join_kb())
        self.set_timer(self.JOIN_SECONDS, self._start)

    def _join_kb(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("🙋 참가", callback_data="wc:j"),
                                      InlineKeyboardButton("▶️ 바로 시작", callback_data="wc:go")]])

    def _join_text(self) -> str:
        names = ", ".join(esc(n) for _, n in self.players) or "(아직 없음)"
        return (f"🔗 <b>끝말잇기 (차례·탈락)</b> 참가 받아요! {self.JOIN_SECONDS}초 뒤 시작 · {self.MIN_PLAYERS}~{self.MAX_PLAYERS}명\n"
                f"차례에 못 이으면 탈락, 마지막 1명이 우승이에요.\n🙋 {len(self.players)}명: {names}")

    def ai_hint(self) -> str:
        if self.joining:
            return Game.ai_hint(self) + " 지금은 참가 모집 중이다 — 게임 글의 [🙋 참가] 버튼을 누르면 된다고 안내만 한다."
        cur = self.players[0][1] if self.players else "?"
        return Game.ai_hint(self) + f" 지금 {cur} 차례이고 '{self._starts_text()}'(으)로 시작해야 한다. 너는 단어를 내지 않는다."

    async def on_callback(self, query: CallbackQuery, parts: list[str]) -> None:
        user = query.from_user
        if not self.joining:
            await query.answer("이미 시작했어요. 다음 판에 참가해 주세요!")
            return
        if parts[:1] == ["go"]:
            if user.id != self.starter_id and not await self.svc.perms.is_admin(self.bot, self.chat_id, user.id):
                await query.answer("시작한 사람이나 관리자만 바로 시작할 수 있어요.", show_alert=True)
                return
            await query.answer()
            self.cancel_timer()
            await self._start()
            return
        if any(uid == user.id for uid, _ in self.players):
            await query.answer("이미 참가했어요!")
            return
        if len(self.players) >= self.MAX_PLAYERS:
            await query.answer("자리가 다 찼어요.", show_alert=True)
            return
        self.players.append((user.id, user_name(user)[:20]))
        await query.answer("🙋 참가했어요!")
        if time.monotonic() - self._edited >= GAP_SECONDS:     # 명단 수정도 간격을 두고 (몰려도 과부하 없게)
            self._edited = time.monotonic()
            try:
                await self.bot.edit_message_text(self._join_text(), chat_id=self.chat_id, message_id=self.join_msg.message_id,
                                                 parse_mode="HTML", reply_markup=self._join_kb())
            except TelegramError:
                pass

    async def _start(self) -> None:
        if not self.joining:
            return
        self.joining = False
        if len(self.players) < self.MIN_PLAYERS:
            await self.finish(f"🙅 참가자가 {self.MIN_PLAYERS}명보다 적어서 취소했어요.")
            return
        random.shuffle(self.players)
        self.joined = len(self.players)
        await self._announce(f"🏁 {self.joined}명 시작! 첫 낱말: <b>{self.last}</b>\n")

    def _turn_seconds(self) -> int:
        return max(self.TURN_MIN, self.TURN_START - self.turns // 3)

    async def _announce(self, head: str = "") -> None:
        uid, name = self.players[0]
        t = self._turn_seconds()
        await self.pace()
        await self.say(f"{head}👉 {mention(uid, name)} 차례! <b>{self.last}</b> → '{self._starts_text()}'(으)로 ({t}초) "
                       f"· 남은 {len(self.players)}명")
        self.set_timer(t, self._turn_timeout)

    async def _turn_timeout(self) -> None:
        uid, name = self.players.pop(0)
        head = f"💥 {esc(name)} 탈락!\n"
        if len(self.players) == 1:
            win_id, win_name = self.players[0]
            bonus = 5 + 2 * self.joined
            entry = self.scores.setdefault(win_id, [win_name, 0])
            entry[1] += bonus
            from .casino.core import credit
            await credit(self.svc.db, self.chat_id, win_id, bonus, f"game:{self.title}")
            await self.finish(f"{head}🏆 {mention(win_id, win_name)} 우승! +{bonus}점 (총 {len(self.used)}단어)")
            return
        await self._announce(head)

    def check(self, word: str) -> str | None:
        why = super().check(word)
        return "no" if why == "late" else why       # 차례 모드엔 '늦음'이 없음

    async def on_text(self, msg: Message, text: str) -> bool:
        if self.joining or not self.players or msg.from_user.id != self.players[0][0]:
            return False                            # 차례가 아닌 사람 말은 평범한 채팅
        word = text.strip()
        why = self.check(word)
        if why == "no":
            return False
        if why:
            return await self.react(msg, why)       # 시간 안에 다시 치면 됨
        self.used.add(word)
        self.last, self.turns = word, self.turns + 1
        self.players.append(self.players.pop(0))
        self.cancel_timer()
        await self.award(msg.from_user, 1)
        await self._announce(f"✅ {esc(word)}\n")
        return True


GAMES: dict[str, type[Game]] = {
    "끝말잇기": WordChain, "끝말": WordChain,
    "끝말잇기 차례": WordChainTurn, "끝말잇기차례": WordChainTurn, "차례": WordChainTurn, "끝말 차례": WordChainTurn,
}
GAME_LIST = "끝말잇기 (아무나 먼저) · 끝말잇기 차례 (참가·탈락)"


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
        cls = GAMES.get(" ".join(key.split())) or GAMES.get(key.split()[0] if key.split() else "")
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
