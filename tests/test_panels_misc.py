"""AI 패널·학습 자료·공통 자료·관리 기록·규칙·숫자 직접 입력·봇 관리자 관리: python tests/test_panels_misc.py"""
import asyncio
import sys
import time
from datetime import datetime
from types import SimpleNamespace

from fakes import FakeBot, FakeMsg, FakeQuery, cfg, fake_user, make_db, make_svc, runner

from sodam import knowledge, menu
from sodam.billing import Billing
from sodam.panels import ai as ai_panel

test, run_all = runner()
CHAT, OTHER = -1001111, -1002222
TGA, BOTADM, MEMBER, OWNER = 1, 30, 20, 7
PAY = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


async def _async(v):
    return v


async def setup():
    """uid 1 = CHAT 텔레그램 관리자, 30 = .봇관리자, 20 = 멤버, 7 = 봇 오너."""
    db = await make_db()
    svc = await make_svc(db)
    for cid, title in ((CHAT, "내 방"), (OTHER, "남의 방")):
        await db.ensure_chat(cid, title)
    state = SimpleNamespace(tg={TGA}, bot={BOTADM}, owners={OWNER}, forgets=0)
    svc.perms.is_admin = lambda bot, cid, uid: _async(uid in state.owners or (cid == CHAT and uid in state.tg | state.bot))
    svc.perms.is_tg_admin = lambda bot, cid, uid: _async(uid in state.owners or (cid == CHAT and uid in state.tg))
    svc.perms.owners = lambda: _async(set(state.owners))

    def forget(cid):
        state.forgets += 1
    svc.perms.forget = forget
    for uid, name, username in ((TGA, "방장", "boss"), (BOTADM, "부방장", "sub"), (MEMBER, "멤버", "member1"),
                                (OWNER, "오너", None)):
        await db.upsert_user(fake_user(uid, name, username))
        await db.touch_member(CHAT, uid)
    return db, svc, FakeBot(), state


def buttons(kb):
    return [b for row in kb.inline_keyboard for b in row] if kb else []


def find(kb, text):
    return next(b for b in buttons(kb) if text in b.text)


async def press(svc, bot, uid, data):
    q = FakeQuery(uid, fake_user(uid), data)
    await menu.on_callback(svc, bot, q, data.split(":")[1:])
    assert len(q.answers) == 1, (data, q.answers)
    return q


def denied(q):
    return not q.edits and q.answers[0][1]  # 알림창으로 거절, 화면은 그대로


async def dm(svc, bot, uid, text="", **kw):
    msg = FakeMsg(uid, fake_user(uid), text, **kw)
    assert await menu.handle_input(svc, bot, msg)
    return msg.replies


def with_billing(svc, db, **kw):
    svc.cfg = cfg(db.path, pay_address=PAY, sub_price_usdt="37", **kw)
    svc.billing = Billing(svc.cfg, db)


async def expire(db, cid):
    await db.start_subscription(cid, int(time.time()) - 86400, None)


async def add_docs(db, cid, n, prefix="자료"):
    return [(await knowledge.add_document(db, cid, f"{prefix} {i}", f"{prefix} {i} 내용입니다. " * 5, "메뉴 입력", 1))[0]
            for i in range(n)]


