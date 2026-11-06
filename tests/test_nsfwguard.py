"""🔞 음란물 자동 감지·삭제: python tests/run_all.py nsfwguard

본코드 경로(handlers.on_group_message → 관리 검사 통과 → hooks → nsfwguard.on_message)와
관리자 1:1 알림 버튼(menu.on_callback → m:nsx)을 가짜 텔레그램 + 가짜 판정으로 돈다.
OpenAI moderation 은 실제로 안 부르고 svc.llm.moderate_image / vision._fetch 를 가짜로 바꿔 끼운다.
"""
import asyncio
from types import SimpleNamespace

from fake_llm import Room
from fakes import FakeQuery, fake_user, runner

from sodam import menu, nsfwguard, vision

test, run_all = runner()
ADMIN, MEMBER, OTHER = 5, 20, 21


async def room(*, mode="delete", can_moderate=True, paid=True):
    r = Room()
    await r.open(admins=(ADMIN,), settings={"nsfw_mode": mode} if mode else {})
    r.bot.admins = [fake_user(ADMIN, "방장")]
    r.bot.can_moderate = can_moderate
    perms = r.svc.perms

    async def can(bot, cid, uid, right="restrict"):
        return uid in perms.admins
    perms.can = can
    await r.join(fake_user(MEMBER, "멤버"))
    await r.join(fake_user(OTHER, "딴사람"))
    if not paid:   # 구독과 무관하게 동작해야 함 — paid=False 로 확인
        r.svc.billing = SimpleNamespace(active=_inactive)
    return r


async def _inactive(chat_id):
    return False


# ── 가짜 판정 끼우기 ──────────────────────────────────────
_SAVED = {}


def patch_scores(r, score_by_uid):
    """file_unique_id → sexual 점수. vision 은 아무 이미지나 가져온 척, moderate_image 는 대본대로."""
    calls = []

    def media_of(m):
        return SimpleNamespace(kind="photo", mime="image/jpeg", size=1000) if nsfwguard.media_obj(m) else None

    async def fetch(bot, src, md):
        return SimpleNamespace(data=b"img-bytes", mime="image/jpeg")

    async def moderate(data, mime):
        calls.append(1)
        uid = r._cur_uid
        sc = score_by_uid.get(uid, 0.0)
        return {"sexual": sc, "violence": 0.0, "flagged": sc >= 0.5}

    vision._media_of, vision._fetch = media_of, fetch
    r.svc.llm.moderate_image = moderate
    r.llm.moderate_image = moderate
    return calls


async def send_media(r, uid, *, file_uid="porn1", kind="sticker", name="멤버"):
    """멤버가 스티커/사진을 올림 → 파이프라인 전체."""
    r._mid += 1
    m = SimpleNamespace(chat_id=r.CHAT, from_user=fake_user(uid, name), text="", caption=None,
                        message_id=r._mid, reply_to_message=None, entities=(), caption_entities=(),
                        deleted=False, replies=[], reply_kws=[])
    m.chat = SimpleNamespace(id=r.CHAT, title="대표님들 소통방", type="supergroup")
    m.sender_chat = None
    obj = SimpleNamespace(file_id=f"F{file_uid}", file_unique_id=file_uid, file_size=2000,
                          emoji="", set_name="", is_video=False, is_animated=False, type="regular")
    if kind == "sticker":
        m.sticker = obj
    elif kind == "photo":
        m.photo = (obj,)

    async def _del():
        m.deleted = True
    m.delete = _del
    r._cur_uid = uid
    await handlers_on_group(r, m)
    await r.settle()
    return m


async def handlers_on_group(r, m):
    from fake_llm import handlers
    await handlers.on_group_message(SimpleNamespace(message=m), r.ctx)


def dms(r, uid=None):
    return [c for c in r.bot.named("send_message") if c[1] > 0 and (uid is None or c[1] == uid)]


def buttons(call):
    kb = call[3].get("reply_markup")
    return [b.callback_data for row in kb.inline_keyboard for b in row] if kb else []


async def press(r, uid, data):
    q = FakeQuery(uid, fake_user(uid, "관리자"), data)   # 알림 버튼은 관리자 1:1 (chat_id = 그 사람)
    await menu.on_callback(r.svc, r.bot, q, data.split(":")[1:])
    return q


