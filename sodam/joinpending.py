"""📥 입장요청(입요) 쌓기 — 봇이 받는 모든 가입 신청을 DB 에 저장해 두고, 나중에 전원 승인·가라 거절을 봇 혼자 한다.

왜: 텔레그램은 '가입 신청 명단 보기'를 **관리자인 유저 계정**에만 허용한다 (봇 API 엔 목록 기능이 없고, 봇 토큰 MTProto 는 BotMethodInvalid,
일반 멤버 계정은 ChatAdminRequired — 2026-11-22 슈퍼배드 소통방에서 실측). 하지만 봇은 신청이 들어올 때마다 `chat_join_request` **업데이트**를 받는다.
예전엔 그 방 가입확인(join_verify)이 켜졌을 때만 저장했는데(joinreq.py), 꺼진 방은 그냥 버렸다 → 나중에 명단을 못 봤다.
이제 **설정과 무관하게 들어오는 입요를 전부 join_pending 에 쌓는다** → 봇이 아는 user_id 로 `approveChatJoinRequest` 를 돌리면
관리자 승격·MTProto 없이 전원 승인(또는 가라 전원 거절)이 된다. 모든 방 공통. (단, 이 기능 배포 **이후** 들어온 신청만 쌓임 — 예전 밀린 건 관리자 조회가 1회 필요.)

비우기: 승인·거절·실제 입장 때 그 줄을 지운다 (handle_new_member·_decline·channel.decide·bulk). 오래된 줄은 tick 이 정리.
"""
from __future__ import annotations

import asyncio
import re
import time

from telegram.error import RetryAfter, TelegramError

from . import hooks
from .db import register_schema
from .services import Services

register_schema("""
CREATE TABLE IF NOT EXISTS join_pending (
    chat_id  INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    name     TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    ts       INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
""", migrate={"join_pending": "composite"})

KEEP_DAYS = 60              # 이보다 오래된 신청 줄은 정리 (텔레그램도 오래되면 만료)
_HANGUL = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ]")
BATCH = 6                  # 전원 처리 동시 호출 수
GAP = 0.4                  # 배치 사이 대기 (테스트에서 0 으로)
MAX_BULK = 5000            # 한 번에 처리 상한


async def record(svc: Services, req) -> None:
    """가입 신청 1건을 저장 (방·채널 공통, 설정 무관). handlers.on_join_request 맨 앞에서 부른다."""
    try:
        u = req.from_user
        if u is None or getattr(u, "is_bot", False):
            return
        name = ((u.first_name or "") + " " + (u.last_name or "")).strip()[:64]
        await svc.db._write(
            "INSERT OR REPLACE INTO join_pending(chat_id, user_id, name, username, ts) VALUES(?,?,?,?,?)",
            (req.chat.id, u.id, name, (u.username or "")[:64], int(time.time())))
    except Exception:
        pass   # 기록 실패해도 가입확인·관리 흐름은 계속


async def forget(svc: Services, chat_id: int, user_id: int) -> None:
    """승인·거절·실제 입장으로 끝난 신청 줄을 지운다."""
    try:
        await svc.db._write("DELETE FROM join_pending WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    except Exception:
        pass


async def count(svc: Services, chat_id: int) -> tuple[int, int, int]:
    """(전체, 한글 이름, 비한글) — 가라(중국·러시아·랜덤)는 보통 비한글."""
    rows = await svc.db._all("SELECT name FROM join_pending WHERE chat_id=?", (chat_id,))
    ko = sum(1 for r in rows if _HANGUL.search(r["name"] or ""))
    return len(rows), ko, len(rows) - ko


def _match(name: str, scope: str) -> bool:
    ko = bool(_HANGUL.search(name or ""))
    if scope == "korean":
        return ko
    if scope == "foreign":
        return not ko
    return True   # all


async def pending(svc: Services, chat_id: int, scope: str = "all", limit: int = MAX_BULK) -> list[int]:
    rows = await svc.db._all("SELECT user_id, name FROM join_pending WHERE chat_id=? ORDER BY ts LIMIT ?",
                             (chat_id, limit))
    return [r["user_id"] for r in rows if _match(r["name"] or "", scope)]


async def bulk(svc: Services, bot, chat_id: int, approve: bool, scope: str = "all") -> tuple[int, int]:
    """명단으로 전원 승인(approve=True) 또는 전원 거절. (성공, 실패). 이미 처리된 건 성공으로 보고 줄만 지운다."""
    ids = await pending(svc, chat_id, scope)
    ok = fail = 0
    lock = asyncio.Lock()

    async def one(uid: int) -> None:
        nonlocal ok, fail
        for _ in range(6):
            try:
                if approve:
                    await bot.approve_chat_join_request(chat_id, uid)
                else:
                    await bot.decline_chat_join_request(chat_id, uid)
                await forget(svc, chat_id, uid)
                async with lock:
                    ok += 1
                return
            except RetryAfter as e:
                await asyncio.sleep(float(getattr(e, "retry_after", 2)) + 0.3)
                continue
            except TelegramError:
                # 이미 처리됨·신청 취소·이미 멤버 → 더 할 게 없으니 줄 지우고 성공으로
                await forget(svc, chat_id, uid)
                async with lock:
                    ok += 1
                return
            except Exception:
                await asyncio.sleep(0.5)
                continue
        async with lock:
            fail += 1

    for i in range(0, len(ids), BATCH):
        await asyncio.gather(*(one(u) for u in ids[i:i + BATCH]))
        if GAP:
            await asyncio.sleep(GAP)
    return ok, fail


async def cleanup(svc: Services) -> None:
    """오래된 신청 줄 정리 (tick 에서 한 시간에 한 번)."""
    try:
        await svc.db._write("DELETE FROM join_pending WHERE ts < ?", (int(time.time()) - KEEP_DAYS * 86400,))
    except Exception:
        pass


_last_clean = [0.0]


async def _tick(svc: Services, bot) -> None:
    now = time.time()
    if now - _last_clean[0] < 3600:
        return
    _last_clean[0] = now
    await cleanup(svc)


hooks.add_tick_hook(_tick)