# ── 🤖 AI ────────────────────────────────────────────────
@test
async def ai_screen_toggle_presets_and_permissions():
    db, svc, bot, state = await setup()
    q = await press(svc, bot, TGA, f"m:g:{CHAT}")
    assert f"m:ai:{CHAT}" in [b.callback_data for b in buttons(q.kb)]
    q = await press(svc, bot, TGA, f"m:ai:{CHAT}")
    assert "AI 설정" in q.edits[-1] and "최대 400자" in q.edits[-1]
    assert find(q.kb, "말투").callback_data == f"m:st:{CHAT}"
    toggle = find(q.kb, "AI 대화")
    assert toggle.callback_data == f"m:ait:{CHAT}:0"
    for _ in range(2):                                               # 재전송해도 결과·기록 한 번
        q = await press(svc, bot, TGA, toggle.callback_data)
    assert (await db.get_settings(CHAT))["ai_enabled"] is False and "꺼짐" in q.answers[0][0]
    assert len(await db.recent_mod_log(CHAT)) == 1 and "AI 설정" in q.edits[-1]
    q = await press(svc, bot, TGA, f"m:ait:{CHAT}:x")                # 목표값 형식 틀림
    assert not q.edits and (await db.get_settings(CHAT))["ai_enabled"] is False

    q = await press(svc, bot, TGA, f"m:n:{CHAT}:reply_max_chars:800")
    assert (await db.get_settings(CHAT))["reply_max_chars"] == 800 and "AI 설정" in q.edits[-1]
    assert find(q.kb, "800자").text.startswith("●")
    await press(svc, bot, TGA, f"m:n:{CHAT}:user_rate_per_min:5")
    for bad in ("reply_max_chars:3500", "reply_max_chars:1", "user_rate_per_min:60"):  # 버튼에 없는 값
        q = await press(svc, bot, TGA, f"m:n:{CHAT}:{bad}")
        assert not q.edits, bad
    s = await db.get_settings(CHAT)
    assert (s["reply_max_chars"], s["user_rate_per_min"]) == (800, 5)
    # ai_enabled 를 기능 화면에서 바꾸면 여전히 기능 화면으로 돌아감 (AI 화면과 안 섞임)
    q = await press(svc, bot, TGA, f"m:t:{CHAT}:ai_enabled:1")
    assert "기능 켜기" in q.edits[-1]

    for uid in (MEMBER, 999):                                        # 멤버·모르는 사람
        for data in (f"m:ai:{CHAT}", f"m:ait:{CHAT}:0", f"m:n:{CHAT}:reply_max_chars:200"):
            assert denied(await press(svc, bot, uid, data)), (uid, data)
    assert denied(await press(svc, bot, TGA, f"m:ait:{OTHER}:0"))    # 남의 방 ID 위조
    assert (await db.get_settings(OTHER))["ai_enabled"] is True
    assert (await press(svc, bot, BOTADM, f"m:ai:{CHAT}")).edits      # 봇관리자는 AI 설정 가능
    state.bot.discard(BOTADM)                                        # 강등되면 바로 막힘
    assert denied(await press(svc, bot, BOTADM, f"m:ai:{CHAT}"))


@test
async def ai_usage_shows_count_never_price():
    db, svc, bot, _ = await setup()
    with_billing(svc, db, free_ai_per_day=10)
    await expire(db, CHAT)
    day = datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")
    await db.bump(day, CHAT, "free_ai", 4)
    await db.bump("2000-01-01", CHAT, "free_ai", 9)                  # 다른 날은 안 셈
    await db.bump(day, OTHER, "free_ai", 7)                          # 다른 방도 안 셈
    for uid in (TGA, BOTADM):
        q = await press(svc, bot, uid, f"m:ai:{CHAT}")
        text = q.edits[-1]
        assert "4 / 10회" in text, text
        for leak in ("USDT", "37", "$", PAY, "결제", "구독"):
            assert leak not in text, (leak, text)
        assert not any("💳" in b.text or "pay:" in (b.callback_data or "") for b in buttons(q.kb))
    await db.bump(day, CHAT, "free_ai", 20)                          # 한도를 넘게 셌어도 표시는 한도까지
    text = (await press(svc, bot, TGA, f"m:ai:{CHAT}")).edits[-1]
    assert "10 / 10회" in text and "다 썼어요" in text and "37" not in text
    await svc.billing.extend(CHAT, 30)                               # 이용 중이면 무료 한도 없음
    text = (await press(svc, bot, TGA, f"m:ai:{CHAT}")).edits[-1]
    assert "한도 없이" in text and "/ 10회" not in text


