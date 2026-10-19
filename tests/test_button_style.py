"""인라인 버튼 색 자동 입히기 (Bot API 9.4 style) — python tests/run_all.py button_style

- 글자/콜백으로 색 판정: 밴→danger · 결제→success · 조회→primary · 뒤로→기본(None)
- 이미 지정된 색은 존중(브랜드·테마) · install() 이 InlineKeyboardMarkup 전역에 적용
- 콜백·글자·64바이트 불변(모양만 추가)
"""
import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from fakes import runner

from sodam import btnstyle

test, run_all = runner()


@test
async def words_decide_color():
    assert btnstyle.style_for(InlineKeyboardButton("🚫 밴", callback_data="x")) == btnstyle.DANGER
    assert btnstyle.style_for(InlineKeyboardButton("🗑 삭제", callback_data="x")) == btnstyle.DANGER
    assert btnstyle.style_for(InlineKeyboardButton("💳 구독하기", callback_data="x")) == btnstyle.SUCCESS
    assert btnstyle.style_for(InlineKeyboardButton("✅ 확인", callback_data="x")) == btnstyle.SUCCESS
    assert btnstyle.style_for(InlineKeyboardButton("🔎 조회", callback_data="x")) == btnstyle.PRIMARY
    assert btnstyle.style_for(InlineKeyboardButton("📄 원글 보기", callback_data="x")) == btnstyle.PRIMARY
    assert btnstyle.style_for(InlineKeyboardButton("⬅️ 뒤로", callback_data="x")) is None
    assert btnstyle.style_for(InlineKeyboardButton("닫기", callback_data="x")) is None


@test
async def prefix_backs_up_when_no_word():
    # 글자에 단서 없고 콜백 접두어가 제재 계열이면 danger
    assert btnstyle.style_for(InlineKeyboardButton("1일", callback_data="fbx:1:2:b")) == btnstyle.DANGER


@test
async def danger_wins_over_success_word():
    # 위험이 제일 세다: '구독 취소'는 취소(danger)
    assert btnstyle.style_for(InlineKeyboardButton("구독 취소", callback_data="x")) == btnstyle.DANGER


@test
async def explicit_style_is_kept():
    b = InlineKeyboardButton("밴", callback_data="x", style=btnstyle.SUCCESS)   # 일부러 초록
    out = btnstyle.apply_rows([[b]])[0][0]
    assert out.style == btnstyle.SUCCESS, "지정된 색은 안 바뀜"


@test
async def apply_fills_and_keeps_fields():
    rows = [[InlineKeyboardButton("💳 결제", callback_data="pay:new"),
             InlineKeyboardButton("뒤로", callback_data="m:g")],
            [InlineKeyboardButton("사이트", url="https://t.me/x")]]
    out = btnstyle.apply_rows(rows)
    assert out[0][0].style == btnstyle.SUCCESS and out[0][0].callback_data == "pay:new"
    assert out[0][1].style is None and out[0][1].text == "뒤로"
    # url 버튼도 글자로 색(없으면 그대로), url 유지
    assert out[1][0].url == "https://t.me/x"


@test
async def install_colors_every_markup():
    btnstyle.install()
    try:
        mk = InlineKeyboardMarkup([[InlineKeyboardButton("🚫 밴", callback_data="x"),
                                    InlineKeyboardButton("✅ 확인", callback_data="y")]])
        d = mk.to_dict()["inline_keyboard"][0]
        assert d[0]["style"] == "danger" and d[0]["callback_data"] == "x"
        assert d[1]["style"] == "success"
        # 두 번 만들어도 안 꼬임(색 있으면 존중)
        mk2 = InlineKeyboardMarkup(mk.inline_keyboard)
        assert mk2.to_dict()["inline_keyboard"][0][0]["style"] == "danger"
    finally:
        btnstyle.uninstall()
    # uninstall 뒤엔 색 안 붙음
    mk3 = InlineKeyboardMarkup([[InlineKeyboardButton("🚫 밴", callback_data="x")]])
    assert "style" not in mk3.to_dict()["inline_keyboard"][0][0]


@test
async def composer_themes():
    from sodam import composer
    rows = [[{"t": "💳 구독하기", "u": "https://t.me/a"}, {"t": "문의", "u": "https://t.me/b"}]]
    # 신호등(기본): 글자 뜻 → 구독=success, 문의=파랑(기본 fallback)
    mk = composer.markup(rows, "signal").to_dict()["inline_keyboard"][0]
    assert mk[0]["style"] == "success" and mk[1]["style"] == "primary"
    # 파랑: 전부 primary
    mk = composer.markup(rows, "blue").to_dict()["inline_keyboard"][0]
    assert mk[0]["style"] == "primary" and mk[1]["style"] == "primary"
    # 무채색: 색 없음
    mk = composer.markup(rows, "mono").to_dict()["inline_keyboard"][0]
    assert "style" not in mk[0] and "style" not in mk[1]
    # 강조: 첫 버튼만 primary
    mk = composer.markup(rows, "accent").to_dict()["inline_keyboard"][0]
    assert mk[0]["style"] == "primary" and "style" not in mk[1]
    # url 은 유지
    assert mk[0]["url"] == "https://t.me/a"


@test
async def url_buttons_not_auto_colored():
    # 전역 자동색은 콜백 버튼만 — url 버튼(composer 테마 담당)은 안 건드림
    b = InlineKeyboardButton("🚫 밴", url="https://t.me/x")
    assert btnstyle._styled(b).style is None


@test
async def callback_and_length_unchanged():
    # 색을 붙여도 콜백 64바이트 한도·글자는 그대로
    b = InlineKeyboardButton("🚫 밴 (영구)", callback_data="act:ban:12345")
    out = btnstyle._styled(b)
    assert out.callback_data == "act:ban:12345" and out.text == "🚫 밴 (영구)"
    assert len(out.callback_data.encode()) <= 64


if __name__ == "__main__":
    import sys
    sys.exit(1 if asyncio.run(run_all()) else 0)
