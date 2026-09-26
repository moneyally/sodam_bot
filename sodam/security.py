"""프롬프트 인젝션 탐지(1층 규칙) + 출력 필터 + 비신뢰 데이터 감싸기.

2층(AI 판별)은 agent/llm.py 의 classify_injection 이 담당한다.
"""
import re
import secrets
import unicodedata
from dataclasses import dataclass

_ZERO_WIDTH = re.compile("[​-‏⁠-⁤﻿­]")

# (패턴, 가중치, 이름). 합계 3 이상이면 차단, 1~2면 AI 판별로 넘긴다.
# 3점짜리는 평범한 대화에서 거의 안 나오는 표현만. 애매한 건 2점으로 두고 AI가 판단하게 한다.
# (tests/test_offline.py 의 NORMAL_CHAT 문장들이 걸리지 않아야 한다)
_IGNORE_VERB = r"(무시\s*(해|하고|하라|하세요|해라|해줘|할\s*것)|잊(어|고|어라|어줘|으세요)(?!\s*버))"
_RULES: list[tuple[re.Pattern, int, str]] = [
    (re.compile(p, re.I), w, name)
    for p, w, name in [
        (r"(이전|위의?|앞의?|기존|모든|지금까지의?)\s*(지시|명령|규칙|프롬프트|지침|설정)\S*\s*"
         r"(을|를|은|는)?\s*(다\s*|전부\s*|모두\s*)?" + _IGNORE_VERB, 3, "지시 무시"),
        (r"(ignore|disregard|forget)\s+(all\s+|the\s+|any\s+)?(previous|above|prior|earlier|your|system)\s+"
         r"(instructions?|rules?|prompts?|guidelines?)", 3, "ignore instructions"),
        (r"시스템\s*(프롬프트|메시지|지시|지침)", 2, "시스템 프롬프트 언급"),
        (r"system\s*(prompt|message)|developer\s*message", 2, "system prompt"),
        (r"(프롬프트|지시문|지침|너의?\s*설정|내부\s*규칙|초기\s*설정).{0,8}(보여|알려|출력|공개|말해|복사)", 2, "프롬프트 요구"),
        (r"(지금부터|이제부터)\s*(너는|넌|당신은)", 2, "역할 재정의"),
        (r"you\s+are\s+now|act\s+as\s+(an?\s+)?(unrestricted|jailbroken|dan)\b", 2, "role override"),
        (r"(개발자|관리자|디버그|갓|god|dan|탈옥)\s*모드|developer\s*mode|admin\s*mode|debug\s*mode", 2, "특수 모드"),
        (r"(탈옥|jailbreak|프롬프트\s*인젝션|prompt\s*injection)", 2, "탈옥 언급"),
        (r"(제한|필터|검열|안전\s*장치)\s*(없이|없는|해제|풀어|끄고|꺼)", 2, "제한 해제"),
        (r"(?<![가-힣])(나는|내가|난|나)\s*(이\s*방\s*)?(관리자|운영자|오너|방장)(야|다|임|입니다|이야|거든|이니까)", 2, "권한 사칭"),
        (r"(모두|전부|전원|모든\s*(사람|멤버|인원|유저|사용자))\s*(를|을|다)?\s*(밴|강퇴|추방)\s*(해|시켜|하)", 3, "대량 제재 요청"),
        (r"<\|?(im_start|im_end|system|endoftext)\|?>|\[/?(inst|system)\]|<<\s*sys\s*>>", 3, "제어 토큰"),
        (r"</?(chat_log|request|tool_result|speaker|user_memory|reply_to)\b", 3, "태그 위조"),
        (r"[A-Za-z0-9+/]{120,}={0,2}", 1, "긴 인코딩 문자열"),
    ]
]
# 줄 단위로 보는 규칙 (normalize 가 줄바꿈을 없애기 전에 검사)
_HEADER = re.compile(r"^\s*(system|assistant|developer|시스템)\s*[:：]", re.I | re.M)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _ZERO_WIDTH.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class ScanResult:
    score: int
    hits: list[str]

    @property
    def blocked(self) -> bool:
        return self.score >= 3

    @property
    def suspicious(self) -> bool:
        return 1 <= self.score < 3