# ── 📚 학습 자료 ──────────────────────────────────────────
@test
async def kb_list_pagination_view_and_common_readonly():
    db, svc, bot, _ = await setup()
    ids = await add_docs(db, CHAT, ai_panel.PAGE + 3)
    other_id = (await add_docs(db, OTHER, 1, "남의자료"))[0]
    common_id, _, _ = await knowledge.add_document(db, 0, "공통 FAQ <b>", "모든 방 공통 안내입니다 & 끝.", "직접 입력", OWNER)
    q = await press(svc, bot, TGA, f"m:kb:{CHAT}")
    assert "1/2쪽" in q.edits[-1] and find(q.kb, "다음").callback_data == f"m:kb:{CHAT}:1"
    assert "남의자료" not in q.edits[-1]
    last = await press(svc, bot, TGA, f"m:kb:{CHAT}:1")
    assert "2/2쪽" in last.edits[-1] and "[공통]" in last.edits[-1] and "&lt;b&gt;" in last.edits[-1]
    for page in ("99", "-3", "abc", "²"):                            # 범위 밖·이상한 쪽 번호 → 가까운 쪽
        q = await press(svc, bot, TGA, f"m:kb:{CHAT}:{page}")
        assert "쪽)" in q.edits[-1], page
    assert "2/2쪽" in (await press(svc, bot, TGA, f"m:kb:{CHAT}:99")).edits[-1]
    assert "1/2쪽" in (await press(svc, bot, TGA, f"m:kb:{CHAT}:-3")).edits[-1]

    q = await press(svc, bot, TGA, f"m:kbv:{CHAT}:{ids[0]}")          # 앞부분 500자 보기
    assert "앞부분" in q.edits[-1] and any("삭제" in b.text for b in buttons(q.kb))
    q = await press(svc, bot, TGA, f"m:kbv:{CHAT}:{common_id}")       # 공통 자료: 보기만
    assert "공통 (모든 방)" in q.edits[-1] and not any("삭제" in b.text for b in buttons(q.kb))
    assert denied(await press(svc, bot, TGA, f"m:kbd:{CHAT}:{common_id}"))
    q = await press(svc, bot, TGA, f"m:kbv:{CHAT}:{other_id}")        # 남의 방 자료 ID 위조
    assert "남의자료" not in q.edits[-1] and "없는 자료" in q.answers[0][0]
    assert denied(await press(svc, bot, TGA, f"m:kbd:{CHAT}:{other_id}"))
    # 방 관리자가 공통 자료 ID 로 삭제 토큰을 만들어도(위조) 방 범위라서 안 지워짐
    tok = menu.token(svc, TGA, CHAT, "del_kb", common_id)
    await press(svc, bot, TGA, f"m:k:{tok}")
    assert await ai_panel._doc(svc, (0,), common_id)
    assert await ai_panel._doc(svc, (OTHER,), other_id)

    long_id, _, _ = await knowledge.add_document(db, CHAT, "긴 자료", "가" * 3000, "x.txt", 1)
    q = await press(svc, bot, TGA, f"m:kbv:{CHAT}:{long_id}")
    assert q.edits[-1].count("가") <= ai_panel.VIEW_CHARS and q.edits[-1].endswith("…")


