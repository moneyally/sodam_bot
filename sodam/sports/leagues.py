"""리그·종목·팀 이름표 (한국어 별칭 ↔ 데이터 소스 이름). 코드만 — 네트워크 없음.

리그 키(code)는 DB(sports_follow.league)에 저장되므로 바꾸지 말 것. 소스별 이름:
- espn  = site.api.espn.com 경로 '{sport}/{league}' (실측 2026-09-29, tests/fixtures/sports)
- naver = api-gw.sports.naver.com categoryId (실측 2026-09-29, 기본 꺼짐 SPORTS_NAVER=1)
- sdb   = TheSportsDB 리그 ID (유료 키일 때만)
- aps   = API-Sports (종목 API, 리그 ID) — 실측 2026-10-01 (/leagues?country=South-Korea·Japan, 경기 응답의 league.id).
          국내: K리그1 292 · K리그2 293 · KBO 5 · NPB 2 · KBL 91 · WKBL 92 · V리그 남 151 · 여 152. MLB 1 (경기 응답에서 확인).
          해외 리그는 ESPN 전용 (API-Sports 를 대신 쓰면 라이브 재조회 src·영어 팀 이름표가 안 맞음 — 리뷰 2026-10-01).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class League:
    code: str
    name: str                 # 화면 이름 (한국어/약칭)
    sport: str                # soccer / baseball / basketball / volleyball / hockey / mma
    espn: str | None = None
    naver: str | None = None
    sdb: str | None = None
    aps: tuple[str, int] | None = None   # (API-Sports 종목: football/baseball/basketball/volleyball, 리그 ID)
    aliases: tuple[str, ...] = field(default=())
    korean: bool = False      # 국내 리그 (ESPN 에 없음)
    naver_codes: tuple[str, ...] = ()   # 네이버 종합대회(아시안게임 등): gameId[4:7] 종목 코드로 거름 (FBL 축구 · VVO 배구 · VBV 비치발리볼)
    minor: bool = False       # 이름으로 부를 때만 (종목 전체 '.스포츠 축구' 에선 빼서 ESPN 요청이 수십 개로 늘지 않게)


SPORT_EMOJI = {"soccer": "⚽", "baseball": "⚾", "basketball": "🏀", "volleyball": "🏐", "hockey": "🏒", "mma": "🥊",
               "football": "🏈", "racing": "🏎️", "golf": "⛳", "tennis": "🎾", "afootball": "🏉"}
SPORT_KO = {"soccer": "축구", "baseball": "야구", "basketball": "농구", "volleyball": "배구", "hockey": "아이스하키",
            "mma": "격투기", "football": "미식축구", "racing": "모터스포츠", "golf": "골프", "tennis": "테니스",
            "afootball": "호주식 풋볼"}
SPORT_WORDS = {"축구": "soccer", "해외축구": "soccer", "soccer": "soccer", "football": "soccer", "야구": "baseball",
               "baseball": "baseball", "농구": "basketball", "basketball": "basketball", "배구": "volleyball",
               "volleyball": "volleyball", "하키": "hockey", "아이스하키": "hockey", "hockey": "hockey",
               "격투기": "mma", "종합격투기": "mma", "mma": "mma", "풋볼": "football", "골프": "golf", "golf": "golf",
               "테니스": "tennis", "tennis": "tennis", "모터스포츠": "racing", "레이싱": "racing", "자동차경주": "racing"}
# 골(득점 하나하나) 알림이 의미 있는 종목. 나머지(야구·농구·배구)는 기본 시작·결과만 (득점이 너무 잦음)
GOAL_SPORTS = {"soccer", "hockey"}

_L = [
    League("epl", "EPL", "soccer", "soccer/eng.1", "epl", "4328", None,
           ("epl", "프리미어리그", "프리미어", "프리미어 리그", "premier league", "잉글랜드", "prem", "프리미어리그epl")),
    League("laliga", "라리가", "soccer", "soccer/esp.1", "primera", "4335", None,
           ("라리가", "la liga", "laliga", "스페인", "프리메라리가")),
    League("seriea", "세리에A", "soccer", "soccer/ita.1", "seria", "4332", None,
           ("세리에a", "세리에", "serie a", "seriea", "이탈리아")),
    League("bundesliga", "분데스리가", "soccer", "soccer/ger.1", "bundesliga", "4331", None,
           ("분데스리가", "분데스", "bundesliga", "독일")),
    League("ligue1", "리그1", "soccer", "soccer/fra.1", "ligue1", "4334", None,
           ("리그1", "리그앙", "ligue1", "ligue 1", "프랑스")),
    League("ucl", "챔스", "soccer", "soccer/uefa.champions", "champs", "4480", None,
           ("ucl", "챔스", "챔피언스리그", "챔피언스 리그", "champions league", "챔스리그")),
    League("uel", "유로파", "soccer", "soccer/uefa.europa", None, "4481", None,
           ("uel", "유로파", "유로파리그", "europa league")),
    # 국가대표 대회 (A매치 기간엔 클럽 리그가 쉼 → 2026-10-01 베베방 '축구 경기 없어요' 실측: 네이션스리그 8경기가 있었음)
    # ESPN 경로 실측 2026-10-01 (dates=하루, 200). afc.asian·afc.asian.qual 은 400 이라 뺌
    League("unl", "UEFA 네이션스리그", "soccer", "soccer/uefa.nations", None, None, None,
           ("네이션스리그", "uefa네이션스리그", "nations league", "유럽네이션스리그", "unl")),
    League("friendly", "A매치 친선", "soccer", "soccer/fifa.friendly", None, None, None,
           ("a매치", "친선", "친선경기", "평가전", "국대", "국가대표", "a매치친선")),
    League("worldcup", "월드컵", "soccer", "soccer/fifa.world", None, None, None, ("월드컵", "world cup", "fifa월드컵")),
    League("wcq_afc", "월드컵 아시아 예선", "soccer", "soccer/fifa.worldq.afc", None, None, None,
           ("월드컵아시아예선", "아시아예선", "월드컵예선")),
    League("wcq_uefa", "월드컵 유럽 예선", "soccer", "soccer/fifa.worldq.uefa", None, None, None, ("월드컵유럽예선", "유럽예선")),
    League("euro", "유로", "soccer", "soccer/uefa.euro", None, None, None, ("유로", "euro", "유로대회")),
    League("euroq", "유로 예선", "soccer", "soccer/uefa.euroq", None, None, None, ("유로예선", "euro qualifying")),
    League("copa", "코파 아메리카", "soccer", "soccer/conmebol.america", None, None, None, ("코파", "코파아메리카", "copa america")),
    League("afcon", "아프리카 네이션스컵", "soccer", "soccer/caf.nations", None, None, None,
           ("아프리카네이션스컵", "afcon", "아프리카컵")),
    League("afcon_q", "아프리카 네이션스컵 예선", "soccer", "soccer/caf.nations_qual", None, None, None,
           ("아프리카네이션스컵예선", "아프리카예선", "afcon예선")),
    # 아시아 축구 (ESPN 리그 목록 실측 2026-10-02 sports.core.api.espn.com/v2/sports/soccer/leagues — 전부 200)
    League("acl", "AFC 챔피언스리그", "soccer", "soccer/afc.champions", None, None, None,
           ("acl", "afc챔피언스리그", "아챔", "아시아챔피언스리그", "챔피언스리그엘리트", "acl엘리트")),
    League("acl2", "AFC 챔피언스리그2", "soccer", "soccer/afc.cup", None, None, None, ("acl2", "afc컵", "afc챔피언스리그2", "아챔2")),
    League("asiancup", "AFC 아시안컵", "soccer", "soccer/afc.asian.cup", None, None, None,
           ("아시안컵", "afc아시안컵", "asian cup", "피파아시안컵")),
    League("w_asiancup", "AFC 여자 아시안컵", "soccer", "soccer/afc.w.asian.cup", None, None, None, ("여자아시안컵", "여자 아시안컵")),
    # 아시안게임 (아이치·나고야 2026, ~10/4): ESPN 에 없음 → 네이버 종합대회 일정 (SPORTS_NAVER=1).
    # 실측 2026-10-02: 팀 이름·점수가 비어 옴(제목 '여자 금메달전'·시각·진행 상태만) → 경기 이름으로 시작·종료만 알림.
    # 다음 대회는 네이버 categoryId 가 바뀜 (asiangames2030 등) — 그때 이 두 줄만 고치면 됨
    League("ag_soccer", "아시안게임 축구", "soccer", None, "asiangames2026", None, None,
           ("아시안게임축구", "아겜축구", "아시안게임 축구"), naver_codes=("FBL",)),
    League("ag_volley", "아시안게임 배구", "volleyball", None, "asiangames2026", None, None,
           ("아시안게임배구", "아겜배구", "아시안게임 배구", "아시안게임"), naver_codes=("VVO", "VBV")),
    # 2026-10-03 방 요청: 북중미 네이션스리그 (ESPN 실측 — 조별 9/23~10/6, 8강 11월, 결승 3월)
    League("concacaf_nl", "북중미 네이션스리그", "soccer", "soccer/concacaf.nations.league", None, None, None,
           ("북중미네이션스리그", "북중미 네이션스", "콘카카프", "콘카카프네이션스리그", "concacaf", "concacaf nations league",
            "북중미")),
    League("uecl", "컨퍼런스리그", "soccer", "soccer/uefa.europa.conf", None, None, None,
           ("컨퍼런스리그", "uecl", "컨퍼런스", "conference league")),
    League("jleague", "J리그", "soccer", "soccer/jpn.1", None, "4633", None, ("j리그", "j1", "제이리그", "일본축구", "j league")),
    League("kleague", "K리그1", "soccer", None, "kleague", "4689", ("football", 292),
           ("k리그", "k리그1", "케이리그", "kleague", "k league", "k리그 1"), korean=True),
    League("kleague2", "K리그2", "soccer", None, "kleague2", None, ("football", 293), ("k리그2", "케이리그2", "k league 2"), korean=True),
    League("mlb", "MLB", "baseball", "baseball/mlb", "mlb", "4424", None,
           ("mlb", "메이저리그", "메이저 리그", "엠엘비", "미국야구", "빅리그")),
    League("kbo", "KBO", "baseball", None, "kbo", "4830", ("baseball", 5), ("kbo", "크보", "국야", "프로야구", "한국야구"), korean=True),
    League("npb", "NPB", "baseball", None, "npb", "4591", ("baseball", 2), ("npb", "일본야구", "일야", "일본 프로야구"), korean=True),
    League("nba", "NBA", "basketball", "basketball/nba", "nba", "4387", None, ("nba", "엔비에이", "미국농구", "미농")),
    League("kbl", "KBL", "basketball", None, "kbl", None, ("basketball", 91), ("kbl", "프로농구", "한국농구", "남자농구"), korean=True),
    League("wkbl", "WKBL", "basketball", None, "wkbl", None, ("basketball", 92), ("wkbl", "한국여자농구", "국내여자농구"), korean=True),
    League("wnba", "WNBA", "basketball", "basketball/wnba", None, None, None,
           ("wnba", "미국여자농구", "미국 여자농구", "여자nba")),
    League("vleague", "V리그 남자", "volleyball", None, "kovo", None, ("volleyball", 151),
           ("v리그", "브이리그", "kovo", "남자배구", "v리그 남자", "v리그남자", "남배"), korean=True),
    League("wvleague", "V리그 여자", "volleyball", None, "wkovo", None, ("volleyball", 152),
           ("여자배구", "v리그 여자", "v리그여자", "wkovo", "여배"), korean=True),
    League("nhl", "NHL", "hockey", "hockey/nhl", None, "4380", None, ("nhl", "북미하키", "엔에이치엘", "미국하키")),
    League("ufc", "UFC", "mma", "mma/ufc", "ufc", None, None, ("ufc", "유에프씨")),
    # ── 2026-10-03 추가 ('우리 없는 데이터 전부' — 방 요청이 계속 옴). ESPN 실측으로 경기가 오는 것만, 이름으로 부를 때만 (minor)
    League("championship", "잉글랜드 챔피언십", "soccer", "soccer/eng.2", None, None, None, ('챔피언십', 'efl챔피언십', '잉글랜드2부', 'championship'), minor=True),
    League("facup", "FA컵", "soccer", "soccer/eng.fa", None, None, None, ('fa컵', 'facup', '에프에이컵'), minor=True),
    League("carabao", "카라바오컵", "soccer", "soccer/eng.league_cup", None, None, None, ('카라바오컵', 'efl컵', '리그컵', 'carabao cup'), minor=True),
    League("copadelrey", "코파 델 레이", "soccer", "soccer/esp.copa_del_rey", None, None, None, ('코파델레이', '국왕컵', '스페인국왕컵', 'copa del rey'), minor=True),
    League("dfbpokal", "DFB 포칼", "soccer", "soccer/ger.dfb_pokal", None, None, None, ('dfb포칼', '포칼', '독일컵', 'dfb pokal'), minor=True),
    League("coppaitalia", "코파 이탈리아", "soccer", "soccer/ita.coppa_italia", None, None, None, ('코파이탈리아', '이탈리아컵', 'coppa italia'), minor=True),
    League("coupedefrance", "쿠프 드 프랑스", "soccer", "soccer/fra.coupe_de_france", None, None, None, ('쿠프드프랑스', '프랑스컵', 'coupe de france'), minor=True),
    League("eredivisie", "에레디비시", "soccer", "soccer/ned.1", None, None, None, ('에레디비시', '에레디비지', '네덜란드리그', 'eredivisie'), minor=True),
    League("primeira", "프리메이라리가", "soccer", "soccer/por.1", None, None, None, ('프리메이라리가', '포르투갈리그', 'primeira liga'), minor=True),
    League("scotland", "스코틀랜드 프리미어십", "soccer", "soccer/sco.1", None, None, None, ('스코틀랜드리그', '스코티시프리미어십', 'spfl'), minor=True),
    League("superlig", "튀르키예 쉬페르리그", "soccer", "soccer/tur.1", None, None, None, ('쉬페르리그', '터키리그', '튀르키예리그', 'super lig'), minor=True),
    League("belgium", "벨기에 프로리그", "soccer", "soccer/bel.1", None, None, None, ('벨기에리그', '주필러리그', 'jupiler'), minor=True),
    League("mls", "MLS", "soccer", "soccer/usa.1", None, None, None, ('mls', '미국축구', '메이저리그사커'), minor=True),
    League("ligamx", "리가 MX", "soccer", "soccer/mex.1", None, None, None, ('리가mx', '멕시코리그', 'liga mx'), minor=True),
    League("brasileirao", "브라질 세리에A", "soccer", "soccer/bra.1", None, None, None, ('브라질리그', '브라질레이랑', '브라질세리에a', 'brasileirao'), minor=True),
    League("argentina", "아르헨티나 리가", "soccer", "soccer/arg.1", None, None, None, ('아르헨티나리그', '아르헨리그'), minor=True),
    League("saudi", "사우디 프로리그", "soccer", "soccer/ksa.1", None, None, None, ('사우디리그', '사우디프로리그', 'spl'), minor=True),
    League("csl", "중국 슈퍼리그", "soccer", "soccer/chn.1", None, None, None, ('중국슈퍼리그', '중국리그', 'csl'), minor=True),
    League("aleague", "호주 A리그", "soccer", "soccer/aus.1", None, None, None, ('a리그', '호주리그', '에이리그', 'a-league'), minor=True),
    League("cwc", "FIFA 클럽월드컵", "soccer", "soccer/fifa.cwc", None, None, None, ('클럽월드컵', '클월', 'club world cup'), minor=True),
    League("goldcup", "골드컵", "soccer", "soccer/concacaf.gold", None, None, None, ('골드컵', '북중미골드컵', 'gold cup'), minor=True),
    League("concacaf_cl", "북중미 챔피언스컵", "soccer", "soccer/concacaf.champions", None, None, None, ('북중미챔피언스컵', '콘카카프챔피언스컵', 'concacaf champions cup'), minor=True),
    League("libertadores", "코파 리베르타도레스", "soccer", "soccer/conmebol.libertadores", None, None, None, ('리베르타도레스', '남미챔스', 'libertadores'), minor=True),
    League("sudamericana", "코파 수다메리카나", "soccer", "soccer/conmebol.sudamericana", None, None, None, ('수다메리카나', 'sudamericana'), minor=True),
    League("wcq_conmebol", "월드컵 남미 예선", "soccer", "soccer/fifa.worldq.conmebol", None, None, None, ('월드컵남미예선', '남미예선'), minor=True),
    League("wcq_concacaf", "월드컵 북중미 예선", "soccer", "soccer/fifa.worldq.concacaf", None, None, None, ('월드컵북중미예선', '북중미예선'), minor=True),
    League("wcq_caf", "월드컵 아프리카 예선", "soccer", "soccer/fifa.worldq.caf", None, None, None, ('월드컵아프리카예선', '아프리카예선'), minor=True),
    League("uwcl", "UEFA 여자 챔스", "soccer", "soccer/uefa.wchampions", None, None, None, ('여자챔스', '여자챔피언스리그', 'uwcl'), minor=True),
    League("wsl", "잉글랜드 여자 슈퍼리그", "soccer", "soccer/eng.w.1", None, None, None, ('wsl', '잉글랜드여자리그', '여자슈퍼리그'), minor=True),
    League("wwc", "FIFA 여자 월드컵", "soccer", "soccer/fifa.wwc", None, None, None, ('여자월드컵', "women's world cup"), minor=True),
    League("nwsl", "NWSL", "soccer", "soccer/usa.nwsl", None, None, None, ('nwsl', '미국여자축구'), minor=True),
    League("supercup", "UEFA 슈퍼컵", "soccer", "soccer/uefa.super_cup", None, None, None, ('슈퍼컵', 'uefa슈퍼컵'), minor=True),
    League("olympic_soccer", "올림픽 남자 축구", "soccer", "soccer/fifa.olympics", None, None, None, ('올림픽축구', '올림픽 축구'), minor=True),
    League("ncaab", "NCAA 남자 농구", "basketball", "basketball/mens-college-basketball", None, None, None, ('ncaa농구', '미국대학농구', '대학농구', 'ncaab', 'march madness', '3월의광란'), minor=True),
    League("ncaaw", "NCAA 여자 농구", "basketball", "basketball/womens-college-basketball", None, None, None, ('ncaa여자농구', '미국대학여자농구', '대학여자농구'), minor=True),
    League("fiba", "FIBA 농구 월드컵", "basketball", "basketball/fiba", None, None, None, ('fiba', '농구월드컵', 'fiba월드컵'), minor=True),
    League("olympic_bball", "올림픽 남자 농구", "basketball", "basketball/mens-olympics-basketball", None, None, None, ('올림픽농구',), minor=True),
    League("ncaah", "NCAA 아이스하키", "hockey", "hockey/mens-college-hockey", None, None, None, ('대학하키', 'ncaa하키', '미국대학하키'), minor=True),
    League("ncaabase", "NCAA 야구", "baseball", "baseball/college-baseball", None, None, None, ('대학야구', 'ncaa야구', '미국대학야구'), minor=True),
    League("wbc", "WBC", "baseball", "baseball/world-baseball-classic", None, None, None, ('wbc', '월드베이스볼클래식', '야구월드컵'), minor=True),
    League("nfl", "NFL", "football", "football/nfl", None, None, None, ('nfl', '미식축구', '슈퍼볼', '엔에프엘'), minor=True),
    League("ncaaf", "NCAA 미식축구", "football", "football/college-football", None, None, None, ('대학미식축구', 'ncaa미식축구', '칼리지풋볼'), minor=True),
    League("f1", "F1", "racing", "racing/f1", None, None, None, ('f1', '포뮬러원', '포뮬러1', 'formula 1', '에프원'), minor=True),
    League("pga", "PGA 투어", "golf", "golf/pga", None, None, None, ('pga', 'pga투어', '남자골프', '미국골프'), minor=True),
    League("atp", "ATP 테니스", "tennis", "tennis/atp", None, None, None, ('atp', '남자테니스'), minor=True),
    League("wta", "WTA 테니스", "tennis", "tennis/wta", None, None, None, ('wta', '여자테니스'), minor=True),
    League("afl", "AFL", "afootball", "australian-football/afl", None, None, None, ('afl', '호주풋볼', '호주식축구'), minor=True),
]
LEAGUES: dict[str, League] = {lg.code: lg for lg in _L}
# 인자 없이 '.스포츠 오늘' 이면 보여줄 리그 (쓸 수 있는 것만 남김)
POPULAR = ("epl", "laliga", "seriea", "bundesliga", "ucl", "kbo", "kleague", "mlb", "nba")


def norm(s: str) -> str:
    return re.sub(r"[\s·.\-_']+", "", (s or "").lower())


_ALIAS: dict[str, str] = {}
for _lg in _L:
    for _a in (_lg.code, _lg.name) + _lg.aliases:
        _ALIAS.setdefault(norm(_a), _lg.code)


# 여러 리그를 한 번에 보는 말 (예: '여자농구' = 국내 WKBL + 미국 WNBA — 시즌이 서로 달라 하나만 고르면 빈 화면)
GROUPS: dict[str, tuple[str, ...]] = {"여자농구": ("wkbl", "wnba"), "여농": ("wkbl", "wnba"),
                                       "여자축구": ("uwcl", "wsl", "nwsl", "wwc", "w_asiancup"),
                                       "컵대회": ("facup", "carabao", "copadelrey", "dfbpokal", "coppaitalia", "coupedefrance"),
                                       "남미축구": ("libertadores", "sudamericana", "brasileirao", "argentina", "copa"),
                                       "대학스포츠": ("ncaab", "ncaaw", "ncaaf", "ncaah")}


def find_group(text: str) -> list[League] | None:
    codes = GROUPS.get(norm(text))
    return [LEAGUES[c] for c in codes] if codes else None


def find_league(text: str) -> League | None:
    return LEAGUES.get(_ALIAS.get(norm(text), ""))


def find_sport(text: str) -> str | None:
    return SPORT_WORDS.get(norm(text))


def leagues_of_sport(sport: str, minor: bool = False) -> list[League]:
    """종목 전체. 기본은 큰 리그만 (minor 는 이름으로 부를 때) — 큰 리그가 없는 종목(골프·F1…)은 전부."""
    every = [lg for lg in _L if lg.sport == sport]
    major = [lg for lg in every if not lg.minor]
    return every if minor or not major else major


# ── 팀 이름표: (리그, 소스가 쓰는 이름, 한국어 표시, 별칭…) ─────────────
# 소스 이름 = ESPN displayName (실측 teams 목록 2026-09-29) / 네이버 리그는 네이버 팀 이름 그대로
_T = [
    # EPL
    ("epl", "Tottenham Hotspur", "토트넘", "토트넘홋스퍼", "스퍼스", "tottenham", "spurs"),
    ("epl", "Manchester United", "맨유", "맨체스터유나이티드", "맨체스터 유나이티드", "man utd", "man united"),
    ("epl", "Manchester City", "맨시티", "맨체스터시티", "맨체스터 시티", "man city"),
    ("epl", "Liverpool", "리버풀", "리버풀fc"),
    ("epl", "Arsenal", "아스널", "아스날"),
    ("epl", "Chelsea", "첼시"),
    ("epl", "Newcastle United", "뉴캐슬", "newcastle"),
    ("epl", "Aston Villa", "애스턴 빌라", "아스톤빌라", "빌라"),
    ("epl", "Brighton & Hove Albion", "브라이튼", "브라이턴", "brighton"),
    ("epl", "Brentford", "브렌트퍼드", "브렌트포드"),
    ("epl", "Crystal Palace", "크리스탈 팰리스", "팰리스", "크팰"),
    ("epl", "Everton", "에버턴", "에버튼"),
    ("epl", "Fulham", "풀럼"),
    ("epl", "Nottingham Forest", "노팅엄", "노팅엄 포레스트"),
    ("epl", "AFC Bournemouth", "본머스", "bournemouth"),
    ("epl", "Leeds United", "리즈", "리즈 유나이티드"),
    ("epl", "Sunderland", "선덜랜드"),
    ("epl", "Coventry City", "코번트리", "코벤트리"),
    ("epl", "Hull City", "헐 시티", "헐시티"),
    ("epl", "Ipswich Town", "입스위치"),
    ("epl", "West Ham United", "웨스트햄", "west ham"),
    ("epl", "Wolverhampton Wanderers", "울버햄튼", "울브스", "wolves"),
    # 라리가
    ("laliga", "Real Madrid", "레알", "레알 마드리드", "레알마드리드"),
    ("laliga", "Barcelona", "바르사", "바르셀로나", "바르샤", "barca"),
    ("laliga", "Atlético Madrid", "AT 마드리드", "아틀레티코", "아틀레티코 마드리드", "atletico madrid", "아틀레티코마드리드"),
    ("laliga", "Sevilla", "세비야"), ("laliga", "Valencia", "발렌시아"), ("laliga", "Villarreal", "비야레알"),
    ("laliga", "Real Sociedad", "레알 소시에다드", "소시에다드"), ("laliga", "Athletic Club", "아틀레틱 빌바오", "빌바오"),
    ("laliga", "Real Betis", "레알 베티스", "베티스"), ("laliga", "Girona", "지로나"), ("laliga", "Mallorca", "마요르카"),
    ("laliga", "Getafe", "헤타페"), ("laliga", "Osasuna", "오사수나"), ("laliga", "Celta Vigo", "셀타 비고"),
    # 세리에A
    ("seriea", "Napoli", "나폴리"), ("seriea", "Internazionale", "인터밀란", "인테르", "인터 밀란", "inter"),
    ("seriea", "AC Milan", "AC 밀란", "밀란", "ac밀란"), ("seriea", "Juventus", "유벤투스", "유베"),
    ("seriea", "AS Roma", "AS 로마", "로마"), ("seriea", "Lazio", "라치오"), ("seriea", "Atalanta", "아탈란타"),
    ("seriea", "Fiorentina", "피오렌티나"), ("seriea", "Bologna", "볼로냐"), ("seriea", "Como", "코모"),
    # 분데스리가
    ("bundesliga", "Bayern Munich", "뮌헨", "바이에른 뮌헨", "바이에른", "bayern"),
    ("bundesliga", "Borussia Dortmund", "도르트문트", "돌문", "dortmund"),
    ("bundesliga", "Bayer Leverkusen", "레버쿠젠"), ("bundesliga", "RB Leipzig", "라이프치히"),
    ("bundesliga", "VfB Stuttgart", "슈투트가르트"), ("bundesliga", "Eintracht Frankfurt", "프랑크푸르트"),
    ("bundesliga", "Mainz", "마인츠"), ("bundesliga", "SC Freiburg", "프라이부르크"),
    ("bundesliga", "Borussia Mönchengladbach", "묀헨글라트바흐", "글라트바흐"),
    # 리그1
    ("ligue1", "Paris Saint-Germain", "PSG", "파리 생제르맹", "파리생제르맹", "파리", "psg"),
    ("ligue1", "Marseille", "마르세유"), ("ligue1", "AS Monaco", "모나코"), ("ligue1", "Lyon", "리옹"),
    ("ligue1", "Lille", "릴"), ("ligue1", "Nice", "니스"), ("ligue1", "Lens", "랑스"),
    # MLB (30)
    ("mlb", "Los Angeles Dodgers", "다저스", "LA 다저스", "la다저스", "dodgers"),
    ("mlb", "San Diego Padres", "샌디에이고", "파드리스", "padres"),
    ("mlb", "San Francisco Giants", "샌프란시스코", "SF 자이언츠", "자이언츠"),
    ("mlb", "New York Yankees", "양키스", "뉴욕 양키스", "yankees"), ("mlb", "New York Mets", "메츠", "뉴욕 메츠"),
    ("mlb", "Boston Red Sox", "보스턴", "레드삭스"), ("mlb", "Toronto Blue Jays", "토론토", "블루제이스"),
    ("mlb", "Tampa Bay Rays", "탬파베이", "레이스"), ("mlb", "Baltimore Orioles", "볼티모어", "오리올스"),
    ("mlb", "Chicago Cubs", "컵스", "시카고 컵스"), ("mlb", "Chicago White Sox", "화이트삭스"),
    ("mlb", "St. Louis Cardinals", "세인트루이스", "카디널스"), ("mlb", "Milwaukee Brewers", "밀워키", "브루어스"),
    ("mlb", "Cincinnati Reds", "신시내티", "레즈"), ("mlb", "Pittsburgh Pirates", "피츠버그", "파이리츠"),
    ("mlb", "Atlanta Braves", "애틀랜타", "브레이브스"), ("mlb", "Philadelphia Phillies", "필라델피아", "필리스"),
    ("mlb", "Miami Marlins", "마이애미", "말린스"), ("mlb", "Washington Nationals", "워싱턴", "내셔널스"),
    ("mlb", "Houston Astros", "휴스턴", "애스트로스"), ("mlb", "Texas Rangers", "텍사스", "레인저스"),
    ("mlb", "Seattle Mariners", "시애틀", "매리너스"), ("mlb", "Los Angeles Angels", "에인절스", "LA 에인절스"),
    ("mlb", "Athletics", "애슬레틱스", "오클랜드"), ("mlb", "Arizona Diamondbacks", "애리조나", "다이아몬드백스"),
    ("mlb", "Colorado Rockies", "콜로라도", "로키스"), ("mlb", "Minnesota Twins", "미네소타", "트윈스"),
    ("mlb", "Detroit Tigers", "디트로이트", "타이거스"), ("mlb", "Cleveland Guardians", "클리블랜드", "가디언스"),
    ("mlb", "Kansas City Royals", "캔자스시티", "로열스"),
    # NBA (30)
    ("nba", "Los Angeles Lakers", "레이커스", "LA 레이커스", "lakers"), ("nba", "LA Clippers", "클리퍼스"),
    # WNBA (2026 시즌 팀, ESPN displayName)
    ("wnba", "Atlanta Dream", "애틀랜타 드림"), ("wnba", "Chicago Sky", "시카고 스카이"),
    ("wnba", "Connecticut Sun", "코네티컷 선"), ("wnba", "Dallas Wings", "댈러스 윙스"),
    ("wnba", "Golden State Valkyries", "골든스테이트 발키리스", "발키리스"), ("wnba", "Indiana Fever", "인디애나 피버", "피버"),
    ("wnba", "Las Vegas Aces", "라스베이거스 에이시스", "에이시스"), ("wnba", "Los Angeles Sparks", "LA 스파크스", "스파크스"),
    ("wnba", "Minnesota Lynx", "미네소타 링크스", "링크스"), ("wnba", "New York Liberty", "뉴욕 리버티", "리버티"),
    ("wnba", "Phoenix Mercury", "피닉스 머큐리", "머큐리"), ("wnba", "Seattle Storm", "시애틀 스톰"),
    ("wnba", "Washington Mystics", "워싱턴 미스틱스", "미스틱스"), ("wnba", "Portland Fire", "포틀랜드 파이어"),
    ("wnba", "Toronto Tempo", "토론토 템포"),
    ("nba", "Golden State Warriors", "골든스테이트", "워리어스", "gsw"), ("nba", "Boston Celtics", "셀틱스", "보스턴 셀틱스"),
    ("nba", "Miami Heat", "마이애미 히트", "히트"), ("nba", "Chicago Bulls", "불스", "시카고 불스"),
    ("nba", "New York Knicks", "닉스"), ("nba", "Brooklyn Nets", "브루클린", "네츠"),
    ("nba", "Philadelphia 76ers", "필라델피아 76ers", "식서스"), ("nba", "Toronto Raptors", "랩터스"),
    ("nba", "Milwaukee Bucks", "벅스"), ("nba", "Cleveland Cavaliers", "캐벌리어스", "클리블랜드 캐벌리어스"),
    ("nba", "Denver Nuggets", "덴버", "너게츠"), ("nba", "Phoenix Suns", "피닉스", "선즈"),
    ("nba", "Dallas Mavericks", "댈러스", "매버릭스"), ("nba", "Oklahoma City Thunder", "오클라호마시티", "썬더", "okc"),
    ("nba", "Minnesota Timberwolves", "미네소타 팀버울브스", "팀버울브스"), ("nba", "Houston Rockets", "로키츠"),
    ("nba", "San Antonio Spurs", "샌안토니오", "샌안토니오 스퍼스"), ("nba", "Memphis Grizzlies", "멤피스"),
    ("nba", "New Orleans Pelicans", "뉴올리언스", "펠리컨스"), ("nba", "Sacramento Kings", "새크라멘토", "킹스"),
    ("nba", "Portland Trail Blazers", "포틀랜드", "블레이저스"), ("nba", "Utah Jazz", "유타", "재즈"),
    ("nba", "Atlanta Hawks", "애틀랜타 호크스", "호크스"), ("nba", "Charlotte Hornets", "샬럿", "호네츠"),
    ("nba", "Detroit Pistons", "디트로이트 피스톤스", "피스톤스"), ("nba", "Indiana Pacers", "인디애나", "페이서스"),
    ("nba", "Orlando Magic", "올랜도", "매직"), ("nba", "Washington Wizards", "위저즈"),
    # NHL (일부)
    ("nhl", "Toronto Maple Leafs", "토론토 메이플리프스", "메이플리프스"), ("nhl", "Edmonton Oilers", "에드먼턴", "오일러스"),
    ("nhl", "New York Rangers", "뉴욕 레인저스"), ("nhl", "Boston Bruins", "보스턴 브루인스", "브루인스"),
    ("nhl", "Vegas Golden Knights", "베이거스", "골든나이츠"), ("nhl", "Florida Panthers", "플로리다", "팬서스"),
    # KBO (네이버 이름)
    ("kbo", "KIA", "KIA", "기아", "기아타이거즈", "타이거즈", "kia 타이거즈"), ("kbo", "LG", "LG", "엘지", "lg트윈스", "엘지트윈스"),
    ("kbo", "한화", "한화", "한화이글스", "이글스"), ("kbo", "삼성", "삼성", "삼성라이온즈", "라이온즈"),
    ("kbo", "두산", "두산", "두산베어스", "베어스"), ("kbo", "롯데", "롯데", "롯데자이언츠"),
    ("kbo", "KT", "KT", "케이티", "kt위즈", "위즈"), ("kbo", "SSG", "SSG", "쓱", "랜더스", "ssg랜더스"),
    ("kbo", "NC", "NC", "엔씨", "nc다이노스", "다이노스"), ("kbo", "키움", "키움", "키움히어로즈", "히어로즈"),
    # K리그 (네이버 이름)
    ("kleague", "서울", "FC서울", "fc서울", "fc 서울"), ("kleague", "울산", "울산", "울산현대", "울산hd"),
    ("kleague", "전북", "전북", "전북현대"), ("kleague", "포항", "포항", "포항스틸러스"),
    ("kleague", "강원", "강원", "강원fc"), ("kleague", "인천", "인천", "인천유나이티드"),
    ("kleague", "대전", "대전", "대전하나시티즌"), ("kleague", "광주", "광주", "광주fc"),
    ("kleague", "제주", "제주", "제주sk", "제주유나이티드"), ("kleague", "김천", "김천", "김천상무"),
    ("kleague", "수원FC", "수원FC", "수원fc"), ("kleague", "대구", "대구", "대구fc"),
    ("kleague", "안양", "FC안양", "안양", "fc안양"),
    # K리그2 (소스 이름 = 짧은 한국어 — 승강이 있어 K리그1·2 는 서로의 이름표도 봄, canon())
    ("kleague2", "서울E", "서울 이랜드", "서울이랜드", "이랜드"), ("kleague2", "충남아산", "충남아산", "아산"),
    ("kleague2", "부천", "부천FC", "부천", "부천fc"), ("kleague2", "경남", "경남FC", "경남", "경남fc"),
    ("kleague2", "부산", "부산 아이파크", "부산", "부산아이파크"), ("kleague2", "성남", "성남FC", "성남", "성남fc"),
    ("kleague2", "안산", "안산 그리너스", "안산", "안산그리너스"), ("kleague2", "전남", "전남 드래곤즈", "전남", "전남드래곤즈"),
    ("kleague2", "수원삼성", "수원 삼성", "수원 블루윙즈"), ("kleague2", "천안", "천안시티", "천안"),
    ("kleague2", "충북청주", "충북청주", "청주"), ("kleague2", "김포", "김포FC", "김포", "김포fc"),
    # KBL (API-Sports 이름 2024-25 실측)
    ("kbl", "정관장", "안양 정관장", "정관장", "안양정관장", "kgc"), ("kbl", "소노", "고양 소노", "소노", "고양소노"),
    ("kbl", "KCC", "부산 KCC", "kcc", "부산kcc"), ("kbl", "가스공사", "한국가스공사", "가스공사", "대구 한국가스공사"),
    ("kbl", "LG", "창원 LG", "창원lg"), ("kbl", "현대모비스", "울산 현대모비스", "현대모비스", "모비스"),
    ("kbl", "SK", "서울 SK", "서울sk"), ("kbl", "삼성", "서울 삼성", "서울삼성", "삼성썬더스"),
    ("kbl", "KT", "수원 KT", "수원kt", "kt소닉붐"), ("kbl", "DB", "원주 DB", "원주db", "db프로미"),
    # WKBL
    ("wkbl", "BNK", "BNK 썸", "bnk", "bnk썸"), ("wkbl", "하나은행", "하나은행", "하나원큐"),
    ("wkbl", "KB", "KB스타즈", "kb스타즈", "kb국민은행"), ("wkbl", "신한은행", "신한은행", "신한", "에스버드"),
    ("wkbl", "삼성생명", "삼성생명", "삼성생명블루밍스"), ("wkbl", "우리은행", "우리은행", "우리은행위비"),
    # V리그 남자
    ("vleague", "OK저축은행", "OK저축은행", "ok저축은행", "읏맨"), ("vleague", "현대캐피탈", "현대캐피탈", "스카이워커스"),
    ("vleague", "대한항공", "대한항공", "점보스"), ("vleague", "KB손해보험", "KB손해보험", "kb손보", "케이비손해보험"),
    ("vleague", "한국전력", "한국전력", "한전", "빅스톰"), ("vleague", "삼성화재", "삼성화재", "블루팡스"),
    ("vleague", "우리카드", "우리카드", "우리카드우리won"),
    # V리그 여자
    ("wvleague", "정관장", "정관장", "정관장레드스파크스"), ("wvleague", "한국도로공사", "한국도로공사", "도로공사", "도공"),
    ("wvleague", "GS칼텍스", "GS칼텍스", "gs칼텍스"), ("wvleague", "흥국생명", "흥국생명", "핑크스파이더스"),
    ("wvleague", "현대건설", "현대건설", "현대건설힐스테이트"), ("wvleague", "IBK기업은행", "IBK기업은행", "ibk", "기업은행"),
    ("wvleague", "페퍼저축은행", "페퍼저축은행", "페퍼"),
]

# API-Sports 가 쓰는 영어 이름 → 위 표의 소스 이름 (실측 2026-10-01: /teams 2024 시즌 · 경기 응답).
# 소스와 상관없이 같은 팀은 같은 이름으로 저장·비교되게 (구독 sports_follow.team 은 소스 이름).
_APS_NAMES = {
    "kbo": {"KIA Tigers": "KIA", "LG Twins": "LG", "Hanwha Eagles": "한화", "Samsung Lions": "삼성", "Doosan Bears": "두산",
            "Lotte Giants": "롯데", "KT Wiz Suwon": "KT", "KT Wiz": "KT", "SSG Landers": "SSG", "NC Dinos": "NC",
            "Kiwoom Heroes": "키움"},
    "kleague": {"Gangwon FC": "강원", "Daegu FC": "대구", "Daejeon Citizen": "대전", "Suwon City FC": "수원FC",
                "Gwangju FC": "광주", "Jeju United FC": "제주", "Jeonbuk Motors": "전북", "Incheon United": "인천",
                "Pohang Steelers": "포항", "FC Seoul": "서울", "Ulsan Hyundai FC": "울산", "Gimcheon Sangmu FC": "김천",
                "FC Anyang": "안양"},
    "kleague2": {"Seoul E-Land FC": "서울E", "Asan Mugunghwa": "충남아산", "Bucheon FC 1995": "부천", "Gyeongnam FC": "경남",
                 "Busan I Park": "부산", "Seongnam FC": "성남", "Ansan Greeners": "안산", "Jeonnam Dragons": "전남",
                 "Suwon Bluewings": "수원삼성", "Cheonan City": "천안", "Cheongju": "충북청주", "Gimpo Citizen": "김포",
                 "FC Anyang": "안양"},
    "kbl": {"Anyang JungKwanJang": "정관장", "Goyang Sono": "소노", "KCC Egis": "KCC", "KoGas": "가스공사", "LG Sakers": "LG",
            "Mobis Phoebus": "현대모비스", "Seoul Knights": "SK", "Seoul Thunders": "삼성", "Suwon KT": "KT", "Wonju DB": "DB"},
    "wkbl": {"Busan BNK Sum W": "BNK", "Hana Bank W": "하나은행", "KB Stars W": "KB", "S-Birds W": "신한은행",
             "Samsung Blue Minx W": "삼성생명", "Woori WON W": "우리은행"},
    "vleague": {"Ansan OK": "OK저축은행", "Hyundai Skywalkers": "현대캐피탈", "KAL Jumbos": "대한항공", "KB Stars": "KB손해보험",
                "Kepco": "한국전력", "Samsung Blue Fangs": "삼성화재", "Woori": "우리카드"},
    "wvleague": {"Daejeon JKJ W": "정관장", "Expressway Co W": "한국도로공사", "GS Caltex W": "GS칼텍스", "Hungkuk W": "흥국생명",
                 "Hyundai E&C Hillstate W": "현대건설", "IBK W": "IBK기업은행", "Pepper Savings Bank W": "페퍼저축은행"},
}
# 승강이 있는 리그끼리는 서로의 이름표도 봄
_FAMILY = {"kleague": ("kleague", "kleague2"), "kleague2": ("kleague2", "kleague")}


@dataclass(frozen=True)
class Team:
    league: str
    src: str        # 소스가 쓰는 이름 (ESPN displayName / 네이버 팀 이름)
    ko: str         # 한국어 표시

    @property
    def key(self) -> str:
        return f"{self.league}:{self.src}"


TEAMS: list[Team] = [Team(t[0], t[1], t[2]) for t in _T]
_TEAM_ALIAS: dict[str, Team] = {}
_KO_OF: dict[str, str] = {}
_KO_BY: dict[tuple[str, str], str] = {}     # (리그, 소스 이름) → 한국어 — 리그가 다르면 같은 '삼성'·'KT' 도 다른 팀
for _t, _row in zip(TEAMS, _T):
    for _a in (_row[1], _row[2]) + tuple(_row[3:]):
        _TEAM_ALIAS.setdefault(norm(_a), _t)
    _KO_OF.setdefault(norm(_row[1]), _row[2])
    _KO_BY[(_row[0], norm(_row[1]))] = _row[2]
_APS_CANON = {lg: {norm(k): v for k, v in names.items()} for lg, names in _APS_NAMES.items()}


# 국가대표 팀 (ESPN displayName → 한국어). 클럽 이름표 뒤에 setdefault — 클럽과 겹치면 클럽 우선
_NATIONS = {
    "South Korea": "대한민국", "Korea Republic": "대한민국", "Japan": "일본", "China PR": "중국", "China": "중국",
    "Australia": "호주", "Iran": "이란", "Saudi Arabia": "사우디", "Qatar": "카타르", "Iraq": "이라크",
    "United Arab Emirates": "UAE", "Uzbekistan": "우즈베키스탄", "Jordan": "요르단", "Oman": "오만", "Syria": "시리아",
    "Lebanon": "레바논", "Vietnam": "베트남", "Thailand": "태국", "Indonesia": "인도네시아", "Maldives": "몰디브",
    "North Korea": "북한", "Kuwait": "쿠웨이트", "Bahrain": "바레인", "Palestine": "팔레스타인", "India": "인도",
    "England": "잉글랜드", "France": "프랑스", "Germany": "독일", "Spain": "스페인", "Italy": "이탈리아",
    "Portugal": "포르투갈", "Netherlands": "네덜란드", "Belgium": "벨기에", "Croatia": "크로아티아", "Denmark": "덴마크",
    "Switzerland": "스위스", "Austria": "오스트리아", "Poland": "폴란드", "Sweden": "스웨덴", "Norway": "노르웨이",
    "Serbia": "세르비아", "Scotland": "스코틀랜드", "Wales": "웨일스", "Ireland": "아일랜드",
    "Republic of Ireland": "아일랜드", "Northern Ireland": "북아일랜드", "Turkey": "튀르키예", "Türkiye": "튀르키예",
    "Greece": "그리스", "Czechia": "체코", "Czech Republic": "체코", "Ukraine": "우크라이나", "Hungary": "헝가리",
    "Romania": "루마니아", "Slovakia": "슬로바키아", "Slovenia": "슬로베니아", "Finland": "핀란드", "Iceland": "아이슬란드",
    "Albania": "알바니아", "Georgia": "조지아", "Bosnia-Herzegovina": "보스니아", "North Macedonia": "북마케도니아",
    "Montenegro": "몬테네그로", "Bulgaria": "불가리아", "Israel": "이스라엘", "Kosovo": "코소보", "Armenia": "아르메니아",
    "Azerbaijan": "아제르바이잔", "Kazakhstan": "카자흐스탄", "Liechtenstein": "리히텐슈타인", "Luxembourg": "룩셈부르크",
    "Cyprus": "키프로스", "Estonia": "에스토니아", "Latvia": "라트비아", "Lithuania": "리투아니아", "Belarus": "벨라루스",
    "Moldova": "몰도바", "Malta": "몰타", "Andorra": "안도라", "San Marino": "산마리노", "Gibraltar": "지브롤터",
    "Faroe Islands": "페로 제도",
    "Brazil": "브라질", "Argentina": "아르헨티나", "Uruguay": "우루과이", "Colombia": "콜롬비아", "Chile": "칠레",
    "Peru": "페루", "Ecuador": "에콰도르", "Paraguay": "파라과이", "Bolivia": "볼리비아", "Venezuela": "베네수엘라",
    "Mexico": "멕시코", "United States": "미국", "USA": "미국", "Canada": "캐나다", "Costa Rica": "코스타리카",
    "Panama": "파나마", "Jamaica": "자메이카", "Honduras": "온두라스",
    # 북중미 네이션스리그 (ESPN 실측 2026-10-02 + 콘카카프 회원국)
    "El Salvador": "엘살바도르", "Guatemala": "과테말라", "Haiti": "아이티", "Trinidad and Tobago": "트리니다드토바고",
    "Curacao": "퀴라소", "Curaçao": "퀴라소", "Suriname": "수리남", "Nicaragua": "니카라과", "Cuba": "쿠바",
    "Dominican Republic": "도미니카공화국", "Martinique": "마르티니크", "Guadeloupe": "과들루프", "St. Lucia": "세인트루시아",
    "Saint Lucia": "세인트루시아", "St. Kitts and Nevis": "세인트키츠네비스", "Grenada": "그레나다", "Bermuda": "버뮤다",
    "Puerto Rico": "푸에르토리코", "French Guiana": "프랑스령 기아나", "Guyana": "가이아나", "Belize": "벨리즈",
    "Barbados": "바베이도스", "Antigua and Barbuda": "앤티가바부다", "Aruba": "아루바", "Bonaire": "보네르",
    "St. Vincent and the Grenadines": "세인트빈센트그레나딘", "Dominica": "도미니카연방", "Montserrat": "몬트세랫",
    "Cayman Islands": "케이맨제도", "Bahamas": "바하마", "Turks and Caicos Islands": "터크스케이커스",
    "US Virgin Islands": "미국령 버진아일랜드", "British Virgin Islands": "영국령 버진아일랜드", "Anguilla": "앵귈라",
    "Sint Maarten": "신트마르턴", "Saint Martin": "생마르탱",
    "Morocco": "모로코", "Senegal": "세네갈", "Egypt": "이집트", "Nigeria": "나이지리아", "Ghana": "가나",
    "Cameroon": "카메룬", "Algeria": "알제리", "Tunisia": "튀니지", "Ivory Coast": "코트디부아르", "Cote d'Ivoire": "코트디부아르",
    "Mali": "말리", "South Africa": "남아공", "Guinea": "기니", "Kenya": "케냐", "Burkina Faso": "부르키나파소",
    "DR Congo": "콩고민주공화국", "Zambia": "잠비아", "Cape Verde": "카보베르데", "Gabon": "가봉", "Angola": "앙골라",
    "New Zealand": "뉴질랜드",
}
for _en, _ko in _NATIONS.items():
    _KO_OF.setdefault(norm(_en), _ko)


def ko_name(name: str, league: str = "") -> str:
    """소스 이름 → 한국어 (모르면 그대로). 리그를 주면 그 리그의 표 먼저 (KBL '삼성' = 서울 삼성)."""
    n = norm(name)
    if league:
        for lg in _FAMILY.get(league, (league,)):
            if (lg, n) in _KO_BY:
                return _KO_BY[(lg, n)]
    return _KO_OF.get(n, name or "?")


def canon(league: str, name: str) -> str:
    """API-Sports 영어 팀 이름 → 이 표의 소스 이름 (모르면 그대로). 승강 리그는 같은 가족 표도 봄."""
    n = norm(name)
    for lg in _FAMILY.get(league, (league,)):
        if n in _APS_CANON.get(lg, {}):
            return _APS_CANON[lg][n]
    return name


def find_team(text: str) -> Team | None:
    """정확한 별칭 → 없으면 앞부분이 같은 것 한 개일 때만 (예: '맨체스터 시' → 맨시티)."""
    q = norm(text)
    if not q:
        return None
    if q in _TEAM_ALIAS:
        return _TEAM_ALIAS[q]
    if len(q) < 3:
        return None
    hits = [(not a.startswith(q), len(a), t) for a, t in _TEAM_ALIAS.items() if a.startswith(q) or q in a]
    if not hits:
        return None
    best = min(h[:2] for h in hits)            # 앞부분이 같은 것 먼저, 그다음 가장 짧은 이름 (국내 농구·배구 팀이 늘어도
    top = {h[2] for h in hits if h[:2] == best}  # '울산현' → 울산 현대(K리그), '레이커' → 레이커스 그대로)
    return next(iter(top)) if len(top) == 1 else None


def same_team(name: str, team_src: str, league: str = "") -> bool:
    """경기의 팀 이름이 구독한 팀인지 (소스 이름 정확히 또는 한국어 이름으로)."""
    n = norm(name)
    return bool(n) and (n == norm(team_src) or norm(ko_name(name, league)) == norm(ko_name(team_src, league)))
