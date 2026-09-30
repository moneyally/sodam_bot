"""관리자 확인 카드(act:<키>:y|p|n, handlers._confirm_action)로 실행하는 멤버 조치 — 종류마다 글자·실행 한 곳.

실제 버그 (2026-09-30 전수 점검): AI 밴 카드가 '내보내기'로 표시됐는데 실행은 영구 밴(ban_chat_member, 해제 없음).
같은 봇에서 '내보내기'는 .킥·캡차 kick(재입장 가능) 뜻이라 "쟤 내보내" → 카드 '내보내기' → 누르면 영구 밴.
이제 밴 = '밴(영구 추방)', 내보내기 = kick(다시 들어올 수 있음) 로 나누고, 푸는 조치(밴 해제·경고 취소·자유 멤버·캡차 통과)도
같은 카드로 (대화 속 숨은 지시로 스패머 밴이 풀리거나 자유 멤버가 되지 않게 — 사람이 눌러야 실행).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from . import free
from .moderation import StillBanned
from .util import esc, human_minutes, mention

Run = Callable[..., Awaitable[tuple[bool, str]]]   # (svc, bot, chat_id, uid, name, presser, action) → (된 것?, 한 줄)


@dataclass(frozen=True)
class Kind:
    label: str             # 카드 버튼·결과 기록 ('밴(영구 추방)')
    notice: str            # 오너 1:1 카드 [실행+방에 안내] 의 방 문구
    punitive: bool         # 관리자·봇 대상이면 거절 (제재)
    needs_bot: bool        # 봇에게 그 방 '사용자 차단' 권한이 있어야 실행되는 것
    run: Run


async def _warn(svc, bot, cid, uid, name, by, act):
    return True, await svc.mod.warn(bot, cid, uid, name, by, act.reason)


async def _mute(svc, bot, cid, uid, name, by, act):
    await svc.mod.mute(bot, cid, uid, act.minutes, by, act.reason)
    return True, f"🔇 {mention(uid, name)}님 {human_minutes(act.minutes)} 채팅 금지했어요."


async def _ban(svc, bot, cid, uid, name, by, act):
    await svc.mod.ban(bot, cid, uid, by, act.reason)
    return True, f"🚫 {mention(uid, name)}님을 밴(영구 추방)했어요. 다시 들어오려면 밴 해제가 필요해요."


async def _kick(svc, bot, cid, uid, name, by, act):
    await svc.mod.kick(bot, cid, uid, by, act.reason)
    return True, f"👢 {mention(uid, name)}님을 내보냈어요 (다시 들어올 수 있어요)."


async def _unban(svc, bot, cid, uid, name, by, act):
    await svc.mod.unban(bot, cid, uid, by)
    return True, f"↩️ {mention(uid, name)}님 밴을 풀었어요 (다시 들어올 수 있어요)."


async def _unwarn(svc, bot, cid, uid, name, by, act):
    if not await svc.db.warning_count(cid, uid):
        return False, f"ℹ️ {mention(uid, name)}님은 경고가 없어요."
    await svc.db.remove_last_warning(cid, uid)
    await svc.db.log_mod(cid, by, uid, "unwarn", act.reason)
    return True, f"↩️ {mention(uid, name)}님 경고 1회 취소했어요 (지금 {await svc.db.warning_count(cid, uid)}회)."


async def _resetwarns(svc, bot, cid, uid, name, by, act):
    if not await svc.db.warning_count(cid, uid):
        return False, f"ℹ️ {mention(uid, name)}님은 경고가 없어요."
    await svc.db.clear_warnings(cid, uid)
    await svc.db.log_mod(cid, by, uid, "resetwarns", act.reason)
    return True, f"🧹 {mention(uid, name)}님 경고를 모두 지웠어요."


async def _free(svc, bot, cid, uid, name, by, act):
    """commands.c_free 와 같게: 자유 멤버 + 경고 지움 + 채팅 금지 풀기 (밴된 사람은 밴 그대로 — StillBanned)."""
    await free.add(svc.db, cid, uid, by)
    await svc.db.clear_warnings(cid, uid)
    await svc.db.log_mod(cid, by, uid, "free", "지정")
    note = ""
    try:
        await svc.mod.unmute(bot, cid, uid, by)
    except StillBanned as e:
        note = f" ⚠️ {esc(e.message)}"
    except Exception as e:   # TelegramError 등: 자유 멤버 지정은 됐고 채팅 금지만 못 풂
        note = f" (채팅 금지는 못 풀었어요: {esc(getattr(e, 'message', str(e)))})"
    return True, f"🕊️ {mention(uid, name)}님을 자유 멤버로 지정했어요 (자동 통제 안 받음, 관리자 권한은 아님).{note}"


async def _unfree(svc, bot, cid, uid, name, by, act):
    if not await free.remove(svc.db, cid, uid):
        return False, f"ℹ️ {mention(uid, name)}님은 자유 멤버가 아니에요."
    await svc.db.log_mod(cid, by, uid, "free", "해제")
    return True, f"🕊️ {mention(uid, name)}님 자유 멤버를 해제했어요 (다시 자동 통제를 받아요)."


async def _captcha_pass(svc, bot, cid, uid, name, by, act):
    if not await svc.captcha.pending(cid, uid):
        return False, f"ℹ️ {mention(uid, name)}님은 캡차 대기 중이 아니에요."
    await svc.captcha.approve(bot, cid, uid, name, by)
    return True, f"✅ {mention(uid, name)}님 캡차를 통과 처리했어요."


KINDS: dict[str, Kind] = {
    "warn": Kind("경고", "경고", True, True, _warn),
    "mute": Kind("채팅 금지", "채팅 금지", True, True, _mute),
    "ban": Kind("밴(영구 추방)", "밴(영구 추방)", True, True, _ban),
    "kick": Kind("내보내기(재입장 가능)", "내보내기", True, True, _kick),
    "unban": Kind("밴 해제", "밴 해제", False, True, _unban),
    "unwarn": Kind("경고 1회 취소", "경고 취소", False, False, _unwarn),
    "resetwarns": Kind("경고 전부 지우기", "경고 초기화", False, False, _resetwarns),
    "free": Kind("자유 멤버 지정", "자유 멤버 지정", False, True, _free),
    "unfree": Kind("자유 멤버 해제", "자유 멤버 해제", False, False, _unfree),
    "captcha_pass": Kind("캡차 통과", "캡차 통과", False, True, _captcha_pass),
}


def label(kind: str, minutes: int = 0) -> str:
    """카드 글자: '밴(영구 추방)' · '채팅 금지 30분'."""
    k = KINDS.get(kind)
    return (k.label if k else kind) + (f" {human_minutes(minutes)}" if kind == "mute" and minutes else "")


def record_label(kind: str, minutes: int = 0) -> str:
    """누른 결과 한 줄(cards.record)용: 뮤트는 예전 그대로 '뮤트 30분'."""
    return f"뮤트 {human_minutes(minutes)}" if kind == "mute" else label(kind)


def notice(kind: str, minutes: int = 0) -> str:
    k = KINDS.get(kind)
    return (f"{human_minutes(minutes)} " if kind == "mute" and minutes else "") + (k.notice if k else kind)
