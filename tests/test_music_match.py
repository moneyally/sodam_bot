"""🎵 신청 글 ↔ 곡 고르기 (sodam/voice/musicmatch.py): python tests/run_all.py music_match

자료 = 서버 실측 2026-10-08 (tests/fixtures/music/search_20261008.json — 실제 신청 36개의 기본 음원 검색 8개씩 + 대체 음원 검색).
오너 '이상한 노래 뽑힌다' → 원인 추적에서 나온 구멍을 하나씩 막았는지 본다:
  ① 맨 위 곡을 그냥 믿음 ② 같은 가수 다른 노래 ③ 'By 다른 가수' 커버가 원곡을 이김 ④ '좋은 발라드' 같은 신청 ⑤ 같은 곡 중복(test_music).
"""
import json
from pathlib import Path

from fakes import runner

from sodam.voice import musicmatch as mm

test, run_all = runner()
DATA = json.loads((Path(__file__).parent / "fixtures/music/search_20261008.json").read_text())


def pick(q):
    return mm.choose(mm.parse(q), DATA["yt"][q], 1200)


@test
def requests_are_cleaned_and_generic_or_compilation_asks_back():
    r = mm.parse("아이유 밤편지 틀어줘")
    assert r.must == ["아이유", "밤편지"] and not r.intent and not r.generic
    assert mm.parse("좋은 발라드").generic and mm.parse("노래 틀어줘").generic and mm.parse("").generic
    assert mm.parse("최유리 노래모음").compilation and mm.parse("발라드 플레이리스트").compilation
    assert mm.parse("아로하 커버").intent == "cover" and mm.parse("아로하 커버").must == ["아로하"]
    assert mm.parse("아이유 라이브 밤편지").intent == "live"
    assert mm.parse("40-넋").must == ["40", "넋"], "한 글자 한글 낱말('넋')도 신청 낱말"


@test
def title_hits_are_word_safe_and_forgive_small_spelling_slips():
    t = mm.Title("디셈버(DECEMBER) - 별이될께 [가사/lyrics]")
    assert t.hit("별이될게") and t.hit("디셈버") and t.hit("december")
    assert not mm.Title("김연아 - 나비").hit("김연지"), "이름 한 글자 다르면 다른 사람 (자모 비슷해도)"
    assert not mm.Title("보고싶어 - 누구").hit("보고싶다"), "끝말이 다른 노래 ('보고싶다' ≠ '보고싶어')"
    assert mm.norm("残酷な天使のテーゼ") == "残酷な天使のテーゼ" and mm.norm("Beyoncé") == "beyonce", "가나 탁점은 그대로, 라틴 악센트만"
    assert not mm.Title("Allow me").hit("all") and mm.Title("Love all").hit("all"), "짧은 영어는 낱말 단위"
    assert mm.Title("40 (포티) - 넋 / 가사").hit("넋") and not mm.Title("40의 노래").hit("넋")
    assert mm.Title("Drowning Love - Chasing Kou").hit("drowing")
    assert mm.Title("말달리자 -- 크라잉 넛").covers(mm.parse("말 달리자")), "띄어쓰기만 다름"
    assert mm.Title("[MV] Kim Na Young(김나영) _ You Are The Sea(너는 바다)").covers(mm.parse("너는바다-김나영"))


@test
def real_requests_pick_the_right_song_or_ask():
    want = {   # 신청 → 바로 틀 곡(제목 일부) / '?' + 고르기 첫 곡
        "조정석 아로하": "조정석 - 아로하 [슬기로운",            # ③ 커버가 먼저 나와도 원곡
        "이보람 당신을 위하여": "이보람 (씨야) - 당신을 위하여",   # ① 맨 위 라이브 클립 X
        "뉴진스 하입보이": "'Hype Boy (하입보이)'",
        "아이유 밤편지": "아이유(IU) - 밤편지",
        "40-넋": "40 (포티) - 넋",                                # 맨 위 '넋'(가수 없음) X
        "말 달리자": "말달리자 -- 크라잉 넛",                       # 띄어쓰기만 다른 원곡
        "lemon": "Kenshi Yonezu  - Lemon",                        # 'Official' 붙은 다른 밴드 X
        "씨야 여인의 향기": "씨야 (SeeYa) - 여인의 향기 (A Woman",  # 콘서트·방송 무대 X
        "정승환 내 머리가 나빠서": "정승환 - 내 머리가 나빠서 (꽃보다",   # 방송 이름 '노래방 옆 만화방' 은 반주 아님
        "drowing love": "Drowning Love - Chasing Kou",
        "김연지 별이될께": "?디셈버(DECEMBER) - 별이될께",         # ① 맨 위 '미친 사랑의 노래'(다른 곡) X → 고르기
        "2am-줄수있는게": "?",                                    # 맞는 곡이 없음 → 바로 틀지 않음
        "블루문": "?엔플라잉",                                     # 두 곡(엔플라잉·효린) → 고르기
        "조석성 아로하": "?조정석 - 아로하",                        # 오타 → 고르기
        "아로하 커버": "?🎤",                                      # 커버 원함 → 커버 먼저 고르기
        "크래비티 아로하": "?",
    }
    bad = []
    for q, w in want.items():
        p = pick(q)
        if w.startswith("?"):
            ok = p.item is None and p.choices and (w == "?" or w[1:] in (p.choices[0]["title"] if w[1:] != "🎤" else
                                                                            mm.KIND_LABEL[p.choices[0]["kind"]]))
        else:
            ok = p.item is not None and w in p.item["title"]
        if not ok:
            bad.append((q, w, p.item and p.item["title"], [c["title"][:40] for c in p.choices]))
    assert not bad, bad


