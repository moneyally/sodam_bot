"""📘 소담 공식 안내서 (sodam/guide/*.md) + AI 도구 sodam_guide — 소담 자신에 대한 질문(가격·결제·데려오기·기능·모드)은 여기서 읽고 답한다.

왜: 벳블리 '소담아 결제하면 얼마야?' → '가격 자료가 없어' (2026-09-29). 오너 결정: 1달 30 USDT, 결제는 [💳 구독하기] 버튼.
- 문서 = 저장소 파일(git 에 버전), 주제마다 front-matter (title · tags · related) → related 로 주제끼리 이어짐 (작은 지식 그래프).
- 가격·기간은 문서에 숫자를 박지 않고 {price}·{days}·{trial}·{voice_min} → 서버 설정값(.env SUB_PRICE_USDT 등)으로 채움.
- 요금·데려오기 주제면 그룹방에 [➕ 우리 방에 소담 추가][💳 구독하기] 버튼 카드 (방마다 10분에 1번). 입금 주소는 절대 방에 안 나감 (결제 화면 = 관리자 1:1).
"""
from __future__ import annotations

import os
import re
import time
from functools import lru_cache
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from .. import tools
from ..tools import Tool, ToolCtx

GUIDE_DIR = Path(__file__).resolve().parents[1] / "guide"
CARD_TOPICS = ("pricing", "invite")
CARD_GAP = 600
_card_sent: dict[int, float] = {}


@lru_cache(maxsize=1)
def load() -> dict[str, dict]:
    """{key: {title, tags, related, body}} — key = 파일 이름."""
    out = {}
    for p in sorted(GUIDE_DIR.glob("*.md")):
        text = p.read_text(encoding="utf-8")
        m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
        meta, body = (m.group(1), m.group(2)) if m else ("", text)
        fields = dict(line.split(":", 1) for line in meta.splitlines() if ":" in line)
        out[p.stem] = {"title": fields.get("title", p.stem).strip(),
                       "tags": [t.strip() for t in fields.get("tags", "").split(",") if t.strip()],
                       "related": [t.strip() for t in fields.get("related", "").split(",") if t.strip()],
                       "body": body.strip()}
    return out


def render(body: str, cfg) -> str:
    values = {"price": getattr(cfg, "sub_price_usdt", "30"), "days": getattr(cfg, "sub_days", 30),
              "trial": getattr(cfg, "trial_days", 3), "voice_min": os.getenv("VOICE_ROOM_MONTH_MIN", "120")}
    return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1), m.group(0))), body)


def find(query: str) -> str | None:
    """주제 키 그대로, 아니면 제목·태그가 가장 많이 겹치는 문서."""
    docs = load()
    q = query.strip().lower()
    if q in docs:
        return q
    squash = re.sub(r"\s+", "", q)
    best, score = None, 0
    for key, d in docs.items():
        s = sum(2 for t in d["tags"] if t.lower() in squash) + sum(1 for w in re.findall(r"[가-힣a-z0-9]{2,}", q)
                                                                  if w in d["title"].lower())
        if s > score:
            best, score = key, s
    return best


async def _card(ctx: ToolCtx) -> str:
    if ctx.chat_id > 0 or not getattr(ctx.bot, "username", None):
        return ""
    now = time.monotonic()
    if now - _card_sent.get(ctx.chat_id, -1e9) < CARD_GAP:
        return ""
    from ..menu import add_to_group_url
    from ..subscription import setup_link
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("➕ 우리 방에 소담 추가", url=add_to_group_url(ctx.bot.username)),
                                InlineKeyboardButton("💳 구독하기", url=setup_link(ctx.bot.username, ctx.chat_id))]])
    try:
        await ctx.bot.send_message(ctx.chat_id, "📘 소담 데려오기·구독은 아래 버튼으로 (결제 화면은 관리자 1:1 에서 열려요)",
                                   reply_markup=kb)
    except TelegramError:
        return ""
    _card_sent[ctx.chat_id] = now
    if len(_card_sent) > 5000:
        _card_sent.clear()
    return "\n(방에 [➕ 우리 방에 소담 추가][💳 구독하기] 버튼 카드를 보냈음 — 링크를 다시 쓰지 말고 '아래 버튼' 이라고만.)"


async def t_sodam_guide(ctx: ToolCtx, a: dict) -> str:
    docs = load()
    q = str(a.get("topic") or "").strip()
    key = find(q) if q else None
    if not key:
        return "공식 안내서 주제: " + " · ".join(f"{k}({d['title']})" for k, d in docs.items()) + " — topic 으로 다시."
    d = docs[key]
    related = " · ".join(f"{k}({docs[k]['title']})" for k in d["related"] if k in docs)
    out = f"[소담 공식 안내서 — {d['title']}]\n{render(d['body'], ctx.svc.cfg)}" + (f"\n관련 주제: {related}" if related else "")
    if key in CARD_TOPICS:
        out += await _card(ctx)
    return out


tools.register_tool(Tool(
    "sodam_guide",
    "소담 자신에 대한 질문(가격·얼마·결제·구독·체험·우리 방에 데려오기·기능·말투/욕 받아치기/19금 모드·음성·사람 찾기·제작자)엔 "
    "지어내지 말고 이 공식 안내서를 먼저 읽고 답한다. topic = 주제 키(pricing·invite·features·modes·voice·lookup·faq) 또는 질문 그대로. "
    "결과의 '관련 주제'로 더 읽을 수 있음.",
    {"topic": {"type": "string", "description": "주제 키 또는 질문"}}, [], t_sodam_guide), read_only=True)