@test
async def kb_delete_token_single_use_and_fresh():
    db, svc, bot, state = await setup()
    doc_id = (await add_docs(db, CHAT, 2))[0]
    q = await press(svc, bot, TGA, f"m:kbd:{CHAT}:{doc_id}")
    confirm = find(q.kb, "🗑 삭제")
    assert "삭제할까요" in q.edits[-1] and confirm.callback_data.startswith("m:k:")
    assert "만료" in (await press(svc, bot, MEMBER, confirm.callback_data)).answers[0][0]  # 다른 사람 → 소모
    assert await ai_panel._doc(svc, (CHAT,), doc_id)
    confirm = find((await press(svc, bot, TGA, f"m:kbd:{CHAT}:{doc_id}")).kb, "🗑 삭제")
    before = state.forgets
    q = await press(svc, bot, TGA, confirm.callback_data)
    assert "삭제했어요" in q.answers[0][0] and not await ai_panel._doc(svc, (CHAT,), doc_id)
    assert state.forgets == before + 1                               # 삭제는 권한을 새로 확인
    assert "만료" in (await press(svc, bot, TGA, confirm.callback_data)).answers[0][0]  # 재전송
    assert any(r["action"] == "knowledge_del" for r in await db.recent_mod_log(CHAT))

    other = (await add_docs(db, CHAT, 1, "남길자료"))[0]              # 확인 화면이 떠 있는 사이 강등
    confirm = find((await press(svc, bot, TGA, f"m:kbd:{CHAT}:{other}")).kb, "🗑 삭제")
    state.tg.discard(TGA)
    assert "관리자만" in (await press(svc, bot, TGA, confirm.callback_data)).answers[0][0]
    assert await ai_panel._doc(svc, (CHAT,), other)


class FileBot(FakeBot):
    def __init__(self, files):
        super().__init__()
        self.files = files

    async def get_file(self, file_id):
        data = self.files[file_id]

        async def download_as_bytearray():
            return bytearray(data)
        return SimpleNamespace(download_as_bytearray=download_as_bytearray)


@test
async def kb_add_by_text_and_file():
    db, svc, _, state = await setup()
    bot = FileBot({"f1": "파일 자료 내용입니다. 영업시간 안내.".encode(), "bad": b"\x00\xff" * 10})
    q = await press(svc, bot, TGA, f"m:in:{CHAT}:kb")
    assert "자료" in q.edits[-1] and svc.inputs[TGA].kind == "kb"
    assert "짧아요" in (await dm(svc, bot, TGA, "짧음"))[0] and TGA in svc.inputs   # 다시 받음
    assert "글이나 파일" in (await dm(svc, bot, TGA, photo=[SimpleNamespace(file_id="p")]))[0]
    replies = await dm(svc, bot, TGA, "가격표 <특가>\nA 상품은 1만원, B 상품은 2만원입니다.")
    assert "등록" in replies[0] and "&lt;특가&gt;" in replies[0] and TGA not in svc.inputs
    docs = await db.knowledge_docs(CHAT)
    assert [d["title"] for d in docs] == ["가격표 <특가>"] and await knowledge.search(db, CHAT, "상품 가격")

    await press(svc, bot, TGA, f"m:in:{CHAT}:kb")
    doc = SimpleNamespace(file_id="f1", file_name="안내.txt", file_size=100)
    assert "등록" in (await dm(svc, bot, TGA, document=doc))[0]
    assert "안내.txt" in [d["title"] for d in await db.knowledge_docs(CHAT)]
    await press(svc, bot, TGA, f"m:in:{CHAT}:kb")
    big = SimpleNamespace(file_id="f1", file_name="big.txt", file_size=knowledge.MAX_FILE_BYTES + 1)
    assert "너무 커요" in (await dm(svc, bot, TGA, document=big))[0]
    assert "지원하는 파일" in (await dm(svc, bot, TGA, document=SimpleNamespace(
        file_id="bad", file_name="x.exe", file_size=20)))[0]
    assert len(await db.knowledge_docs(CHAT)) == 2

    state.tg.discard(TGA)                                            # 입력 대기 중 강등
    assert "관리자만" in (await dm(svc, bot, TGA, "몰래 넣는 자료입니다 열 글자 넘게"))[0]
    assert len(await db.knowledge_docs(CHAT)) == 2
    assert denied(await press(svc, bot, MEMBER, f"m:in:{CHAT}:kb"))

    state.tg.add(TGA)                                                # 이용 기간이 끝난 방: 추가 안 됨 (금액 안내 없음)
    with_billing(svc, db)
    await expire(db, CHAT)
    q = await press(svc, bot, TGA, f"m:kb:{CHAT}")
    assert not any("추가" in b.text for b in buttons(q.kb)) and "37" not in q.edits[-1]
    await press(svc, bot, TGA, f"m:in:{CHAT}:kb")
    reply = (await dm(svc, bot, TGA, "만료된 방에서 넣는 자료입니다 열 글자"))[0]
    assert "이용 기간" in reply and "USDT" not in reply and len(await db.knowledge_docs(CHAT)) == 2


