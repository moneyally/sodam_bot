"""🌐 번역 브리지 (sodam/translate.py). python tests/run_all.py translate

2026-11-22 경쟁조사. 한국인 多·외국인 少 → 오탐(한국어/짧은영어를 번역)이 제일 위험, 거기 집중."""
from types import SimpleNamespace

from fake_llm import Room, reply
from fakes import runner

from sodam import translate
from sodam.permissions import Role

test, run_all = runner()
CHAT = -1007777


def fmsg(text, uid=20):
    return SimpleNamespace(chat_id=CHAT, from_user=SimpleNamespace(id=uid, first_name="John"),
                           message_id=5, text=text)


@test
def should_try_gate():
    # 번역 안 할 것 (오탐 방지 — 한국인이 흔히 쓰는 것들)
    for t in ["안녕하세요 반갑습니다", "ㅋㅋㅋ 그래", "ok", "gg", "hi there", "좋아요 nice",
              "123 456", "https://a.com/x", "@user123", ".시세", "!바카라 10000",
              # 한글 위주인데 영어 낱말 섞인 글 (한글 비율 게이트가 잡아야 — 영어 게이트는 못 막음)
              "오늘 회의실에서 모여서 저녁 먹었는데 payment deposit casino 얘기 나와서 복잡했어요"]:
        assert not translate.should_try(t), f"오탐: {t!r}"
    # 번역할 외국어
    for t in ["How much is the deposit for this room", "コードを教えてください",
              "привет как дела друзья", "我想了解充值优惠活动详情"]:
        assert translate.should_try(t), f"놓침: {t!r}"


@test
async def handle_translates_foreign():
    r = await Room().open()
    await r.db.ensure_chat(CHAT, "방")
    await r.db.set_setting(CHAT, "translate_mode", "on")
    r.llm.script = [reply("이 방 보증금 얼마예요?")]
    await translate.handle(r.svc, r.bot, fmsg("How much is the deposit for this room"), Role.MEMBER)
    assert any("보증금" in str(c) and "🌐" in str(c) for c in r.bot.calls), r.bot.calls


@test
async def handle_off_and_korean_never_call_ai():
    r = await Room().open()
    await r.db.ensure_chat(CHAT, "방")
    # 꺼진 방 → 번역 안 함 (AI 도 안 부름)
    await r.db.set_setting(CHAT, "translate_mode", "off")
    r.llm.script = [reply("x")]
    await translate.handle(r.svc, r.bot, fmsg("How much is the deposit for this room"), Role.MEMBER)
    assert not r.bot.calls and not r.llm.of("chat")
    # 켜도 한국어는 1층에서 걸러 AI 안 부름
    await r.db.set_setting(CHAT, "translate_mode", "on")
    await translate.handle(r.svc, r.bot, fmsg("안녕 다들 오늘 날씨 좋네요 반가워요"), Role.MEMBER)
    assert not r.bot.calls and not r.llm.of("chat")


@test
async def handle_skip_result_not_sent():
    r = await Room().open()
    await r.db.ensure_chat(CHAT, "방")
    await r.db.set_setting(CHAT, "translate_mode", "on")
    r.llm.script = [reply("__SKIP__")]          # AI 가 '번역 불필요' 판정
    await translate.handle(r.svc, r.bot, fmsg("How much is the deposit for this room"), Role.MEMBER)
    assert not any("🌐" in str(c) for c in r.bot.calls)


@test
async def daily_cap_stops_translation():
    r = await Room().open()
    await r.db.ensure_chat(CHAT, "방")
    await r.db.set_setting(CHAT, "translate_mode", "on")
    await r.db.set_setting(CHAT, "translate_daily", 1)
    r.llm.script = [reply("번역1"), reply("번역2")]
    await translate.handle(r.svc, r.bot, fmsg("How much is the deposit for this room"), Role.MEMBER)
    await translate.handle(r.svc, r.bot, fmsg("Where is the payment link please help"), Role.MEMBER)
    sent = [c for c in r.bot.calls if "🌐" in str(c)]
    assert len(sent) == 1, f"하루 1개 한도인데 {len(sent)}개"