@test
def cover_intent_offers_covers_first_and_original_by_default():
    p = pick("아로하 커버")
    assert p.reason == "intent" and p.choices[0]["kind"] == "cover", p.choices
    assert pick("조정석 아로하").item["kind"] == "", "기본은 원곡"
    p = pick("아이유 라이브 밤편지")
    assert p.reason == "intent" and p.choices[0]["kind"] == "live"


@test
def alt_source_needs_both_singer_and_song_from_the_reference():
    cases = [  # (신청, 기준 곡, 대체 음원 검색어, 길이, 받아야 할 것, 받으면 안 되는 것)
        ("조정석 아로하", "조정석 - 아로하 [슬기로운 의사생활 OST] [가사/Lyrics]", "조정석 - 아로하 [슬기로운 의사생활 OST]", 246,
         "조정석 (CHO JUNG SEOK) - 아로하 (Aloha) [슬기로운", "By 원진,민희 of CRAVITY"),            # ③
        ("김연지 별이될께", "디셈버(DECEMBER) - 별이될께 [가사/lyrics]", "디셈버(DECEMBER) - 별이될께", 211,
         "별이될께 - 디셈버", "디셈버 December 이별연습"),                                         # ② 같은 가수 다른 노래
        ("아이유 밤편지", "아이유(IU) - 밤편지 [가사/Lyrics]", "아이유 밤편지", 254,
         "아이유 - 밤편지", "설윤 (SULLYOON) - 밤편지"),                                           # 'Original Song by IU' = 다른 가수
        ("가비엔제이 love all", "LOVE ALL (LOVE ALL)", "LOVE ALL (LOVE ALL)", 219, None, "Love All (feat. JA"),
        ("가비엔제이 love all", "가비엔제이.. Love all..(가사첨부)", "가비엔제이 love all", 219, "가비엔제이 Love all", "All Love"),
        ("블루문", "엔플라잉 (N.Flying) - Blue Moon [가사/Lyrics]", "엔플라잉 (N.Flying) - Blue Moon", 216,
         "N.Flying (엔플라잉) 'Blue Moon'", "Blue Scene"),
        ("뉴진스 하입보이", "NewJeans (뉴진스) 'Hype Boy (하입보이)' 가사", "NewJeans (뉴진스) 'Hype Boy'", 179,
         "NewJeans (뉴진스) 'Hype Boy'", "Remix"),
    ]
    bad = []
    for req, ref, q, dur, good, nope in cases:
        r = mm.parse(req)
        passed = []
        for e in DATA["sc"][q]:
            title, d = e["title"] or "", int(e["duration"] or 0)
            same, _ = mm.same_song(title, ref, r)
            if same and not mm.kind(title, r.allowed, ref) and 45 <= d and abs(d - dur) <= max(30, dur * 0.2):
                passed.append(title)
        passed = [mm.compact(t) for t in passed]
        good, nope = good and mm.compact(good), mm.compact(nope)
        if good and not any(good in t for t in passed):
            bad.append((req, "못 받음", good, passed))
        if any(nope in t for t in passed):
            bad.append((req, "받으면 안 됨", nope, passed))
    assert not bad, bad