@test
async def common_docs_owner_only():
    db, svc, bot, state = await setup()
    cid, _, _ = await knowledge.add_document(db, 0, "공통 FAQ", "모든 방 공통 안내입니다 끝.", "직접 입력", OWNER)
    await add_docs(db, CHAT, 1)
    text, kb = await menu.main_menu(svc, bot, OWNER)
    assert any(b.callback_data == "m:ckb" for b in buttons(kb))
    text, kb = await menu.main_menu(svc, bot, TGA)
    assert not any(b.callback_data == "m:ckb" for b in buttons(kb))
    for uid in (TGA, BOTADM, MEMBER):                                # 방 관리자도 못 봄 (위조 콜백 포함)
        for data in ("m:ckb", f"m:ckbv:{cid}", f"m:ckbd:{cid}", "m:ckb:5"):
            assert denied(await press(svc, bot, uid, data)), (uid, data)
    q = await press(svc, bot, OWNER, "m:ckb")
    assert "공통 FAQ" in q.edits[-1] and "자료 0" not in q.edits[-1]
    q = await press(svc, bot, OWNER, f"m:ckbv:{cid}")
    assert "앞부분" in q.edits[-1]
    room_doc = next(d["id"] for d in await db.knowledge_docs(CHAT) if d["chat_id"] == CHAT)
    q = await press(svc, bot, OWNER, f"m:ckbv:{room_doc}")           # 방 자료는 공통 화면에서 안 보임
    assert "없는 자료" in q.answers[0][0]
    confirm = find((await press(svc, bot, OWNER, f"m:ckbd:{cid}")).kb, "🗑 삭제")
    state.owners.discard(OWNER)                                      # 오너 해제 뒤에는 토큰도 무효
    assert denied(await press(svc, bot, OWNER, confirm.callback_data))
    state.owners.add(OWNER)
    confirm = find((await press(svc, bot, OWNER, f"m:ckbd:{cid}")).kb, "🗑 삭제")
    q = await press(svc, bot, OWNER, confirm.callback_data)
    assert "삭제했어요" in q.answers[0][0] and not await ai_panel._doc(svc, (0,), cid)
    assert await ai_panel._doc(svc, (CHAT,), room_doc)
    assert "만료" in (await press(svc, bot, OWNER, confirm.callback_data)).answers[0][0]


# ── 🗂️ 관리 기록 ─────────────────────────────────────────
@test
async def mod_log_pages_and_escaping():
    from sodam.panels import log as log_panel
    db, svc, bot, _ = await setup()
    for i in range(23):
        await db.log_mod(CHAT, TGA, MEMBER, "warn", f"사유 {i} <script>&")
    await db.log_mod(CHAT, None, None, "setting", "style=free")
    await db.log_mod(CHAT, TGA, 55555, "ban", "")
    await db.log_mod(OTHER, TGA, None, "setting", "남의방기록=1")
    q = await press(svc, bot, TGA, f"m:log:{CHAT}")
    text = q.edits[-1]
    assert "1/3쪽" in text and "총 25건" in text and "남의방기록" not in text
    assert "ID 55555" in text and "🤖 소담" in text and "기본 말투 → 자유분방" in text
    assert "&lt;script&gt;&amp;" in text and "<script>" not in text
    assert text.count("⚠️ 경고") + text.count("⛔ 밴") + text.count("⚙️ 설정") == log_panel.PAGE
    q = await press(svc, bot, TGA, f"m:log:{CHAT}:2")
    assert "3/3쪽" in q.edits[-1] and q.edits[-1].count("⚠️ 경고") == 5
    assert not any("지난 기록" in b.text for b in buttons(q.kb))
    for page in ("99", "-1", "x", "²", "99999999999999999999999"):
        assert "쪽" in (await press(svc, bot, TGA, f"m:log:{CHAT}:{page}")).edits[-1], page
    assert (await press(svc, bot, BOTADM, f"m:log:{CHAT}")).edits
    assert denied(await press(svc, bot, MEMBER, f"m:log:{CHAT}"))
    assert denied(await press(svc, bot, TGA, f"m:log:{OTHER}"))
    db2, svc2, bot2, _ = await setup()
    assert "아직 기록이 없어요" in (await press(svc2, bot2, TGA, f"m:log:{CHAT}")).edits[-1]
    long = "가" * 300
    assert log_panel.describe("warn", long) == long                  # 자르기는 화면에서
    await db2.log_mod(CHAT, TGA, None, "warn", long)
    assert "가" * 81 not in (await press(svc2, bot2, TGA, f"m:log:{CHAT}")).edits[-1]


