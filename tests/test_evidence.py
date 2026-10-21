"""근거(영수증): 제재·단정·따질 때 기록에서 원문+시각 (sodam/evidence.py · panels/evidence.py).
오너 2026-10-21 '거짓말했으면 근거 제시해서 반박 못하게'. 사실은 코드가 기록에서만 (AI 비용 0)."""
import time

from fakes import FakeBot, add_member, fake_user, make_db, make_svc, runner

import sodam.panels  # noqa: F401 — 도구 등록
from sodam import evidence, tools
from sodam.permissions import Role
from sodam.panels import evidence as E
from sodam.tools import ToolCtx

test, run_all = runner()
CHAT, ADMIN, SPAMMER = -100555, 1, 50


async def world():
    db = await make_db()
    svc = await make_svc(db, admins=(ADMIN,))
    await db.ensure_chat(CHAT, "테스트방")
    await add_member(db, CHAT, fake_user(SPAMMER, "광고충"))
    await add_member(db, CHAT, fake_user(ADMIN, "관리"))
    return db, svc, FakeBot(admins=[fake_user(ADMIN, "관리")])


def ctx(svc, bot, uid=ADMIN, role=Role.ADMIN):
    return ToolCtx(svc, bot, CHAT, fake_user(uid, "관리"), role, {})


async def _say(db, uid, mid, text, ago=0):
    await db.log_message(CHAT, uid, mid, text, ts=int(time.time()) - ago)


@test
async def receipts_catches_repeat_spam():
    db, svc, _ = await world()
    await _say(db, SPAMMER, 1, "지금 입장하면 300% 보너스", 120)
    await _say(db, SPAMMER, 2, "지금 입장하면 300% 보너스", 60)
    await _say(db, SPAMMER, 3, "지금 입장하면 300% 보너스", 10)
    out = await evidence.receipts(svc, CHAT, SPAMMER)
    assert "같은 글 3번" in out and "근거" in out, out


@test
async def receipts_masks_links():
    db, svc, _ = await world()
    await _say(db, SPAMMER, 1, "여기 들어와 http://evil.com/win 대박", 30)
    out = await evidence.receipts(svc, CHAT, SPAMMER)
    assert "evil[.]com" in out and "http" not in out, out


@test
async def receipts_counts_prior_warns():
    db, svc, _ = await world()
    await _say(db, SPAMMER, 1, "안녕", 30)
    await db.log_mod(CHAT, ADMIN, SPAMMER, "warn", "도배")
    await db.log_mod(CHAT, ADMIN, SPAMMER, "warn", "또 도배")
    out = await evidence.receipts(svc, CHAT, SPAMMER)
    assert "경고 2번" in out, out


@test
async def receipts_empty_when_nothing():
    db, svc, _ = await world()   # 이 사람 글이 전혀 없음
    out = await evidence.receipts(svc, CHAT, SPAMMER)
    assert out == "", out


@test
async def receipts_ignores_old_messages():
    db, svc, _ = await world()
    await _say(db, SPAMMER, 1, "어제 올린 광고", 7 * 3600)   # 6시간 밖
    out = await evidence.receipts(svc, CHAT, SPAMMER)
    assert out == "", out


@test
async def tool_returns_receipt_and_taints():
    db, svc, bot = await world()
    await _say(db, SPAMMER, 1, "광고 광고 링크 http://x.io", 20)
    await _say(db, SPAMMER, 2, "광고 광고 링크 http://x.io", 10)
    c = ctx(svc, bot)
    out = await E.t_show_evidence(c, {"name": "광고충"})
    assert "같은 글 2번" in out and c.tainted, out
    assert "그대로 근거로" in out   # AI 에게 '원문·시각 그대로 근거로' 지시


@test
async def tool_refuses_to_fabricate_when_no_record():
    db, svc, bot = await world()
    out = await E.t_show_evidence(ctx(svc, bot), {"name": "광고충"})
    assert "지어내지" in out, out


@test
async def tool_in_can_do_map_and_registered():
    from sodam import prompt
    assert "show_evidence" in prompt.SYSTEM
    assert "show_evidence" in tools._BY_NAME


if __name__ == "__main__":
    run_all()
