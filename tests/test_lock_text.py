"""🔤 글자/언어 패턴 잠금 (moderation.LOCK_KINDS 텍스트 종류). python tests/run_all.py lock_text

2026-11-22 경쟁조사 ⑨: 중국어·키릴·아랍·zalgo·캐시태그·이메일 종류별 잠금.
한국인 多·외국인 少 라 오탐(정상 한국어/영어 섞인 글을 막음)이 제일 위험 → 그걸 집중 검사."""
from types import SimpleNamespace

from fakes import runner
from sodam.moderation import LOCK_KINDS

test, run_all = runner()

L = {k: fn for k, _, fn in LOCK_KINDS}   # 설정키 → 판정 람다


def m(text=None, caption=None, **kw):
    return SimpleNamespace(text=text, caption=caption, **kw)


@test
def text_locks_catch_their_kind():
    assert L["lock_cyrillic"](m("привет друзья как дела"))          # 키릴 많음 = 러시아 스팸
    assert L["lock_arabic"](m("مرحبا بكم في القناة"))               # 아랍
    assert L["lock_cjk"](m("免费领取加入我们的群"))                  # 순수 한자(중국어)
    assert L["lock_zalgo"](m("z̴a̵l̶g̷o̸ t̴e̵x̶t̷"))                            # 결합기호 떡칠
    assert L["lock_cashtag"](m("지금 $BTC 사세요 $USDT 입금"))        # 코인 캐시태그
    assert L["lock_email"](m("연락은 scam@evil.com 으로"))           # 이메일


@test
def korean_normal_messages_never_trip_text_locks():
    # 한국인 다수 방 — 평범한 한국어·이모지·섞인 영어는 어떤 글자잠금에도 안 걸려야 (오탐 0)
    text_keys = ["lock_cyrillic", "lock_arabic", "lock_cjk", "lock_zalgo", "lock_cashtag", "lock_email"]
    for t in ["안녕하세요 반갑습니다", "ㅋㅋㅋ 그래 내일 봐", "10시에 만나요 늦지마",
              "ok 그거 nice 하다", "$10 입금했어요", "가격 $5 정도", "커피 한잔 ☕️ 할까",
              "this is good 좋아요", "中 섞어 쓴 한국어 메시지"]:
        tripped = [k for k in text_keys if L[k](m(t))]
        assert not tripped, f"오탐: {t!r} → {tripped}"


@test
def conservative_thresholds():
    assert not L["lock_cyrillic"](m("ok да"))                       # 키릴 2자뿐 → 통과(짧은 건 안 막음)
    assert not L["lock_cjk"](m("이거 中國 음식 맛집"))               # 한자 있지만 한글 섞임 → 통과(오탐 방지)
    assert not L["lock_cjk"](m("한자 二 개만 二"))                   # 한자 2자 + 한글 → 통과
    assert not L["lock_zalgo"](m("é à ü 악센트 조금"))               # 결합기호 소수 → 통과
    assert not L["lock_cashtag"](m("가격은 $100 입니다"))            # $숫자 는 캐시태그 아님 → 통과
    assert L["lock_cjk"](m("加入赚钱转发"))                          # 순수 한자 4자 → 걸림(경계 확인)


@test
def caption_is_checked_too():
    assert L["lock_email"](m(text=None, caption="문의 ad@spam.net"))  # 사진 설명(caption)도 검사
    assert not L["lock_email"](m(text=None, caption="사진 설명 한국어"))