# ── 📜 규칙 ───────────────────────────────────────────────
@test
async def rules_edit_validate_and_delete():
    db, svc, bot, state = await setup()
    q = await press(svc, bot, TGA, f"m:rules:{CHAT}")
    assert "아직 없어요" in q.edits[-1] and not any("삭제" in b.text for b in buttons(q.kb))
    await press(svc, bot, TGA, find(q.kb, "규칙 쓰기").callback_data)
    assert svc.inputs[TGA].kind == "rules"
    assert "2000자" in (await dm(svc, bot, TGA, "가" * 2001))[0] and TGA in svc.inputs
    replies = await dm(svc, bot, TGA, "1. 욕설 금지 <b>\n2. 광고 & 홍보 금지")
    assert "저장" in replies[0] and "&lt;b&gt;" in replies[0] and "광고 &amp; 홍보" in replies[0]
    assert (await db.get_settings(CHAT))["rules"].startswith("1. 욕설 금지 <b>")
    q = await press(svc, bot, TGA, f"m:rules:{CHAT}")
    q = await press(svc, bot, TGA, find(q.kb, "삭제").callback_data)
    confirm = find(q.kb, "🗑 삭제")
    assert "삭제할까요" in q.edits[-1]
    assert denied(await press(svc, bot, MEMBER, f"m:rulesx:{CHAT}"))
    assert "만료" in (await press(svc, bot, MEMBER, confirm.callback_data)).answers[0][0]
    confirm = find((await press(svc, bot, TGA, f"m:rulesx:{CHAT}")).kb, "🗑 삭제")
    before = state.forgets
    q = await press(svc, bot, TGA, confirm.callback_data)
    assert (await db.get_settings(CHAT))["rules"] == "" and state.forgets == before + 1
    assert "만료" in (await press(svc, bot, TGA, confirm.callback_data)).answers[0][0]
    assert [r["action"] for r in await db.recent_mod_log(CHAT)][:2] == ["rules", "rules"]
    assert denied(await press(svc, bot, TGA, f"m:rules:{OTHER}"))


