"""🎵 신청 글 ↔ 곡 제목 맞추기 (무거운 import 없음 — 테스트·봇·음성 담당 같이).

2026-10-08 실제 신청을 서버에서 다시 돌려 찾은 구멍 (tests/test_music_match.py):
  · 기본 음원 검색 맨 위를 확인 없이 믿음 → '김연지 별이될께' = 다른 노래('미친 사랑의 노래'), '이보람 …' = 라이브 클립
  · 대체 음원 보조 낱말이 가수 이름만 남음 → 같은 가수 다른 노래('디셈버 이별연습')
  · 낱말 수 감점이 원곡(영어 이름 붙음)을 깎음 → 'By 다른 가수' 커버가 이김 ('조정석 아로하')
  · '좋은 발라드' 같은 신청 → 노래 이름 없이도 통과, 점수 -2 도 뽑힘
그래서:
  1) 신청 정리: 틀어줘 같은 말 빼고, 커버·라이브… 원하는 버전(intent), 모음·일반 말(장르·좋은)만이면 되묻기.
  2) 기본 음원 후보: 신청 낱말을 '전부' 담은 곡만 바로 틂 (원하는 버전이 아니면 빼고, 방송 무대는 뒤로).
     전부 담은 곡이 없거나 커버·라이브를 원하면 → 고르기 버튼 (엉뚱한 곡을 트느니 사람이 고름).
  3) 대체 음원: 기준 곡 제목을 '가수 / 노래'로 나눠 두 쪽 다 맞아야 (같은 가수 다른 노래·같은 노래 다른 가수 X).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

# ── 낱말 ──────────────────────────────────────────────
def norm(text: str) -> str:
    """소문자 + 악센트 뺌 (Beyoncé → beyonce), 한글은 다시 붙임, 전각 → 반각."""
    t = unicodedata.normalize("NFKD", (text or "").lower())
    out = []
    for c in t:                         # 라틴 글자 위 악센트만 뗌 — 가나 탁점(ゼ→セ)·한글은 그대로
        if unicodedata.combining(c) and out and out[-1].isascii():
            continue
        out.append(c)
    return unicodedata.normalize("NFC", "".join(out))


def _one_ok(w: str) -> bool:
    """한 글자 낱말은 한글·한자·가나만 ('넋'·'별'·'말' — 영어 한 글자·숫자 한 개는 버림)."""
    return len(w) >= 2 or bool(re.match(r"[぀-ヿ㐀-鿿가-힣]", w))


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[^\W_]+", norm(text)) if _one_ok(w)]


def compact(text: str) -> str:
    return "".join(re.findall(r"[^\W_]+", norm(text)))


# ── 신청 정리 ──────────────────────────────────────────
STOP = {"틀어", "틀어줘", "틀어주세요", "틀어봐", "틀어줄래", "재생", "재생해줘", "신청", "신청곡", "좀", "부탁", "부탁해", "들려줘",
        "플레이", "play", "해줘", "줘", "노래좀"}
INTENT = {   # 원하는 버전 — 이 말이 있으면 그 버전도 받고, 고르기 버튼으로
    "cover": ("커버", "cover", "커버곡", "covered"),
    "live": ("라이브", "live", "콘서트", "concert", "직캠"),
    "remix": ("리믹스", "remix"),
    "inst": ("반주", "mr", "inst", "instrumental", "노래방", "karaoke"),
    "piano": ("피아노", "piano"),
    "acoustic": ("어쿠스틱", "acoustic"),
}
COMPILATION = ("모음", "노래모음", "메들리", "medley", "플리", "플레이리스트", "playlist", "연속듣기", "1시간")
GENERIC = {"노래", "음악", "곡", "좋은", "좋은노래", "발라드", "신나는", "슬픈", "최신", "최신곡", "명곡", "추천", "아무거나", "아무",
           "아무노래", "인기", "인기곡", "요즘", "옛날", "감성", "잔잔한", "띵곡", "kpop", "케이팝", "트로트", "힙합", "댄스",
           "pop", "song", "songs", "music", "best", "top", "hits", "노래들", "한곡", "하나", "다른", "다른노래", "아무곡"}


@dataclass
class Req:
    text: str                                        # 사람이 쓴 그대로 (정리 전)
    must: list[str] = field(default_factory=list)    # 곡 제목에 꼭 있어야 하는 낱말 (가수·제목)
    intent: str = ""                                 # cover·live·… (없으면 원곡)
    compilation: bool = False
    generic: bool = False                            # 가수·제목이 하나도 없음

    @property
    def allowed(self) -> str:
        """버전 판단 때 '신청에 있는 말' — 신청에 '라이브'가 있으면 라이브 곡은 변형으로 안 셈."""
        return norm(self.text)


def search_text(text: str) -> str:
    """검색어 = 신청 글에서 '틀어줘'·'좀' 같은 말만 뺀 것 (나머지 글자·순서·'-' 는 그대로 — 검색 엔진이 더 잘 앎)."""
    out = " ".join(t for t in (text or "").split() if norm(t).strip(".,!?~") not in STOP)
    return re.sub(r"\s+", " ", out).strip() or (text or "").strip()


def parse(text: str) -> Req:
    r = Req(text=search_text(text))
    ws = [w for w in words(r.text) if w not in STOP]
    low = norm(r.text)
    for kind, keys in INTENT.items():
        if any(k in ws for k in keys):
            r.intent = r.intent or kind
    intent_words = {k for keys in INTENT.values() for k in keys}
    r.compilation = any(k in low.replace(" ", "") for k in COMPILATION)
    must = [w for w in ws if w not in intent_words and not any(k in w for k in COMPILATION)]
    r.must = must
    r.generic = not [w for w in must if w not in GENERIC]
    return r


# ── 제목에 낱말이 있나 ───────────────────────────────────


def _jamo(s: str) -> str:
    """한글 음절 → 자모 (별이될께 ↔ 별이될게 처럼 받침·모음 하나 다른 걸 비슷하게 보려고)."""
    return unicodedata.normalize("NFD", s)


class Title:
    """후보 곡 제목 한 개 — 비교용으로 한 번만 정리."""

    def __init__(self, title: str):
        self.raw = title or ""
        self.norm = norm(self.raw)
        self.compact = compact(self.raw)
        self.tokens = set(words(self.raw))
        self.ascii_tokens = [t for t in self.tokens if t.isascii()]

    def hit(self, w: str) -> bool:
        if not w:
            return False
        if len(w) == 1:                                   # 한 글자 ('넋'·'별') — 낱말 그대로만
            return w in self.tokens
        if w.isascii():
            if len(w) <= 3:                               # 'all' ⊄ 'allow', '2am' — 낱말 단위
                return w in self.tokens
            if w in self.compact:
                return True
            if len(w) >= 5:                               # 'drowing' ≈ 'drowning'
                return any(SequenceMatcher(None, w, t).ratio() >= 0.85 for t in self.ascii_tokens if abs(len(t) - len(w)) <= 2)
            return False
        if w in self.compact:                             # 한글은 붙여 쓴 것도 ('너는바다' ⊂ '너는 바다')
            return True
        if len(w) >= 4:                                   # '별이될께' ≈ '별이될게' (자모 비교, 같은 길이만 —
            jw, n = _jamo(w), len(w)                       #  3글자 이름은 안 함: '김연지' ≠ '김연아')
            for i in range(0, max(0, len(self.compact) - n) + 1):
                seg = self.compact[i:i + n]
                if not seg.isascii() and SequenceMatcher(None, jw, _jamo(seg)).ratio() >= 0.85:
                    return True
        return False

    def hits(self, ws) -> int:
        return sum(1 for w in ws if self.hit(w))

    def covers(self, req: Req) -> bool:
        """신청 낱말 전부 — 띄어쓰기만 다른 것('말 달리자' ↔ '말달리자')도."""
        if not req.must:
            return False
        if self.hits(req.must) == len(req.must):
            return True
        joined = "".join(req.must)
        return len(joined) >= 3 and joined in self.compact


# ── 버전 (원곡 아님) ─────────────────────────────────────
VARIANTS = {
    "cover": ("cover", "covered", "커버", "원곡", "부른", "불러", "부르는", "ai cover", "original song", "original by", "orig by"),   # '(Original Song by IU)' = 다른 가수
    "live": ("live", "라이브", "콘서트", "concert", "직캠", "fancam", "앵콜"),
    "remix": ("remix", "리믹스", "slowed", "sped up", "speed up", "nightcore", "8d", "reverb", "bass boost", "phonk", "lofi",
              "lo-fi", "mashup", "bootleg", "jersey", "disco", "버전", "트로트 버전", "mix)"),
    "inst": ("inst", "instrumental", "karaoke", "tj노래방", "노래방 버전", "반주", "mr", "melody", "멜로디"),   # '[정승환의 노래방 옆 만화방]' 은 방송 이름
    "piano": ("piano", "피아노", "guitar", "기타커버", "cello", "violin", "kalimba", "orgel", "music box", "8bit", "8-bit",
              "첼로", "바이올린", "오르골"),
    "acoustic": ("acoustic", "어쿠스틱"),
    "compilation": ("모음", "1시간", "1 hour", "1hour", "연속듣기", "연속 듣기", "메들리", "medley", "vlog", "브이로그", "모아듣기"),
}
KIND_LABEL = {"cover": "🎤 커버", "live": "🔴 라이브", "remix": "🎛 리믹스", "inst": "🎹 반주", "piano": "🎻 연주",
              "acoustic": "🎸 어쿠스틱", "compilation": "📚 모음", "": "🎵"}
BROADCAST = ("방송", "인기가요", "뮤직뱅크", "music bank", "엠카운트다운", "음악중심", "music core", "musiccore", "스케치북",
             "sketchbook", "더 시즌즈", "불후의", "복면가왕", "슈가맨", "가요대전", "kbs", "sbs", "mbc", "mnet", "jtbc", "stage", "무대",
             "i am a singer", "나는 가수다", "나가수", "히든싱어", "놀면 뭐하니", "비긴어게인", "begin again", "콘서트7080",
             "열린음악회", "판타스틱 듀오", "show champion", "the show")
QUALITY = ("가사", "lyrics", "lyric", "official", "audio", "mv", "m/v", "뮤직비디오", "music video")


GLUED = ("remix",)              # 붙여 써도 변형 ('miremix'·'xxxRemix' — 실측 'aespa Supernova miremix')
_ODD_VER = re.compile(r"[(\[][^)\]]*?ver(?:sion)?\b\.?[^)\]]*[)\]]", re.I)


def _has(text: str, key: str) -> bool:
    if key in GLUED:
        return key in text
    if key.isascii():
        return re.search(rf"(?<![0-9a-z]){re.escape(key)}(?![0-9a-z])", text) is not None
    return key in text


def kind(title: str, allowed: str = "", known: str = "") -> str:
    """원곡이 아니면 그 종류 (cover·live·…), 원곡이면 ''. allowed(신청 글)에 있는 말은 안 셈.
    'By 다른 사람'(실측: '아로하 (조정석) By 원진,민희 of CRAVITY')도 커버 — known(신청·기준 제목)에 그 이름이 있으면 원곡."""
    t = norm(title)
    a = norm(allowed)
    for k, keys in VARIANTS.items():
        if any(_has(t, key) and not _has(a, key) for key in keys):
            return k
    m = re.search(r"(?<![0-9a-z])by[.:]?\s+([^-\[\](){}|/·]{1,40})", t)
    if m and "by" not in a.split():
        who = set(words(m.group(1)))
        if who and not who & set(words(f"{allowed} {known}")):
            return "cover"
    return ""


def odd_version(title: str, ref: str = "") -> bool:
    """대체 음원에서 '(강희선성우ver)'·'(Acoustic Ver.)' 같은 다른 버전 — 기준 곡 제목에 같은 괄호가 있으면 괜찮음.
    (기본 음원 고를 땐 안 씀: 'Official MV (Performance ver.1)' 은 원곡 영상)"""
    m = _ODD_VER.search(norm(title))
    return bool(m) and compact(m.group(0)) not in compact(ref)


def quality(title: str) -> int:
    t = norm(title)
    return (2 if any(_has(t, k) for k in QUALITY) else 0) - (2 if any(_has(t, k) for k in BROADCAST) else 0)


# ── 기본 음원 후보 고르기 ─────────────────────────────────
@dataclass
class Pick:
    item: dict | None = None                 # 바로 틀 곡
    choices: list[dict] = field(default_factory=list)   # 사람이 고를 곡 (item 이 없을 때)
    reason: str = ""                         # partial(딱 맞는 곡 없음) · intent(버전 고르기) · none(후보 없음)


CHOICES = 4


def choose(req: Req, cands: list[dict], max_sec: int) -> Pick:
    """cands = [{id, title, duration, live_status}] (검색 순서). 바로 틀 곡 또는 고를 곡들."""
    ok = []
    for i, e in enumerate(cands):
        if not e or not e.get("id"):
            continue
        dur = int(e.get("duration") or 0)
        if e.get("live_status") in ("is_live", "is_upcoming") or dur > max_sec or (dur and dur < 30):
            continue
        k = kind(e.get("title") or "", req.allowed)
        if k == "compilation":
            continue
        t = Title(e.get("title") or "")
        ok.append({"e": e, "i": i, "kind": k, "full": t.covers(req), "hits": t.hits(req.must), "q": quality(e.get("title") or "")})
    if not ok:
        return Pick(reason="none")
    want = req.intent
    if not want:
        full = [x for x in ok if x["full"] and not x["kind"]]
        if full:                                 # 신청 낱말 전부 + 원곡 → 바로. 검색 순서대로, 방송 무대만 뒤로
            best = min(full, key=lambda x: (x["q"] < 0, x["i"]))       # ('Official' 가산은 X — 'lemon' → 다른 밴드 공식 영상)
            if best["q"] >= 0:                   # 방송 무대뿐이면 바로 틀지 않고 고르기 (실측: '악뮤 …' → 린&찬혁 방송 듀엣)
                return Pick(item=_item(best))
    # 고르기: 원하는 버전 먼저 → 신청 낱말 많이 → 원곡 → 검색 순서
    ranked = sorted(ok, key=lambda x: (-(x["kind"] == want if want else 0), -int(x["full"]), -x["hits"],
                                       int(bool(x["kind"])) if not want else 0, -x["q"], x["i"]))
    ranked = [x for x in ranked if x["hits"] or x["full"]] or ranked
    return Pick(choices=[_item(x) for x in ranked[:CHOICES]], reason="intent" if want else "partial")


def _item(x: dict) -> dict:
    e = x["e"]
    return {"vid": e["id"], "title": (e.get("title") or "")[:200], "duration": int(e.get("duration") or 0), "kind": x["kind"]}


# ── 대체 음원: 기준 곡과 같은 노래인가 ─────────────────────────
_SQUARE = re.compile(r"[\[【［][^\]】］]*[\]】］]")
_NOISE_PAREN = re.compile(r"[(（][^)）]*?(?:mv|m/v|official|lyrics?|가사|audio|video|live|4k|hd|remaster|ver\.?|version)[^)）]*[)）]", re.I)
_QUOTE = re.compile(r"['‘’\"“”「『〈＜<](.+?)['‘’\"“”」』〉＞>]")
_SEP = re.compile(r"\s+(?:-{1,2}|–|—|_|\||｜|/)\s+|\s*(?:–|—|｜)\s*|\s-|-\s")


def _part(text: str) -> tuple[list[str], list[str]]:
    """'Kim Na Young(김나영)' → (['kim','na','young'], ['김나영']) — 괄호 안은 다른 표기(번역)."""
    alt = [w for grp in re.findall(r"[(（]([^)）]*)[)）]", text) for w in words(grp)]
    main = words(re.sub(r"[(（][^)）]*[)）]", " ", text))
    return (main or alt), (alt if main else [])


def split(title: str) -> list[tuple[list[str], list[str]]]:
    """기준 곡 제목 → 부분들 [(낱말, 다른 표기)] — 보통 [가수, 노래] (순서는 모름, 둘 다 맞아야 함)."""
    t = _SQUARE.sub(" ", title or "")
    t = _NOISE_PAREN.sub(" ", t)
    t = re.sub(r"(?i)\b(?:official\s*(?:music\s*)?(?:video|mv|audio)|m/?v|lyrics?|lyric\s*video|가사)\b", " ", t)
    m = _QUOTE.search(t)
    if m:                                                  # NewJeans (뉴진스) 'Hype Boy' → [NewJeans (뉴진스)] [Hype Boy]
        head, song = t[:m.start()], m.group(1)
        parts = [head, song] if words(head) else [song]
    else:
        bits = [b for b in _SEP.split(t, maxsplit=1) if words(b)]
        parts = bits[:2] if bits else [t]
        if len(parts) == 2:                                # 'Happiness, For You 20060105' → 'Happiness'
            parts[1] = re.split(r"\s*[,/|]\s*|\s+-\s+", parts[1])[0]
    out = []
    for p in parts:
        main, alt = _part(p)
        main = [w for w in main if not w.isdecimal() or len(w) <= 3]   # 날짜 같은 긴 숫자 빼기 ('40'·'2am' 은 남김)
        if main:
            out.append((main, alt))
    return out


def _need(n: int) -> int:
    return n if n <= 2 else 2 if n == 3 else round(n * 0.7)


def _part_hit(t: Title, main: list[str], alt: list[str]) -> bool:
    """부분 하나가 맞나: 낱말 거의 전부 · 다른 표기 전부 · 붙여 쓴 것('크라잉 넛' ↔ '크라잉넛', '좋은 날' ↔ '좋은날')."""
    if t.hits(main) >= _need(len(main)) or (alt and t.hits(alt) == len(alt)):
        return True
    return any(len("".join(g)) >= 2 and "".join(g) in t.compact for g in (main, alt) if g)


def cand_parts_ok(cand_title: str, known: list[str]) -> bool:
    """후보 제목의 부분('가수 - 노래')마다 아는 낱말(기준 곡·신청)이 하나는 있어야 —
    '윈터 - 여인의 향기 (씨야)' 처럼 다른 가수 이름이 한쪽을 차지하면 커버 (실측)."""
    for main, alt in split(cand_title):
        t = Title(" ".join(main + alt))
        if not t.hits(known) and not any(w in t.compact for w in known if len(w) >= 2):
            return False
    return True


def touches_all_parts(cand_title: str, ref_title: str) -> bool:
    """기준 곡의 부분(가수·노래)마다 하나는 맞음 — 신청 낱말로 살릴 때도 가수 쪽이 아예 없으면 안 됨
    ('말달리자 (Run your horse)' ↔ 기준 '말달리자 -- 크라잉 넛': 같은 제목 다른 노래일 수 있음)."""
    t = Title(cand_title)
    return all(t.hits(main) or (alt and t.hits(alt)) or any("".join(g) in t.compact for g in (main, alt) if g)
               for main, alt in split(ref_title))


def extra_words(cand_title: str, known: str) -> int:
    """기준·신청에 없는 낱말 수 — 같은 점수일 때만 씀 (덜 붙은 쪽: '… 최정훈의 밤의공원' 같은 방송·다른 사람 이름이 뒤로)."""
    k = Title(known)
    return sum(1 for w in words(cand_title) if not k.hit(w))


def same_song(cand_title: str, ref_title: str, req: Req | None = None) -> tuple[bool, int]:
    """대체 음원 후보가 기준 곡과 같은 노래인가 → (맞나, 점수). 부분마다: 낱말 거의 전부 또는 다른 표기 전부."""
    t = Title(cand_title)
    parts = split(ref_title)
    if not parts:
        return False, 0
    known = [w for main, alt in parts for w in main + alt] + (req.must if req else [])
    if not cand_parts_ok(cand_title, known):
        return False, -1                       # 다른 가수 이름 부분 → 신청 낱말로도 살리지 않음 (alt_for)
    score = 0
    for main, alt in parts:
        got = t.hits(main)
        ok = _part_hit(t, main, alt)
        if not ok:
            return False, 0
        score += got + (t.hits(alt) if alt else 0)
    if len(parts) == 1:                        # 가수 쪽이 없는 기준 ('LOVE ALL (LOVE ALL)') → 낱말 순서까지 ('All Love' X)
        main, alt = parts[0]
        if "".join(main) not in t.compact and not (alt and "".join(alt) in t.compact):
            return False, 0
        ref = Title(ref_title)
        extra = [w for w in (req.must if req else []) if not ref.hit(w)]
        if extra and t.hits(extra) < len(extra):   # 신청한 가수('가비엔제이')는 있어야 ('Love All (feat. JAŸ-Z)' X)
            return False, 0
    if req and req.must:                       # 사람이 쓴 낱말 중 기준 제목에 없는 것(다른 표기)도 맞으면 가산
        score += t.hits(req.must)
    return True, score


def alt_ok(cand_title: str, req: Req) -> tuple[bool, int]:
    """신청 낱말 전부 (기준 곡이 없거나 기준 제목이 지저분할 때) — 일반 말만이면 안 됨.
    (가수를 안 쓴 신청('drivers license')도 있어서 다른 이름 부분 검사는 기준 곡이 있을 때만 — same_song)"""
    if req.generic or not req.must:
        return False, 0
    t = Title(cand_title)
    if not t.covers(req):
        return False, 0
    return True, t.hits(req.must)