# ── 테스트 ────────────────────────────────────────────────
@test
async def porn_sticker_deleted_and_admin_alerted():
    r = await room(mode="delete")
    patch_scores(r, {MEMBER: 0.95})
    m = await send_media(r, MEMBER, file_uid="p1")
    assert m.deleted, "음란 스티커는 지워져야 함"
    d = dms(r, ADMIN)
    assert d, "차단 권한 관리자에게 1:1 알림"
    assert "음란물" in d[-1][2]
    cbs = buttons(d[-1])
    assert any(":b" in c for c in cbs) and any(":t" in c for c in cbs), cbs
    bad = await r.db._one("SELECT score FROM nsfw_bad WHERE file_uid='p1'")
    assert bad and bad["score"] >= 0.7, "지문 학습"


@test
async def clean_sticker_untouched():
    r = await room(mode="delete")
    calls = patch_scores(r, {MEMBER: 0.05})
    m = await send_media(r, MEMBER, file_uid="clean1")
    assert not m.deleted and not dms(r, ADMIN)
    assert len(calls) == 1, "검사는 함"
    assert not await r.db._one("SELECT 1 FROM nsfw_bad WHERE file_uid='clean1'")


@test
async def off_mode_does_not_even_call_api():
    r = await room(mode="off")
    calls = patch_scores(r, {MEMBER: 0.99})
    m = await send_media(r, MEMBER, file_uid="x1")
    assert not m.deleted and not calls, "꺼진 방은 API 도 안 부름"


@test
async def notify_mode_alerts_without_deleting():
    r = await room(mode="notify")
    patch_scores(r, {MEMBER: 0.95})
    m = await send_media(r, MEMBER, file_uid="n1")
    assert not m.deleted, "알림만 모드는 안 지움"
    assert dms(r, ADMIN), "그래도 알림은 감"


@test
async def repeat_sticker_deleted_without_second_api_call():
    r = await room(mode="delete")
    calls = patch_scores(r, {MEMBER: 0.95, OTHER: 0.95})
    await send_media(r, MEMBER, file_uid="same")
    assert len(calls) == 1
    nsfwguard._CACHE.clear()   # 메모리 캐시 비움(재시작 흉내) → DB 지문(nsfw_bad)만으로 바로 지워야 함
    m2 = await send_media(r, OTHER, file_uid="same", name="딴사람")   # 같은 스티커 도배 (다른 사람)
    assert m2.deleted, "같은 조각은 바로 지움"
    assert len(calls) == 1, "두 번째는 API 안 부름 (DB 지문)"


@test
async def admin_and_free_members_excluded():
    r = await room(mode="delete")
    patch_scores(r, {ADMIN: 0.99, OTHER: 0.99})
    ma = await send_media(r, ADMIN, file_uid="a1", name="방장")
    assert not ma.deleted, "관리자는 검사 안 함"
    from sodam import free
    await free.add(r.db, r.CHAT, OTHER, ADMIN)
    mf = await send_media(r, OTHER, file_uid="f1", name="딴사람")
    assert not mf.deleted, "자유 멤버는 검사 안 함"


@test
async def false_positive_allowlist_stops_future_deletes():
    r = await room(mode="delete")
    patch_scores(r, {MEMBER: 0.95, OTHER: 0.95})
    await send_media(r, MEMBER, file_uid="fp")
    d = dms(r, ADMIN)[-1]
    aid = int([c for c in buttons(d) if c.endswith(":t")][0].split(":")[3])
    await press(r, ADMIN, f"m:nsx:{r.CHAT}:{aid}:t")   # ✅ 오탐 (이 조각 통과)
    assert await r.db._one("SELECT 1 FROM nsfw_ok WHERE chat_id=? AND file_uid='fp'", (r.CHAT,))
    assert not await r.db._one("SELECT 1 FROM nsfw_bad WHERE file_uid='fp'"), "나쁨 기록도 지움"
    m2 = await send_media(r, OTHER, file_uid="fp", name="딴사람")
    assert not m2.deleted, "오탐 처리한 조각은 다시 안 지움"


@test
async def alert_button_bans():
    r = await room(mode="notify")
    patch_scores(r, {MEMBER: 0.95})
    await send_media(r, MEMBER, file_uid="b1")
    d = dms(r, ADMIN)[-1]
    aid = int([c for c in buttons(d) if c.endswith(":b")][0].split(":")[3])
    await press(r, ADMIN, f"m:nsx:{r.CHAT}:{aid}:b")
    assert any(c[0] == "ban" and c[2] == MEMBER for c in r.bot.calls), "밴 실행"


@test
async def works_even_when_subscription_inactive():
    r = await room(mode="delete", paid=False)
    patch_scores(r, {MEMBER: 0.95})
    m = await send_media(r, MEMBER, file_uid="exp1")
    assert m.deleted, "구독 끝난 방도 음란물은 막아야 함"


if __name__ == "__main__":
    run_all()