# ── ✏️ 숫자 직접 입력 ─────────────────────────────────────
@test
async def number_input_whitelist_and_range():
    db, svc, bot, state = await setup()
    for code, key in (("sec", "flood_count"), ("wl", "warn_ban_at"), ("ai", "reply_max_chars")):
        q = await press(svc, bot, TGA, f"m:{code}:{CHAT}")
        btn = find(q.kb, "숫자 직접 입력")
        assert btn.callback_data == f"m:num:{CHAT}:{code}" and buttons(q.kb)[-1].text.startswith("⬅️")
        q = await press(svc, bot, TGA, btn.callback_data)
        assert f"m:numi:{CHAT}:{key}" in [b.callback_data for b in buttons(q.kb)]
    q = await press(svc, bot, TGA, f"m:fl:{CHAT}:strict")            # 도배 프리셋 뒤에도 버튼 유지
    assert any("숫자 직접 입력" in b.text for b in buttons(q.kb))
    q = await press(svc, bot, TGA, f"m:t:{CHAT}:link_filter:0")      # 토글 뒤 다시 그린 화면에도
    assert any("숫자 직접 입력" in b.text for b in buttons(q.kb))

    for bad in ("rules", "captcha_minutes", "style", "ai_enabled", "", "whitelist_domains"):
        q = await press(svc, bot, TGA, f"m:numi:{CHAT}:{bad}")
        assert not q.edits and TGA not in svc.inputs, bad
    q = await press(svc, bot, TGA, f"m:numi:{CHAT}:flood_count")
    assert "2 ~ 100" in q.edits[-1] and svc.inputs[TGA].args == ["flood_count"]
    for raw, err in (("abc", "숫자로"), ("1", "2 ~ 100"), ("101", "2 ~ 100"), ("²", "숫자로"), ("", "글자로")):
        replies = await dm(svc, bot, TGA, raw)
        assert err in replies[0] and TGA in svc.inputs, raw
    replies = await dm(svc, bot, TGA, "12")
    assert "12개" in replies[0] and "보안" in replies[0] and TGA not in svc.inputs
    assert (await db.get_settings(CHAT))["flood_count"] == 12

    await press(svc, bot, TGA, f"m:numi:{CHAT}:warn_mute_minutes")
    await dm(svc, bot, TGA, "1,440")
    assert (await db.get_settings(CHAT))["warn_mute_minutes"] == 1440

    # 멤버·위조 방·강등
    assert denied(await press(svc, bot, MEMBER, f"m:numi:{CHAT}:flood_count"))
    assert denied(await press(svc, bot, TGA, f"m:numi:{OTHER}:flood_count"))
    assert denied(await press(svc, bot, MEMBER, f"m:num:{CHAT}"))
    await press(svc, bot, TGA, f"m:numi:{CHAT}:dup_limit")
    state.tg.discard(TGA)
    assert "관리자만" in (await dm(svc, bot, TGA, "7"))[0]
    assert (await db.get_settings(CHAT))["dup_limit"] == 3
    # 입력 흐름 kind 'num' 을 m:in 으로 억지로 시작해도 아무것도 못 바꿈
    state.tg.add(TGA)
    await press(svc, bot, TGA, f"m:in:{CHAT}:num")
    await dm(svc, bot, TGA, "9")
    assert (await db.get_settings(CHAT))["dup_limit"] == 3
    q = await press(svc, bot, TGA, f"m:num:{CHAT}")                  # 취소·시간초과 뒤 '메뉴로' 가 여는 화면
    assert q.edits and len(buttons(q.kb)) > 8


