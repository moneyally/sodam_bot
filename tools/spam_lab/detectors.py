"""규칙 탐지기 A · 여러 방 지문 탐지기 B (AI 비용 0).

각 탐지기: unit → {"score": float, "reasons": [...], ...}. 판정 문턱은 evaluate.py 에서 dev 로 정한다.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict

from sodam import accountage
from sodam.scamguard import _INVITE
from sodam.security import WALLETS, find_links, find_mentions, normalize

BOT_USERNAMES = {"sodam_ai_bot"}

# ── A. 규칙 ───────────────────────────────────────────────
LURE = re.compile(r"(dm|디엠|갠톡|개인\s*톡|개인\s*메(시|세)지|1:1|개인적으로\s*연락|먼저\s*연락|말\s*걸어|프로필|바이오|소개란|프사)", re.I)
PROMISE = re.compile(r"(수익\s*보장|원금\s*보장|확정\s*수익|손실\s*(없|0)|\d+\s*배\s*(로\s*)?(돌려|반환|지급)|두\s*배|2배|"
                     r"일\s*\d+\s*%|월\s*\d+\s*%|하루\s*\d+\s*~?\s*\d*\s*%|적중률)")
IMPERSONATE = re.compile(r"(운영진|운영팀|관리자(입니다|공지)|방장(입니다|님\s*(부탁|지인|이\s*대신))|고객\s*센터|공식\s*(상담|지원|담당)|"
                         r"보안\s*팀|지원\s*팀|인증.{0,6}(필요|완료|하세요|해\s*주)|복구\s*(문구|구문)|시드\s*(구문|문구)|강퇴됩니다)")
RECRUIT = re.compile(r"(일당|고액\s*알바|부업|통장\s*대여|계좌\s*(매입|대여)|체크\s*카드.{0,6}빌려|현금\s*수거|해외\s*취업|숙식\s*제공)")
WARN_CTX = re.compile(r"(조심|사기(입니다|예요|에요|임)|믿지\s*마|속지\s*마|가짜|주의|신고)")
NAME_IMP = re.compile(r"(관리자|운영|admin|support|고객\s*센터|상담원|보안|official|공식|방장)", re.I)
_EMOJI = re.compile(r"[\U0001F300-\U0001FAFF☀-➿]")


def _tokens(text: str) -> dict[str, set[str]]:
    norm = normalize(text)
    wallets = {m.group(0) for w in WALLETS for m in w.finditer(norm)}
    invites = {m.lower() for m in re.findall(r"(?:t|telegram)\.me/[+\w-]+", norm, re.I)}
    links = set(find_links(norm)) - {"t.me", "telegram.me"}
    ids = {m for m in find_mentions(norm) if m not in BOT_USERNAMES}
    return {"wallet": wallets, "invite": invites, "link": links, "id": ids}


def rules(unit: dict, now: float) -> dict:
    """신규 입장자의 처음 N개 메시지에 대한 규칙 점수. hard = 혼자서도 강한 신호(지갑·초대링크·사칭 문구·약속+유인)."""
    reasons: list[str] = []
    score = 0.0
    hard = False
    text = "\n".join(unit["msgs"])
    tok = _tokens(text)
    replied = any(unit.get("reply") or [])
    warn = bool(WARN_CTX.search(text))
    if tok["wallet"]:
        if replied:
            score += 1; reasons.append("지갑(답장 안)")
        else:
            score += 3; hard = True; reasons.append("지갑주소")
    if tok["invite"] or _INVITE.search(normalize(text)):
        score += 3; hard = True; reasons.append("초대링크")
    if tok["link"]:
        score += 1; reasons.append("외부링크")
    if tok["id"]:
        score += 1; reasons.append("@아이디")
    if LURE.search(text) and not replied:
        score += 1; reasons.append("DM 유도")
    if PROMISE.search(text) and not warn:
        score += 2; reasons.append("수익 약속")
    if IMPERSONATE.search(text) and not warn:
        score += 3; hard = True; reasons.append("사칭 문구")
    if RECRUIT.search(text) and not warn:
        score += 2; reasons.append("모집")
    if "수익 약속" in reasons and "DM 유도" in reasons:
        hard = True
    # 프로필
    p = unit["profile"]
    recent = accountage.is_recent(p["user_id"], now=now)
    if recent:
        score += 0.5; reasons.append("최근 계정")
    if not p.get("username"):
        score += 0.5; reasons.append("아이디 없음")
    if NAME_IMP.search(p.get("name") or ""):
        score += 2; reasons.append("이름 사칭형"); hard = True
    if len(_EMOJI.findall(p.get("name") or "")) >= 2:
        score += 0.5; reasons.append("이모지 이름")
    return {"score": score, "reasons": reasons, "hard": hard, "recent": recent}


# ── B. 여러 방 지문 ───────────────────────────────────────
def shingles(text: str, k: int = 3) -> set[str]:
    t = unicodedata.normalize("NFKC", text).lower()
    t = re.sub(r"https?://\S+|t\.me/\S+|@\w+|\b0x[0-9a-f]{40}\b|\bT[1-9A-HJ-NP-Za-km-z]{33}\b", " ", t)
    t = re.sub(r"[^\w가-힣]+", "", t)
    return {t[i:i + k] for i in range(max(len(t) - k + 1, 0))}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


class CrossRoom:
    """모든 메시지(실제+주입)를 시간순으로 두고, 한 단위의 메시지가 W초 안에 '다른 방'에 올라온 글과
    (1) 같은 링크·초대·@아이디·지갑 토큰, (2) 글자 3-gram Jaccard ≥ J(거의 복사), (3) 임베딩 코사인 ≥ C(바꿔 쓴 글)
    로 겹치는지 본다. 같은 계정/다른 계정·그대로 복사/바꿔 쓰기를 따로 센다."""

    def __init__(self, msgs: list[dict], window_s: int = 1800, emb: dict | None = None):
        self.msgs = sorted(msgs, key=lambda m: m["ts"])
        self.window = window_s
        self.emb = emb or {}
        for m in self.msgs:
            m["_sh"] = shingles(m["text"])
            m["_tok"] = set().union(*[{f"{k}:{v}" for v in vs} for k, vs in _tokens(m["text"]).items()])
        self.by_tok: dict[str, list[dict]] = defaultdict(list)
        for m in self.msgs:
            for t in m["_tok"]:
                self.by_tok[t].append(m)

    def _near(self, ts: float) -> list[dict]:
        import bisect
        keys = [m["ts"] for m in self.msgs] if not hasattr(self, "_keys") else self._keys
        self._keys = keys
        lo = bisect.bisect_left(keys, ts - self.window)
        hi = bisect.bisect_right(keys, ts + self.window)
        return self.msgs[lo:hi]

    def check(self, unit: dict, j_min: float = 0.5, cos_same: float = 0.72, cos_other: float = 0.70,
              min_len: int = 12) -> dict:
        """copy = 거의 같은 글(J≥0.9 또는 코사인≥0.97, 이모지만 있는 글 포함), para = copy 는 아니지만 J≥j_min 또는
        코사인 ≥ cos_same/cos_other. 코사인 문턱은 실제 정상 글끼리의 최댓값(같은 계정 0.69·다른 계정 0.65) 바로 위."""
        reasons: set[str] = set()
        f = {"tok_same": 0, "tok_other": 0, "copy_same": 0, "copy_other": 0, "para_same": 0, "para_other": 0,
             "rooms": set()}
        uid, chat = unit["profile"]["user_id"], unit["chat_id"]
        for text, ts in zip(unit["msgs"], unit["ts"]):
            sh = shingles(text)
            tok = set().union(*[{f"{k}:{v}" for v in vs} for k, vs in _tokens(text).items()])
            e = self.emb.get(text)
            for m in self._near(ts):
                if m["chat_id"] == chat:
                    continue
                same = m["user_id"] == uid
                who = "same" if same else "other"
                if tok & m["_tok"]:
                    f["tok_" + who] += 1; f["rooms"].add(m["chat_id"])
                    reasons.add(f"다른 방 같은 토큰({'같은' if same else '다른'} 계정)")
                if len(text) >= min_len and len(m["text"]) >= min_len:
                    jac = jaccard(sh, m["_sh"])
                    cos = (sum(a * b for a, b in zip(e, self.emb[m["text"]]))
                           if e is not None and m["text"] in self.emb else 0.0)
                    if jac >= 0.9 or cos >= 0.97:
                        f["copy_" + who] += 1; f["rooms"].add(m["chat_id"])
                        reasons.add(f"다른 방 복사글({'같은' if same else '다른'} 계정)")
                    elif jac >= j_min or cos >= (cos_same if same else cos_other):
                        f["para_" + who] += 1; f["rooms"].add(m["chat_id"])
                        reasons.add(f"다른 방 바꿔 쓴 글({'같은' if same else '다른'} 계정)")
        f["n_rooms"] = len(f.pop("rooms"))
        # 점수: 바꿔 쓰기(사람은 보통 그대로 복사·LLM 은 매번 바꿈) > 토큰 > 복사
        score = (2 * (f["para_same"] > 0) + 2 * (f["para_other"] > 0) + 1.5 * (f["tok_other"] > 0)
                 + 1 * (f["tok_same"] > 0) + 0.5 * (f["copy_same"] > 0) + 1 * (f["copy_other"] > 0))
        return {"score": score, "reasons": sorted(reasons), **f}
