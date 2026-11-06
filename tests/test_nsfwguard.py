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
ADMIN, MEMBER, OTHER, OWNER = 5, 20, 21, 9
_ORIG_SKIN = nsfwguard._skin_ratio


async def room(*, mode="delete", can_moderate=True, paid=True):
    nsfwguard._CACHE.clear()
    nsfwguard._LOCKS.clear()
    nsfwguard._RECENT.clear()
    nsfwguard._skin_ratio = _ORIG_SKIN   # 앞 테스트가 바꾼 살색 함수 원복
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


async def send_media(r, uid, *, file_uid="porn1", kind="sticker", name="멤버", set_name=""):
    """멤버가 스티커/사진을 올림 → 파이프라인 전체."""
    r._mid += 1
    m = SimpleNamespace(chat_id=r.CHAT, from_user=fake_user(uid, name), text="", caption=None,
                        message_id=r._mid, reply_to_message=None, entities=(), caption_entities=(),
                        deleted=False, replies=[], reply_kws=[])
    m.chat = SimpleNamespace(id=r.CHAT, title="대표님들 소통방", type="supergroup")
    m.sender_chat = None
    obj = SimpleNamespace(file_id=f"F{file_uid}", file_unique_id=file_uid, file_size=2000,
                          emoji="", set_name=set_name, is_video=False, is_animated=False, type="regular")
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
async def porn_sticker_deleted_silently():
    r = await room(mode="delete")
    patch_scores(r, {MEMBER: 0.95})
    m = await send_media(r, MEMBER, file_uid="p1")
    assert m.deleted, "음란 스티커는 지워져야 함"
    assert not dms(r, ADMIN), "삭제 모드는 조용히 — 1:1 알림 안 보냄(도배 때 요청 폭주 방지)"
    assert not [c for c in r.bot.named("send_message") if c[1] == r.CHAT], "방에 문구도 안 보냄"
    bad = await r.db._one("SELECT score FROM nsfw_bad WHERE file_uid='p1'")
    assert bad and bad["score"] >= 0.7, "지문 학습"
    assert await r.db._one("SELECT 1 FROM mod_log WHERE chat_id=? AND action='nsfw_del'", (r.CHAT,)), "기록엔 남김"


@test
async def notify_mode_sends_buttons():
    r = await room(mode="notify")
    patch_scores(r, {MEMBER: 0.95})
    await send_media(r, MEMBER, file_uid="n0")
    d = dms(r, ADMIN)
    assert d and "음란물" in d[-1][2], "알림만 모드는 1:1 알림"
    cbs = buttons(d[-1])
    assert any(c.endswith(":b") for c in cbs) and any(c.endswith(":t") for c in cbs), cbs


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
async def only_owner_is_excluded():
    r = await room(mode="delete")
    r.svc.perms.owner_ids = {OWNER}
    await r.join(fake_user(OWNER, "오너"))
    patch_scores(r, {OWNER: 0.99})
    m = await send_media(r, OWNER, file_uid="own1", name="오너")
    assert not m.deleted, "오너(운영자)만 검사 제외"


@test
async def admin_media_is_now_checked():
    r = await room(mode="delete")
    patch_scores(r, {ADMIN: 0.99})
    m = await send_media(r, ADMIN, file_uid="adm1", name="방장")
    assert m.deleted, "관리자가 올려도 지움 (오너만 제외)"


@test
async def free_member_is_now_checked():
    r = await room(mode="delete")
    from sodam import free
    await free.add(r.db, r.CHAT, OTHER, ADMIN)
    patch_scores(r, {OTHER: 0.99})
    m = await send_media(r, OTHER, file_uid="fr1", name="딴사람")
    assert m.deleted, "자유 멤버가 올려도 지움 (오너만 제외)"


@test
async def false_positive_allowlist_stops_future_deletes():
    r = await room(mode="notify")   # 알림만 모드에서 [✅ 오탐] 버튼으로 통과시킴
    patch_scores(r, {MEMBER: 0.95, OTHER: 0.95})
    await send_media(r, MEMBER, file_uid="fp")
    d = dms(r, ADMIN)[-1]
    aid = int([c for c in buttons(d) if c.endswith(":t")][0].split(":")[3])
    await press(r, ADMIN, f"m:nsx:{r.CHAT}:{aid}:t")   # ✅ 오탐 (이 조각 통과)
    assert await r.db._one("SELECT 1 FROM nsfw_ok WHERE chat_id=? AND file_uid='fp'", (r.CHAT,))
    assert not await r.db._one("SELECT 1 FROM nsfw_bad WHERE file_uid='fp'"), "나쁨 기록도 지움"
    await r.db.set_setting(r.CHAT, "nsfw_mode", "delete")   # 이제 지우는 모드여도
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


@test
async def sticker_pack_blocked_after_two_pieces():
    r = await room(mode="delete")
    calls = patch_scores(r, {MEMBER: 0.95})
    await send_media(r, MEMBER, file_uid="cut1", set_name="bodypack")
    await send_media(r, MEMBER, file_uid="cut2", set_name="bodypack")   # 두 번째 → 팩 통째 차단
    assert await r.db._one("SELECT 1 FROM nsfw_set WHERE set_name='bodypack'"), "조각 2개 걸리면 팩 차단"
    n = len(calls)
    m3 = await send_media(r, OTHER, file_uid="cut3", set_name="bodypack", name="딴사람")   # 새 조각
    assert m3.deleted, "차단된 팩의 새 조각은 즉시 삭제"
    assert len(calls) == n, "팩 차단 조각은 AI 안 부름"