# ── 🛠️ 봇 관리자 관리 ────────────────────────────────────
@test
async def bot_admin_management_tg_admin_only():
    db, svc, bot, state = await setup()
    await db.set_bot_admin(CHAT, BOTADM, True)
    q = await press(svc, bot, TGA, f"m:g:{CHAT}")
    assert any(b.callback_data == f"m:badm:{CHAT}" for b in buttons(q.kb))
    q = await press(svc, bot, BOTADM, f"m:g:{CHAT}")                 # 봇관리자에겐 버튼 없음
    assert not any("봇 관리자" in b.text for b in buttons(q.kb))
    for data in (f"m:badm:{CHAT}", f"m:in:{CHAT}:badm"):             # 직접 눌러도 거절
        assert denied(await press(svc, bot, BOTADM, data)), data
    assert BOTADM not in svc.inputs
    tok = menu.token(svc, BOTADM, CHAT, "del_badm", BOTADM)          # 토큰을 손에 넣어도 TG_ADMIN 확인
    assert denied(await press(svc, bot, BOTADM, f"m:k:{tok}"))
    assert BOTADM in await db.bot_admin_ids(CHAT)

    q = await press(svc, bot, TGA, f"m:badm:{CHAT}")
    assert "부방장" in q.edits[-1] and f"<code>{BOTADM}</code>" in q.edits[-1]
    await press(svc, bot, TGA, f"m:in:{CHAT}:badm")
    assert "못 찾았어요" in (await dm(svc, bot, TGA, "@nobody"))[0] and TGA in svc.inputs
    assert "양수" in (await dm(svc, bot, TGA, "-5"))[0]
    assert "봇 자신" in (await dm(svc, bot, TGA, str(bot.id)))[0]
    assert "텔레그램 관리자" in (await dm(svc, bot, TGA, "@boss"))[0]  # 이미 TG 관리자 → 추가 안 함
    assert await db.bot_admin_ids(CHAT) == {BOTADM}
    await press(svc, bot, TGA, f"m:in:{CHAT}:badm")
    replies = await dm(svc, bot, TGA, "@MEMBER1")                    # 대소문자 무시
    assert "추가했어요" in replies[0] and await db.bot_admin_ids(CHAT) == {BOTADM, MEMBER}
    await press(svc, bot, TGA, f"m:in:{CHAT}:badm")
    assert "추가했어요" in (await dm(svc, bot, TGA, "424242"))[0]      # 숫자 ID 는 처음 보는 사람도
    assert 424242 in await db.bot_admin_ids(CHAT)
    assert "ID 424242" in (await press(svc, bot, TGA, f"m:badm:{CHAT}")).edits[-1]

    q = await press(svc, bot, TGA, f"m:badm:{CHAT}")                 # 빼기: 목록 토큰 → 확인 토큰(새로 확인)
    ask = find(q.kb, "멤버")
    assert ask.callback_data.startswith("m:k:") and str(MEMBER) not in ask.callback_data
    q = await press(svc, bot, TGA, ask.callback_data)
    confirm = find(q.kb, "빼기")
    before = state.forgets
    q = await press(svc, bot, TGA, confirm.callback_data)
    assert MEMBER not in await db.bot_admin_ids(CHAT) and state.forgets == before + 1
    assert "만료" in (await press(svc, bot, TGA, confirm.callback_data)).answers[0][0]
    log = await db.recent_mod_log(CHAT)
    assert [r["action"] for r in log[:3]] == ["bot_admin"] * 3

    q = await press(svc, bot, TGA, f"m:badm:{CHAT}")                 # 확인 화면에서 강등되면 못 뺌
    confirm = find((await press(svc, bot, TGA, find(q.kb, "부방장").callback_data)).kb, "빼기")
    state.tg.discard(TGA)
    assert denied(await press(svc, bot, TGA, confirm.callback_data))
    assert BOTADM in await db.bot_admin_ids(CHAT)
    state.tg.add(TGA)
    await press(svc, bot, TGA, f"m:in:{CHAT}:badm")                  # 입력 대기 중 강등
    state.tg.discard(TGA)
    await dm(svc, bot, TGA, "@member1")
    assert MEMBER not in await db.bot_admin_ids(CHAT)
    assert denied(await press(svc, bot, OWNER + 1000, f"m:badm:{CHAT}"))


@test
async def all_new_callbacks_fit_64_bytes():
    db, svc, bot, _ = await setup()
    long = -1009999999999999999
    await db.ensure_chat(long, "긴 방")
    svc.perms.is_admin = lambda bot, cid, uid: _async(True)
    svc.perms.is_tg_admin = lambda bot, cid, uid: _async(True)
    await db.set_setting(long, "rules", "규칙")
    await db.set_bot_admin(long, 123456789012, True)
    ids = await add_docs(db, long, ai_panel.PAGE + 1, "가" * 60)
    for data in ("ai", "kb", "kb:1", f"kbv:{ids[-1]}", f"kbd:{ids[-1]}", "rules", "rulesx", "log", "num",
                 "num:ai", "numi:flood_mute_minutes", "badm", "sec", "wl"):
        q = await press(svc, bot, 1, f"m:{data.split(':')[0]}:{long}" + (":" + data.split(":", 1)[1] if ":" in data else ""))
        assert q.edits, data
        for b in buttons(q.kb):
            assert len((b.callback_data or "").encode()) <= 64, b.callback_data


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
