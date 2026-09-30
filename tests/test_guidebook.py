"""📘 소담 공식 안내서 + sodam_guide (실제 사례 2026-09-29 벳블리 '결제하면 얼마야?' → '가격 자료가 없어')."""
import re
from pathlib import Path
from types import SimpleNamespace

from fakes import FakeBot, fake_user, make_db, make_svc, runner

import sodam.panels  # noqa: F401
from sodam import prompt, tools
from sodam.panels import guidebook as G
from sodam.permissions import Role
from sodam.tools import ToolCtx

test, run_all = runner()
CHAT = -100555


def ctx(svc, bot, chat):
    return ToolCtx(svc, bot, chat, fake_user(5, "테드정"), Role.MEMBER, {})


CFG = SimpleNamespace(sub_price_usdt="30", sub_days=30, trial_days=3, free_ai_per_day=10, invoice_minutes=60)
PLACEHOLDERS = {"price", "days", "trial", "free_ai", "invoice_min", "voice_min", "call_min", "video_weekly", "video_sec"}


@test
async def guide_docs_follow_the_writing_rules():
    """조사 2026-09-29 (Diátaxis·llms.txt·토스/카카오): 한 질문 = 한 문서, 한 줄 답 맨 위, 버튼은 화면 글자 그대로, 숫자는 설정값."""
    docs = G.load()
    assert {"index", "pricing", "payment", "invite", "admin-menu", "features", "ai-chat", "modes", "memory", "voice",
            "lookup", "games", "schedule", "alerts", "security", "commands", "privacy", "faq", "news"} <= set(docs)
    src = "".join(p.read_text(encoding="utf-8") for p in Path(G.__file__).parents[1].rglob("*.py"))
    seen_tags = {}
    for k, d in docs.items():
        assert d["title"] and d["summary"] and d["tags"] and d["related"] and d["audience"], k
        assert all(r in docs for r in d["related"]), (k, d["related"])          # 그래프 끊긴 곳 없음
        body = G.render(G.for_ai(d["body"]), CFG)
        assert not re.search(r"\{\w+\}", body + G.render(d["summary"], CFG)), k
        assert set(re.findall(r"\{(\w+)\}", d["body"])) <= PLACEHOLDERS, k
        assert len(body) <= 1500, (k, len(body))
        assert k in ("index", "faq") or body.startswith("## 한 줄 답"), k
        assert "관련 문서" not in body and ".md)" not in body, k                # 사람용 링크는 AI 에 안 감
        raw = d["body"] + d["summary"]
        assert not re.search(r"\d+\s*USDT|\d+일 무료|하루 \d+번 무료", raw), f"{k}: 숫자는 {{price}} 같은 자리표시자로"
        assert not re.search(r"T[1-9A-HJ-NP-Za-km-z]{33}|https?://|t\.me/", raw), f"{k}: 주소·링크 금지"
        for label in re.findall(r"\[([^\]\n]+)\](?!\()", raw):            # 버튼 글자가 코드에 실제로 있어야
            for part in re.split(r"\{\w+\}", label):
                assert part.strip() in src, f"{k}: 코드에 없는 버튼 [{label}]"
        for t in d["tags"]:
            assert t not in seen_tags, f"태그 '{t}' 가 {seen_tags.get(t)}·{k} 둘에 (엉뚱한 문서가 뽑힘)"
            seen_tags[t] = k
    index = docs["index"]["body"]
    assert all(f"({k}.md)" in index for k in docs if k != "index"), "목차에서 모든 문서로 갈 수 있어야"
    body = G.render(docs["pricing"]["body"], CFG)
    assert "30일(1달) 30 USDT" in body and "구독하기" in body and "처음 넣은 방만 3일" in body


