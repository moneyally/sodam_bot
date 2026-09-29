"""리그·종목·팀 이름표 (한국어 별칭 ↔ 데이터 소스 이름). 코드만 — 네트워크 없음.

리그 키(code)는 DB(sports_follow.league)에 저장되므로 바꾸지 말 것. 소스별 이름:
- espn  = site.api.espn.com 경로 '{sport}/{league}' (실측 2026-09-29, tests/fixtures/sports)
- naver = api-gw.sports.naver.com categoryId (실측 2026-09-29, 기본 꺼짐 SPORTS_NAVER=1)
- sdb   = TheSportsDB 리그 ID (유료 키일 때만)
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
    aliases: tuple[str, ...] = field(default=())
    korean: bool = False      # 국내 리그 (ESPN 에 없음)


SPORT_EMOJI = {"soccer": "⚽", "baseball": "⚾", "basketball": "🏀", "volleyball": "🏐", "hockey": "🏒", "mma": "🥊"}
SPORT_KO = {"soccer": "축구", "baseball": "야구", "basketball": "농구", "volleyball": "배구", "hockey": "아이스하키",
            "mma": "격투기"}
SPORT_WORDS = {"축구": "soccer", "해외축구": "soccer", "soccer": "soccer", "football": "soccer", "야구": "baseball",
               "baseball": "baseball", "농구": "basketball", "basketball": "basketball", "배구": "volleyball",
               "volleyball": "volleyball", "하키": "hockey", "아이스하키": "hockey", "hockey": "hockey",
               "격투기": "mma", "종합격투기": "mma", "mma": "mma"}
# 골(득점 하나하나) 알림이 의미 있는 종목. 나머지(야구·농구·배구)는 기본 시작·결과만 (득점이 너무 잦음)
GOAL_SPORTS = {"soccer", "hockey"}

_L = [
    League("epl", "EPL", "soccer", "soccer/eng.1", "epl", "4328",
           ("epl", "프리미어리그", "프리미어", "프리미어 리그", "premier league", "잉글랜드", "prem", "프리미어리그epl")),
    League("laliga", "라리가", "soccer", "soccer/esp.1", "primera", "4335",
           ("라리가", "la liga", "laliga", "스페인", "프리메라리가")),
    League("seriea", "세리에A", "soccer", "soccer/ita.1", "seria", "4332",
           ("세리에a", "세리에", "serie a", "seriea", "이탈리아")),
    League("bundesliga", "분데스리가", "soccer", "soccer/ger.1", "bundesliga", "4331",
           ("분데스리가", "분데스", "bundesliga", "독일")),
    League("ligue1", "리그1", "soccer", "soccer/fra.1", "ligue1", "4334",
           ("리그1", "리그앙", "ligue1", "ligue 1", "프랑스")),
    League("ucl", "챔스", "soccer", "soccer/uefa.champions", "champs", "4480",
           ("ucl", "챔스", "챔피언스리그", "챔피언스 리그", "champions league", "챔스리그")),
    League("uel", "유로파", "soccer", "soccer/uefa.europa", None, "4481",
           ("uel", "유로파", "유로파리그", "europa league")),
    League("jleague", "J리그", "soccer", "soccer/jpn.1", None, "4633", ("j리그", "j1", "제이리그", "일본축구", "j league")),
    League("kleague", "K리그1", "soccer", None, "kleague", "4689",
           ("k리그", "k리그1", "케이리그", "kleague", "k league", "k리그 1"), korean=True),
    League("kleague2", "K리그2", "soccer", None, "kleague2", None, ("k리그2", "케이리그2", "k league 2"), korean=True),
    League("mlb", "MLB", "baseball", "baseball/mlb", "mlb", "4424",
           ("mlb", "메이저리그", "메이저 리그", "엠엘비", "미국야구", "빅리그")),
    League("kbo", "KBO", "baseball", None, "kbo", "4830", ("kbo", "크보", "국야", "프로야구", "한국야구"), korean=True),
    League("npb", "NPB", "baseball", None, "npb", "4591", ("npb", "일본야구", "일야", "일본 프로야구"), korean=True),
    League("nba", "NBA", "basketball", "basketball/nba", "nba", "4387", ("nba", "엔비에이", "미국농구", "미농")),
    League("kbl", "KBL", "basketball", None, "kbl", None, ("kbl", "프로농구", "한국농구", "남자농구"), korean=True),
    League("wkbl", "WKBL", "basketball", None, "wkbl", None, ("wkbl", "여자농구", "여농"), korean=True),
    League("vleague", "V리그 남자", "volleyball", None, "kovo", None,
           ("v리그", "브이리그", "kovo", "남자배구", "v리그 남자", "v리그남자", "남배"), korean=True),
    League("wvleague", "V리그 여자", "volleyball", None, "wkovo", None,
           ("여자배구", "v리그 여자", "v리그여자", "wkovo", "여배"), korean=True),
    League("nhl", "NHL", "hockey", "hockey/nhl", None, "4380", ("nhl", "북미하키")),
    League("ufc", "UFC", "mma", "mma/ufc", "ufc", None, ("ufc", "유에프씨")),
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


def find_league(text: str) -> League | None:
    return LEAGUES.get(_ALIAS.get(norm(text), ""))


def find_sport(text: str) -> str | None:
    return SPORT_WORDS.get(norm(text))


def leagues_of_sport(sport: str) -> list[League]:
    return [lg for lg in _L if lg.sport == sport]


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
]


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
for _t, _row in zip(TEAMS, _T):
    for _a in (_row[1], _row[2]) + tuple(_row[3:]):
        _TEAM_ALIAS.setdefault(norm(_a), _t)
    _KO_OF[norm(_row[1])] = _row[2]


def ko_name(name: str) -> str:
    """소스 이름 → 한국어 (모르면 그대로)."""
    return _KO_OF.get(norm(name), name or "?")


def find_team(text: str) -> Team | None:
    """정확한 별칭 → 없으면 앞부분이 같은 것 한 개일 때만 (예: '맨체스터 시' → 맨시티)."""
    q = norm(text)
    if not q:
        return None
    if q in _TEAM_ALIAS:
        return _TEAM_ALIAS[q]
    hits = {t for a, t in _TEAM_ALIAS.items() if len(q) >= 3 and (a.startswith(q) or q in a)}
    return next(iter(hits)) if len(hits) == 1 else None


def same_team(name: str, team_src: str) -> bool:
    """경기의 팀 이름이 구독한 팀인지 (소스 이름 정확히 또는 한국어 이름으로)."""
    n = norm(name)
    return bool(n) and (n == norm(team_src) or norm(ko_name(name)) == norm(ko_name(team_src)))