@test
async def one_piece_does_not_block_whole_pack():
    r = await room(mode="delete")
    patch_scores(r, {MEMBER: 0.95})
    await send_media(r, MEMBER, file_uid="only1", set_name="publicpack")
    assert not await r.db._one("SELECT 1 FROM nsfw_set WHERE set_name='publicpack'"), "조각 1개론 팩 차단 안 함(공개팩 오폭 방지)"


@test
async def admin_can_block_whole_pack_button():
    r = await room(mode="notify")   # 알림만 모드에서 [🧩 팩 차단] 버튼
    patch_scores(r, {MEMBER: 0.95})
    await send_media(r, MEMBER, file_uid="p1", set_name="nudeset")
    d = dms(r, ADMIN)[-1]
    sbtn = [c for c in buttons(d) if c.endswith(":s")]
    assert sbtn, "스티커 알림엔 [🧩 팩 차단] 버튼"
    aid = int(sbtn[0].split(":")[3])
    await press(r, ADMIN, f"m:nsx:{r.CHAT}:{aid}:s")
    assert await r.db._one("SELECT 1 FROM nsfw_set WHERE set_name='nudeset'"), "버튼으로 팩 차단"
    await r.db.set_setting(r.CHAT, "nsfw_mode", "delete")
    m2 = await send_media(r, OTHER, file_uid="p2", set_name="nudeset", name="딴사람")
    assert m2.deleted, "차단 팩 새 조각 즉시 삭제"


@test
async def mute_mode_auto_mutes_spammer():
    r = await room(mode="mute")
    patch_scores(r, {MEMBER: 0.97})
    m = await send_media(r, MEMBER, file_uid="mu1")
    assert m.deleted
    assert any(c[0] == "restrict" and c[2] == MEMBER for c in r.bot.calls), "mute 모드는 자동 뮤트"


@test
async def ban_mode_auto_bans_spammer():
    r = await room(mode="ban")
    patch_scores(r, {MEMBER: 0.97})
    m = await send_media(r, MEMBER, file_uid="bn1")
    assert m.deleted
    assert any(c[0] == "ban" and c[2] == MEMBER for c in r.bot.calls), "ban 모드는 자동 밴"


@test
async def no_auto_sanction_below_high_confidence():
    r = await room(mode="ban")
    patch_scores(r, {MEMBER: 0.72})   # 지우는 기준(0.70)은 넘지만 제재 기준(0.85)은 아래
    m = await send_media(r, MEMBER, file_uid="lo1")
    assert m.deleted, "0.70 넘으면 지움"
    assert not any(c[0] == "ban" for c in r.bot.calls), "확신 낮으면 자동 밴 안 함(오탐 방지)"


@test
async def skin_fragment_caught_even_below_threshold():
    r = await room(mode="delete")
    patch_scores(r, {MEMBER: 0.4})           # AI 는 0.4 (평소 기준 0.70 밑)
    nsfwguard._skin_ratio = lambda data: 0.8  # 화면 거의 살색 = 신체 조각
    m = await send_media(r, MEMBER, file_uid="frag1")
    assert m.deleted, "AI 가 애매해도 거의 살색이면 조각으로 보고 잡음"


@test
async def low_skin_fragment_not_caught():
    r = await room(mode="delete")
    patch_scores(r, {MEMBER: 0.4})           # AI 0.4 (애매) 이지만
    nsfwguard._skin_ratio = lambda data: 0.3  # 살색 적음 = 신체 조각 아님
    m = await send_media(r, MEMBER, file_uid="notfrag")
    assert not m.deleted, "살색 적으면 조각으로 안 봄(오탐 방지)"


@test
async def low_suspicion_high_skin_not_caught():
    r = await room(mode="delete")
    patch_scores(r, {MEMBER: 0.1})           # AI 가 거의 의심 안 함 (얼굴·손 등 평범한 살색)
    nsfwguard._skin_ratio = lambda data: 0.9
    m = await send_media(r, MEMBER, file_uid="face1")
    assert not m.deleted, "AI 가 의심 안 하면 살색만으론 안 지움(오탐 방지)"


@test
async def burst_lowers_threshold_for_mosaic():
    r = await room(mode="delete")
    patch_scores(r, {MEMBER: 0.5})   # 조각 하나는 0.5 (평소 기준 0.70 밑 → 안 걸림)
    first = await send_media(r, MEMBER, file_uid="mos1", set_name="mos")
    assert not first.deleted, "평소엔 0.5 통과"
    await send_media(r, MEMBER, file_uid="mos2", set_name="mos")
    await send_media(r, MEMBER, file_uid="mos3", set_name="mos")
    fourth = await send_media(r, MEMBER, file_uid="mos4", set_name="mos")   # 도배 → 기준 0.45
    assert fourth.deleted, "우르르 올리면 기준 낮춰 조각도 잡음"


if __name__ == "__main__":
    run_all()