# 실제 질문 → 문서 평가표 (문서 고칠 때마다 이게 지켜져야 함)
EVAL = [("소담아 근데 너 결제하면 얼마야?", {"pricing", "payment"}), ("한달에 얼마임", {"pricing"}),
        ("구독 끝나면 어떻게 돼", {"pricing"}), ("무료체험 며칠이야", {"pricing"}), ("구독 어떻게 해", {"payment"}),
        ("입금했는데 확인이 안돼", {"payment"}), ("연장하려면", {"payment"}), ("우리방에도 데려가려면 어떻게 해", {"invite"}),
        ("다른방에 넣으려면", {"invite"}), ("권한 뭐 줘야돼", {"invite"}), ("설정 어디서 해", {"admin-menu"}),
        ("너 뭐할수있어", {"features"}), ("왜 답안해", {"ai-chat"}), ("어떻게 부르면 돼", {"ai-chat"}),
        ("욕 받아치기 모드 어떻게 켜", {"modes"}), ("여친 말투로 바꾸는법", {"modes"}), ("19금 드립 켜줘", {"modes"}),
        ("내 기억 지우는법", {"memory"}), ("학습자료 추가 어떻게", {"memory"}), ("음성방 들어올수있어?", {"voice"}),
        ("이 사람 예전이름 어떻게 봐", {"lookup"}), ("끝말잇기 어떻게 해", {"games"}), ("포인트 돈으로 바꿀수있어?", {"games"}),
        ("매일 9시에 공지 예약하는법", {"schedule"}), ("태그알림 끄는법", {"alerts"}), ("태그 알림 말로 끌 수 있어?", {"alerts"}), ("소담아 출석 말로 해도 돼?", {"games"}), ("캡차 끄는법", {"security"}),
        ("뮤트 어떻게 해", {"security"}), ("명령어 목록", {"commands"}), ("대화 기록 며칠 저장해", {"privacy"}),
        ("소담아 오늘 EPL 경기 뭐 있어?", {"sports"}), ("스포츠 알림 어떻게 받아", {"sports"}), ("야구 순위 보는법", {"sports"}),
        ("너 사람이야?", {"faq"}), ("소담아 오늘 세계 뉴스 뭐 있어?", {"news"}),
        ("뉴스 알림 켜는법", {"news"}), ("속보 알림 끄고 싶어", {"news"}), ("코인 뉴스도 받을 수 있어?", {"news"}), ("누가 만들었어", {"faq"}), ("modes", {"modes"}), ("ai-chat", {"ai-chat"})]
# 관리자 말로 하는 관리 (tests/test_admin_nl.py, 2026-09-30)
EVAL += [("밴해제 어떻게 해", {"security"}), ("경고 취소 어떻게 해", {"security"}), ("소담아 영희 밴 풀어줘", {"security"}),
         ("예약 끄는법", {"schedule"}), ("예약목록 보는법", {"schedule"})]
# AI 영상 만들기 (tests/test_videogen.py, 2026-09-30)
EVAL += [("소담아 영상 만들어줄 수 있어?", {"video"}), ("동영상 생성 한 주에 몇 개야", {"video"}), ("이 사진 움직이는 영상으로 돼?", {"video"}),
         ("영상 몇개까지 만들 수 있어", {"video"})]


@test
async def questions_find_the_right_topic():
    wrong = [(q, G.find(q)) for q, want in EVAL if G.find(q) not in want]
    assert not wrong, wrong


@test
async def unknown_topic_lists_summaries_and_ties_show_the_other_topic():
    db = await make_db()
    svc = await make_svc(db, sub_price_usdt="30", sub_days=30, trial_days=3)
    bot = FakeBot()
    out = await G.t_sodam_guide(ctx(svc, bot, 5), {"topic": "ㅁㄴㅇㄹ"})
    assert "- pricing: 방 하나당 30일(1달) 30 USDT" in out and "- index" not in out, out
    assert (await G.t_sodam_guide(ctx(svc, bot, 5), {"topic": "index"})).startswith("공식 안내서 주제"), "목차 = 요약 목록"
    out = await G.t_sodam_guide(ctx(svc, bot, 5), {"topic": "결제하면 얼마야"})
    assert "비슷한 주제" in out and ("pricing:" in out or "payment:" in out), out
    out = await G.t_sodam_guide(ctx(svc, bot, 5), {"topic": "invite"})
    assert "(대상: 방 관리자)" in out and "pricing(요금·무료 체험)" in out and ".md" not in out, out
    assert G.for_ai("- [요금](pricing.md): 설명\n\n## 관련 문서\n- [x](faq.md)") == "- 요금(pricing): 설명"


@test
async def tool_answers_price_from_config_and_sends_buttons_once_in_room_only():
    db = await make_db()
    svc = await make_svc(db, sub_price_usdt="30", sub_days=30, trial_days=3)
    bot = FakeBot()
    G._card_sent.clear()
    out = await G.t_sodam_guide(ctx(svc, bot, CHAT), {"topic": "결제하면 얼마야"})
    assert "30 USDT" in out and "버튼 카드" in out, out
    [(_, cid, _text, kw)] = bot.named("send_message")
    urls = [b.url for row in kw["reply_markup"].inline_keyboard for b in row]
    assert cid == CHAT and any("startgroup" in u for u in urls) and any(f"start=sub_{CHAT}" in u for u in urls)
    await G.t_sodam_guide(ctx(svc, bot, CHAT), {"topic": "pricing"})
    assert len(bot.named("send_message")) == 1, "같은 방 10분 안엔 카드 1번"
    await G.t_sodam_guide(ctx(svc, bot, 5), {"topic": "pricing"})
    assert len(bot.named("send_message")) == 1, "1:1 엔 카드 안 보냄"
    assert "sodam_guide" in {t.name for t in tools.available(Role.MEMBER, {}, False)} and "sodam_guide" in tools.READ_ONLY
    assert "sodam_guide" in prompt.SYSTEM


if __name__ == "__main__":
    run_all()