@test
def reference_title_splits_into_singer_and_song():
    assert mm.split("[MV] Paul Kim(폴킴) _ Me After You(너를 만나) [가사/Lyrics]") == [(["paul", "kim"], ["폴킴"]),
                                                                                    (["me", "after", "you"], ["너를", "만나"])]
    assert mm.split("NewJeans (뉴진스) 'Hype Boy' Official MV") == [(["newjeans"], ["뉴진스"]), (["hype", "boy"], [])]
    assert mm.split("Gavy NJ - Happiness, 가비엔제이 - Happiness, For You 20060105") == [(["gavy", "nj"], []), (["happiness"], [])]
    assert mm.split("40 (포티) - 넋 / 가사 (Lyrics)") == [(["40"], ["포티"]), (["넋"], [])]


@test
def by_someone_else_is_a_cover_unless_that_name_was_asked():
    assert mm.kind("Aloha / 아로하 (조정석) By 원진,민희 of CRAVITY - 슬기로운 의사생활 OST") == "cover"
    assert mm.kind("Aloha / 아로하 By 원진,민희", allowed="원진 민희 아로하") == ""
    assert mm.kind("03 배운게 사랑이라 By 디셈버(December)", known="디셈버 - 별이될께") == ""
    assert mm.kind("Stand By Me", allowed="stand by me") == ""
    assert mm.kind("NewJeans (뉴진스) 'Hype Boy' Official MV (Performance ver.1)") == "", "'ver.' 만으로 변형 아님"
    assert mm.kind("Olivia Rodrigo - drivers license (Deluxe Edition)") == ""
    assert mm.kind("[TJ노래방] 아로하 - 조정석 / TJ Karaoke") == "inst"


@test
def alt_source_holes_found_by_live_server_run():
    """서버에서 새 규칙을 실제로 돌려 보고 찾은 것 (2026-10-08 2차)."""
    r = mm.parse("씨야 여인의 향기")
    same, why = mm.same_song("윈터 - 여인의 향기 (씨야)", "씨야 (SeeYa) - 여인의 향기 (A Woman's Scent)", r)
    assert not same and why == -1, "다른 가수 이름이 한쪽을 차지 = 커버 (괄호 속 원곡 가수로 통과 X)"
    assert mm.same_song("씨야 - 여인의 향기", "씨야 (SeeYa) - 여인의 향기 (A Woman's Scent)", r)[0]
    assert mm.kind("aespa (에스파) 'Supernova' miremix") == "remix", "붙여 쓴 remix"
    assert mm.odd_version("잔나비 (JANNABI) - 주저하는 연인들을 위해 (강희선성우ver)", "잔나비 - 주저하는 연인들을 위해")
    assert not mm.odd_version("폴킴 - 모든 날, 모든 순간", "폴킴 - 모든 날, 모든 순간 (Every day, Every Moment)")
    assert mm.same_song("아이유 - 좋은날", "IU (아이유) _ Good Day (좋은 날) _ MV", mm.parse("IU 좋은날"))[0], "띄어쓰기만 다른 다른 표기"
    assert mm.same_song("크라잉넛 - 말 달리자", "말달리자 -- 크라잉 넛", mm.parse("말 달리자"))[0]
    assert mm.same_song("Lemon - Kenshi Yonezu", "米津玄師  Kenshi Yonezu  - Lemon", mm.parse("lemon"))[0], "가수 표기 3개 중 2개"
    assert not mm.same_song("Lemon - Napraw", "米津玄師  Kenshi Yonezu  - Lemon", mm.parse("lemon"))[0], "같은 제목 다른 가수"
    assert not mm.touches_all_parts("말달리자 (Run your horse)", "말달리자 -- 크라잉 넛"), "가수 쪽이 없으면 신청 낱말로도 안 살림"
    assert mm.touches_all_parts("요아소비x수현-아마도(たぶん)", "처음으로 돌아갈 수 있을까🛋: 요아소비 - 아마도(たぶん, tabun) [가사]")
    assert mm.extra_words("주저하는 연인들을 위해  잔나비 최정훈의 밤의공원", "잔나비 - 주저하는 연인들을 위해") > \
        mm.extra_words("잔나비 - 주저하는 연인들을 위해", "잔나비 - 주저하는 연인들을 위해")
    akmu = [{"id": "x1", "title": "린&찬혁이 부르는 AKMU의 '어떻게 이별까지 사랑하겠어, 널 사랑하는 거지' [더 시즌즈-악뮤의 오날오밤] | KBS", "duration": 132},
            {"id": "x2", "title": "악뮤 어떻게 이별까지 사랑하겠어 널 사랑하는 거지 (불후의 명곡)", "duration": 290}]
    p = mm.choose(mm.parse("악뮤 어떻게 이별까지 사랑하겠어, 널 사랑하는 거지"), akmu, 1200)
    assert p.item is None and p.choices, "방송 무대·다른 사람이 부른 것뿐이면 바로 틀지 않음"


if __name__ == "__main__":
    run_all()