def scan(text: str) -> ScanResult:
    norm = normalize(text)
    hits: list[str] = []
    score = 0
    lines = _ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", text))
    if _HEADER.search(lines):
        score += 3
        hits.append("역할 헤더 위조")
    for pattern, weight, name in _RULES:
        if pattern.search(norm):
            score += weight
            hits.append(name)
    return ScanResult(score, hits)


# ── 비신뢰 데이터 감싸기 ─────────────────────────────────
def nonce() -> str:
    return secrets.token_hex(4)


_TAG_LIKE = re.compile(r"</?\s*(chat_log|request|tool_result|speaker|user_memory|system)[^>]*>", re.I)


def defang(text: str) -> str:
    """사용자 글 안의 가짜 태그를 무력화."""
    return _TAG_LIKE.sub(lambda m: m.group(0).replace("<", "‹").replace(">", "›"), text)


def wrap(tag: str, body: str, n: str, **attrs: str) -> str:
    attr = "".join(f' {k}="{v}"' for k, v in attrs.items())
    return f'<{tag} id="{n}"{attr}>\n{defang(body)}\n</{tag} id="{n}">'


# ── 출력 필터 ─────────────────────────────────────────────
_URL = re.compile(r"(https?://\S+|www\.\S+|\b(t|telegram)\.me/\S+|\btg://\S+)", re.I)
_MENTION = re.compile(r"(?<![\w@])@([A-Za-z][A-Za-z0-9_]{3,31})")
_WALLETS = [
    re.compile(r"\bT[1-9A-HJ-NP-Za-km-z]{33}\b"),          # TRON
    re.compile(r"\b0x[a-fA-F0-9]{40}\b"),                   # EVM
    re.compile(r"\b(bc1|[13])[a-zA-HJ-NP-Z0-9]{25,62}\b"),  # BTC
]


def strip_unsafe(text: str, allowed_usernames: set[str] = frozenset()) -> str:
    """링크·지갑주소·외부 @멘션 제거 (길이는 건드리지 않음)."""
    text = _URL.sub("[링크 생략]", text)
    for w in _WALLETS:
        text = w.sub("[주소 생략]", text)
    return _MENTION.sub(
        lambda m: m.group(0) if m.group(1).lower() in allowed_usernames else m.group(1), text)


def filter_output(text: str, *, max_chars: int, allowed_usernames: set[str],
                  secret_words: tuple[str, ...] = ()) -> str:
    """AI 답변을 방에 보내기 전 마지막 검사. 링크·지갑주소·외부 @멘션·비밀값 제거 + 길이 제한."""
    text = strip_unsafe(text, allowed_usernames)
    for word in secret_words:
        if word and word in text:
            text = text.replace(word, "○" * len(word))
    text = text.strip()
    if len(text) > max_chars:
        cut = text[:max_chars]
        # 문장 끝에서 자르기
        for mark in ("\n", ". ", "다. ", "요. ", "! ", "? "):
            idx = cut.rfind(mark)
            if idx > max_chars * 0.6:
                cut = cut[: idx + len(mark)]
                break
        text = cut.rstrip() + "\n…(더 궁금하시면 '더 알려줘' 해주세요)"
    return text or "음… 뭐라고 답해야 할지 모르겠어요."


# ── 링크 탐지 (도배/링크 필터용) ──────────────────────────
_ANY_LINK = re.compile(
    r"(https?://|www\.|\b(t|telegram)\.me/|\b[a-z0-9-]+\.(com|net|org|io|xyz|me|kr|co|site|top|app|link|cc|gg)\b)",
    re.I)
_DOMAIN = re.compile(r"(?:https?://)?(?:www\.)?([a-z0-9.-]+\.[a-z]{2,})", re.I)


def find_links(text: str) -> list[str]:
    if not _ANY_LINK.search(text):
        return []
    return [d.lower() for d in _DOMAIN.findall(text)] or ["link"]


def link_allowed(domains: list[str], whitelist: list[str]) -> bool:
    if not domains:
        return True
    return all(any(d == w or d.endswith("." + w) for w in whitelist) for d in domains)
