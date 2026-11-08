"""✍️ 글 편집기 (채널 글 공용): 텔레그램 HTML 본문 · 사진/영상 · URL 버튼 · 링크 미리보기 · 조용히 보내기.

- 본문은 두 길로 받는다: 대표님이 텔레그램에서 서식(굵게·링크 등)을 넣어 보낸 메시지 → 엔티티를 HTML 로 (msg_html),
  서식 없는 글 → 직접 쓴 HTML 로 본다. 어느 쪽이든 clean_html 허용 목록 검사를 통과한 HTML 만 저장·전송한다
  (모르는 태그는 글자만 남기고 빼고, 짝이 안 맞거나 위험한 링크면 위치와 함께 오류 → 깨진 HTML 은 절대 안 보냄).
- 초안은 DB(composer_drafts)에 → 봇이 재시작돼도 이어서. 올리기·예약은 state 를 한 문장 UPDATE 로 차지해서 연타해도 한 번.
화면은 panels/composer.py, 채널 등록·예약 실행은 channel.py.
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
from urllib.parse import urlsplit

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.error import BadRequest

from . import btnstyle, mediastore, persist
from .db import register_columns, register_schema

log = logging.getLogger(__name__)

TEXT_LIMIT, CAPTION_LIMIT = 4096, 1024
MAX_ROWS, MAX_COLS, LABEL_MAX, URL_MAX = 8, 3, 40, 512
MEDIA = {"photo": "사진", "video": "영상", "animation": "움짤(GIF)"}
DRAFT_KEEP = 7 * 86400      # 손 놓은 초안은 7일 뒤 정리
MAX_DRAFTS = 10             # 한 사람이 들고 있는 초안 (예약된 글 제외)

register_schema("""
CREATE TABLE IF NOT EXISTS composer_drafts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    target     INTEGER NOT NULL,              -- 올릴 채널
    body       TEXT NOT NULL DEFAULT '',      -- clean_html 을 통과한 HTML
    media_type TEXT,
    media_id   TEXT,
    buttons    TEXT NOT NULL DEFAULT '[]',    -- [[{"t": 글자, "u": 주소}, …], …]
    preview    INTEGER NOT NULL DEFAULT 1,    -- 링크 미리보기
    silent     INTEGER NOT NULL DEFAULT 0,    -- 알림 없이 보내기
    edit_of    INTEGER,                       -- 이미 올린 글(메시지 ID)을 고치는 중
    pending    TEXT,                          -- 만드는 중인 버튼 {"t", "u"}
    source     TEXT NOT NULL DEFAULT 'manual',  -- manual / ai
    state      TEXT NOT NULL DEFAULT 'draft',   -- draft / posting / posted / scheduled
    rev        INTEGER NOT NULL DEFAULT 0,
    updated    INTEGER NOT NULL,
    pin        INTEGER NOT NULL DEFAULT 0,    -- 올리면서 고정
    msg_id     INTEGER                        -- 그룹방에 올린 글 번호 (🎛 버튼 메뉴: 나중에 고치기)
);
""")
register_columns("composer_drafts", {"pin": "INTEGER NOT NULL DEFAULT 0", "msg_id": "INTEGER",
                                      "theme": "TEXT NOT NULL DEFAULT 'signal'",
                                      "auto_del": "INTEGER NOT NULL DEFAULT 0"})  # 올린 뒤 N초 지나면 자동삭제(0=안 함)

# ⏰ 자동삭제 프리셋 — 텔레그램은 봇이 48시간 지난 글을 못 지워서 그 안으로만 (persist.TEMP_MAX_AGE)
AUTO_DEL = (0, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 24 * 3600)
AUTO_DEL_MAX = 47 * 3600


def auto_del_of(d) -> int:
    try:
        v = int(d["auto_del"] or 0)
    except (KeyError, IndexError, TypeError, ValueError):
        return 0
    return v if 0 <= v <= AUTO_DEL_MAX else 0


def auto_del_label(secs: int) -> str:
    if not secs:
        return "안 함"
    h = secs // 3600
    return f"{h}시간 뒤" if h else f"{secs // 60}분 뒤"

# 반응 버튼 집계: 올린 글의 [👍] 버튼을 누른 사람 (한 사람 한 표, 다시 누르면 취소)
register_schema("""
CREATE TABLE IF NOT EXISTS composer_votes (
    chat_id INTEGER NOT NULL,
    msg_id  INTEGER NOT NULL,
    rkey    TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    ts      INTEGER NOT NULL,
    PRIMARY KEY (chat_id, msg_id, rkey, user_id)
);
""")


class HtmlError(ValueError):
    pass


# ── HTML 허용 목록 ────────────────────────────────────────
ALLOWED = {"b", "i", "u", "s", "code", "pre", "a", "blockquote", "tg-spoiler"}
ALIAS = {"strong": "b", "em": "i", "ins": "u", "strike": "s", "del": "s"}
_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)((?:\s[^<>]*)?)>")
_ATTR = re.compile(r"""([a-zA-Z-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?""")
_ENTITY = re.compile(r"&(?:lt|gt|amp|quot|#\d{1,7}|#x[0-9a-fA-F]{1,6});")
_LANG = re.compile(r"^(?:language-)?([\w+#-]{1,30})$")
LINK_SCHEMES = ("http", "https", "tg")


def _where(src: str, i: int) -> str:
    line = src.count("\n", 0, i) + 1
    return f"{line}번째 줄 {i - (src.rfind(chr(10), 0, i) + 1) + 1}번째 글자"


def _text(seg: str) -> str:
    """태그 밖 글자: 짝 없는 < > 와 모르는 & 는 글자로 (텔레그램이 거절하지 않게)."""
    out, pos = [], 0
    for m in _ENTITY.finditer(seg):
        out.append(html.escape(seg[pos:m.start()], quote=False))
        out.append(m.group(0))
        pos = m.end()
    out.append(html.escape(seg[pos:], quote=False))
    return "".join(out)


def _attrs(raw: str) -> dict[str, str]:
    return {m.group(1).lower(): html.unescape(next((g for g in m.groups()[1:] if g is not None), ""))
            for m in _ATTR.finditer(raw)}


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def plain(body: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", body))


def clean_html(src: str, limit: int = TEXT_LIMIT) -> tuple[str, list[str]]:
    """텔레그램 HTML 부분집합만 남긴 새 HTML + 뺀 태그 이름들. 짝이 안 맞거나 위험하면 HtmlError(위치 포함)."""
    out, stack, dropped, pos = [], [], [], 0     # stack: (태그 원래 이름, 텔레그램 이름 | None(뺀 태그), 위치)
    for m in _TAG.finditer(src):
        out.append(_text(src[pos:m.start()]))
        pos = m.end()
        closing, name, raw = m.group(1) == "/", m.group(2).lower(), m.group(3) or ""
        if name == "br":
            out.append("\n")
            continue
        if closing:
            if not any(n == name for n, _, _ in stack):
                if name in ALLOWED or name in ALIAS:
                    raise HtmlError(f"{_where(src, m.start())}: 열지 않은 &lt;/{name}&gt; 가 있어요")
                dropped.append(name)
                continue
            while stack[-1][1] is None and stack[-1][0] != name:   # 안에서 안 닫힌 모르는 태그는 그냥 버림
                stack.pop()
            top, canon, at = stack.pop()
            if top != name:
                raise HtmlError(f"{_where(src, m.start())}: &lt;/{name}&gt; 전에 {_where(src, at)}의 "
                                f"&lt;{top}&gt; 를 먼저 닫아야 해요")
            if canon:
                out.append(f"</{canon}>")
            continue
        attrs = _attrs(raw)
        canon = ALIAS.get(name, name)
        if name == "span":
            canon = "tg-spoiler" if "tg-spoiler" in attrs.get("class", "") else None
        elif canon not in ALLOWED:
            canon = None
        if canon is None:               # 모르는 태그: 글자만 남김
            dropped.append(name)
            if not raw.rstrip().endswith("/"):
                stack.append((name, None, m.start()))
            continue
        inside = [c for _, c, _ in stack if c]
        if inside and inside[-1] in ("code", "pre") and not (canon == "code" and inside[-1] == "pre"):
            raise HtmlError(f"{_where(src, m.start())}: 코드 칸 안에는 다른 서식을 넣을 수 없어요")
        if canon in inside and canon in ("a", "blockquote", "pre", "code"):
            raise HtmlError(f"{_where(src, m.start())}: &lt;{name}&gt; 안에 &lt;{name}&gt; 를 또 넣을 수 없어요")
        tag = canon
        if canon == "a":
            href = attrs.get("href", "").strip()
            if urlsplit(href).scheme.lower() not in LINK_SCHEMES or re.search(r"\s", href):
                raise HtmlError(f"{_where(src, m.start())}: 링크 주소는 https:// · http:// · tg:// 로 시작해야 해요")
            tag = f'a href="{html.escape(href)}"'
        elif canon == "code" and inside and inside[-1] == "pre" and (lang := _LANG.match(attrs.get("class", ""))):
            tag = f'code class="language-{lang.group(1)}"'
        elif canon == "blockquote" and "expandable" in attrs:
            tag = "blockquote expandable"
        stack.append((name, canon, m.start()))
        out.append(f"<{tag}>")
    out.append(_text(src[pos:]))
    open_ = [(n, at) for n, c, at in stack if c]
    if open_:
        n, at = open_[-1]
        raise HtmlError(f"{_where(src, at)}: &lt;{n}&gt; 가 닫히지 않았어요 (&lt;/{n}&gt; 필요)")
    body = "".join(out).strip()
    size = utf16_len(plain(body))
    if size > limit:
        raise HtmlError(f"글이 너무 길어요 ({size}/{limit}자{' — 사진·영상 설명은 1024자까지' if limit == CAPTION_LIMIT else ''})")
    return body, sorted(set(dropped))


_FORMAT = {"bold", "italic", "underline", "strikethrough", "spoiler", "code", "pre", "text_link", "text_mention",
           "blockquote", "expandable_blockquote", "custom_emoji"}


def msg_html(msg) -> str:
    """관리자가 보낸 메시지 → HTML 원문. 서식(엔티티)이 있으면 텔레그램이 준 서식 그대로, 없으면 직접 쓴 HTML 로 본다."""
    if msg.text is not None:
        ents, raw, conv = msg.entities, msg.text, "text_html"
    else:
        ents, raw, conv = msg.caption_entities, msg.caption or "", "caption_html"
    if any(getattr(e, "type", None) in _FORMAT for e in ents or ()):
        return getattr(msg, conv)
    return raw


# ── URL 버튼 ─────────────────────────────────────────────
def check_url(url: str) -> str | None:
    """버튼 주소: https:// 또는 tg:// 만. 'naver.com'·'t.me/x' 처럼 쓰면 https:// 를 붙여 준다."""
    u = url.strip()
    if "://" not in u and re.match(r"^[\w-]+(\.[\w-]+)*\.[a-zA-Z]{2,}(/\S*)?$", u):
        u = "https://" + u
    if len(u) > URL_MAX or re.search(r"\s", u):
        return None
    p = urlsplit(u)
    if p.scheme == "https" and "." in p.netloc and not p.netloc.startswith(".") or p.scheme == "tg" and (p.netloc or p.path):
        return u
    return None


def buttons(d) -> list[list[dict]]:
    try:
        rows = json.loads(d["buttons"] or "[]")
    except (TypeError, ValueError):
        return []
    # URL 버튼(u) 또는 반응 버튼(r = 누르면 집계) 둘 다 허용
    return [[b for b in r if isinstance(b, dict) and b.get("t") and (b.get("u") or b.get("r"))]
            for r in rows if r][:MAX_ROWS]


# 버튼 색 틀(테마) — 방장이 하나만 고르면 코드가 각 버튼 색을 정함 (Bot API 9.4 style)
THEMES = {"signal": "🚦 신호등", "blue": "🔵 파랑", "mono": "⚪ 무채색", "accent": "🎯 강조"}


def theme_of(d) -> str:
    try:
        t = d["theme"]
    except (KeyError, IndexError, TypeError):
        t = None
    return t if t in THEMES else "signal"


def _btn_style(theme: str, label: str, first: bool) -> str | None:
    if theme == "blue":
        return btnstyle.PRIMARY
    if theme == "mono":
        return None
    if theme == "accent":
        return btnstyle.PRIMARY if first else None
    return btnstyle._by_words(label) or btnstyle.PRIMARY   # 신호등(기본): 글자 뜻, 없으면 파랑


# 봇 주인이 프리미엄이 아니라 아이콘(프리미엄 이모지)이 거절되면 이 프로세스 동안 아이콘을 뺀다 (greet 와 같은 방식)
_icons_off = False


def _emoji_id(e) -> str | None:
    return None if _icons_off else btnstyle.valid_emoji_id(e)


def markup(rows: list[list[dict]], theme: str = "signal") -> InlineKeyboardMarkup | None:
    """버튼 색은 버튼별 색(b['c']) 우선, 없으면 전체 테마. 버튼 앞 프리미엄 이모지(b['e'])는 api_kwargs 로."""
    out, seen = [], 0
    for r in rows or []:
        if not r:
            continue
        line = []
        for b in r[:MAX_COLS]:
            st = btnstyle.valid_style(b.get("c")) or _btn_style(theme, b["t"], seen == 0)
            if b.get("r"):          # 반응 버튼: 누르면 집계 (콜백 cr:<키>). 수는 누를 때 on_react 가 붙임
                line.append(InlineKeyboardButton(b["t"], callback_data=f"cr:{b['r']}", style=st))
            else:
                icon = _emoji_id(b.get("e"))
                line.append(InlineKeyboardButton(b["t"], url=b["u"], style=st,
                                                 api_kwargs={"icon_custom_emoji_id": icon} if icon else None))
            seen += 1
        if line:
            out.append(line)
    return InlineKeyboardMarkup(out) if out else None


async def _icon_retry(call):
    """프리미엄 이모지 아이콘이 거절되면(봇 주인 비프리미엄) 아이콘을 끄고 한 번 더 — markup 이 다시 그려짐."""
    global _icons_off
    try:
        return await call()
    except BadRequest as e:
        s = str(e).lower()
        if not _icons_off and any(w in s for w in ("emoji", "icon", "premium")):
            _icons_off = True
            return await call()
        raise


# ── 반응 버튼 집계 ([👍 12]) ──────────────────────────────
_COUNT_TAIL = re.compile(r"\s+\d+$")           # 버튼 글자 끝의 ' 숫자'(집계 수) — 다시 그릴 때 떼어냄
REACT_PRESETS = ("👍", "❤️", "🔥", "😂", "😮", "👏", "🎉", "💯", "🙏", "✅")


async def _vote_toggle(db, chat_id: int, msg_id: int, rkey: str, uid: int) -> bool:
    """이 사람의 반응을 켜고/끔 (다시 누르면 취소). 켜졌으면 True — 한 db.atomic 으로."""
    def run(c):
        if c.execute("DELETE FROM composer_votes WHERE chat_id=? AND msg_id=? AND rkey=? AND user_id=?",
                     (chat_id, msg_id, rkey, uid)).rowcount:
            return False
        c.execute("INSERT INTO composer_votes(chat_id, msg_id, rkey, user_id, ts) VALUES(?,?,?,?,?)",
                  (chat_id, msg_id, rkey, uid, int(time.time())))
        return True
    return await db.atomic(run)


async def vote_counts(db, chat_id: int, msg_id: int) -> dict[str, int]:
    rows = await db._all("SELECT rkey, COUNT(*) n FROM composer_votes WHERE chat_id=? AND msg_id=? GROUP BY rkey",
                         (chat_id, msg_id))
    return {r["rkey"]: r["n"] for r in rows}


def _with_count(text: str, n: int) -> str:
    """버튼 글자에 집계 수를 붙임 (기존 끝 숫자는 떼고). 0 이면 글자만."""
    base = _COUNT_TAIL.sub("", text or "").rstrip()
    return f"{base} {n}" if n else base


async def on_react(svc, bot, q, parts) -> None:
    """반응 버튼(cr:<키>) 누름 → 그 사람 표 토글 → 지금 키보드의 반응 버튼 수만 다시 그림.
    레이아웃은 q.message 의 현재 키보드를 그대로 쓰므로 따로 저장할 필요 없음 (URL 버튼·색·아이콘 유지)."""
    rkey = parts[0] if parts else ""
    msg = getattr(q, "message", None)
    kb = getattr(msg, "reply_markup", None)
    if not rkey or not msg or not kb:
        return await q.answer()
    chat_id, msg_id, uid = msg.chat_id, msg.message_id, q.from_user.id
    on = await _vote_toggle(svc.db, chat_id, msg_id, rkey, uid)
    counts = await vote_counts(svc.db, chat_id, msg_id)
    rows = []
    for row in kb.inline_keyboard:
        nr = []
        for b in row:
            cd = b.callback_data or ""
            if cd.startswith("cr:"):          # 반응 버튼만 수 갱신, 나머지(URL)는 그대로
                nr.append(InlineKeyboardButton(_with_count(b.text, counts.get(cd[3:], 0)),
                                               callback_data=cd, style=getattr(b, "style", None)))
            else:
                nr.append(b)
        rows.append(nr)
    try:
        await q.edit_message_reply_markup(InlineKeyboardMarkup(rows))
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.debug("reaction edit skip: %s", e)
    await q.answer("👍 반영했어요" if on else "취소했어요")


# ── 🧩 레이아웃 템플릿 ─────────────────────────────────────
# 빈 초안을 뼈대로 채움 (제목·목록·인용 블록 + 반응 줄). 〈…〉 는 그대로 고쳐 쓰는 자리.
# 링크 버튼은 죽은 링크가 생기니 넣지 않음 — 관리자가 ➕ 버튼으로 직접. 반응 버튼만 예시로.
TEMPLATES = {
    "notice": {
        "name": "📢 공지",
        "body": "<b>📢 공지</b>\n\n〈공지 내용을 여기에 적어주세요〉\n\n"
                "<blockquote>문의는 아래 버튼으로 받아요</blockquote>",
        "buttons": [],
    },
    "event": {
        "name": "🎉 이벤트",
        "body": "<b>🎉 이벤트 안내</b>\n\n〈이벤트 제목〉\n\n"
                "📅 기간: 〈언제부터 언제까지〉\n🎁 혜택: 〈무엇을 받는지〉\n\n많은 참여 바랍니다!",
        "buttons": [[{"t": "🎉", "r": "🎉"}, {"t": "👍", "r": "👍"}, {"t": "🔥", "r": "🔥"}]],
    },
    "price": {
        "name": "💰 가격표",
        "body": "<b>💰 이용 안내</b>\n\n• 〈상품/서비스 1〉 — 〈가격〉\n• 〈상품/서비스 2〉 — 〈가격〉\n"
                "• 〈상품/서비스 3〉 — 〈가격〉\n\n<blockquote>자세한 문의는 아래로</blockquote>",
        "buttons": [],
    },
    "promo": {
        "name": "🔥 홍보",
        "body": "<b>🔥 〈제목〉</b>\n\n〈핵심 한 줄〉\n\n✅ 〈특징 1〉\n✅ 〈특징 2〉\n✅ 〈특징 3〉\n\n지금 확인하세요 👇",
        "buttons": [[{"t": "❤️", "r": "❤️"}, {"t": "🔥", "r": "🔥"}]],
    },
    "schedule": {
        "name": "📅 일정",
        "body": "<b>📅 일정 안내</b>\n\n🗓 〈날짜〉\n🕐 〈시간〉\n📍 〈장소/링크〉\n\n〈추가 안내 사항〉",
        "buttons": [[{"t": "👍", "r": "👍"}]],
    },
}


def template_body(key: str) -> tuple[str, str] | None:
    """템플릿 key → (clean_html 통과한 본문, 버튼 JSON). 없으면 None."""
    t = TEMPLATES.get(key)
    if not t:
        return None
    try:
        body, _ = clean_html(t["body"])
    except HtmlError:
        body = plain(t["body"])
    return body, json.dumps(t["buttons"], ensure_ascii=False)


def limit_of(d) -> int:
    return CAPTION_LIMIT if d["media_type"] else TEXT_LIMIT


# ── 초안 (DB) ────────────────────────────────────────────
async def get(db, did: int):
    return await db._one("SELECT * FROM composer_drafts WHERE id=?", (did,))


async def create(db, uid: int, target: int, **fields) -> int:
    now = int(time.time())
    cols = ["user_id", "target", "updated", *fields]

    def run(c):
        # 그룹방에 올린 버튼 메뉴(msg_id 있음)는 나중에 고칠 수 있게 남김
        c.execute("DELETE FROM composer_drafts WHERE (state='draft' OR state='posted' AND msg_id IS NULL) AND updated<?",
                  (now - DRAFT_KEEP,))
        c.execute("DELETE FROM composer_drafts WHERE id IN (SELECT id FROM composer_drafts WHERE user_id=? AND "
                  "state='draft' ORDER BY updated DESC LIMIT -1 OFFSET ?)", (uid, MAX_DRAFTS - 1))
        return c.execute(f"INSERT INTO composer_drafts({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                         (uid, target, now, *fields.values())).lastrowid
    return await db.atomic(run)


async def update(db, did: int, rev: int | None = None, **fields) -> bool:
    """초안 칸 바꾸기 (draft 상태만). rev 를 주면 그 판일 때만 (버튼 연타·옛 화면이 두 번 적용되지 않게)."""
    sets = ", ".join(f"{k}=?" for k in fields)
    return bool(await db.atomic(lambda c: c.execute(
        f"UPDATE composer_drafts SET {sets}, rev=rev+1, updated=? WHERE id=? AND state='draft' AND (? IS NULL OR rev=?)",
        (*fields.values(), int(time.time()), did, rev, rev)).rowcount))


async def claim(db, did: int, frm: str, to: str) -> bool:
    """state frm → to 를 한 문장으로 차지 (두 번 눌러도 한 번만 통과)."""
    return bool(await db.atomic(lambda c: c.execute(
        "UPDATE composer_drafts SET state=?, updated=? WHERE id=? AND state=?", (to, int(time.time()), did, frm)).rowcount))


async def remove(db, did: int) -> None:
    await db._write("DELETE FROM composer_drafts WHERE id=?", (did,))


# ── 보내기 · 고치기 ───────────────────────────────────────
async def send(bot, chat_id: int, d, *, silent: bool | None = None):
    """초안을 chat_id 로 (채널 게시·1:1 미리보기 공용 → 미리보기가 실제와 같음)."""
    body = d["body"] or None
    disable = bool(d["silent"] if silent is None else silent)

    async def go():
        kw = dict(parse_mode="HTML", reply_markup=markup(buttons(d), theme_of(d)), disable_notification=disable)
        if d["media_type"] in MEDIA and d["media_id"]:
            # 봇이 바뀌어 file_id 가 안 먹으면 보관 원본으로 다시 올림 (sodam/mediastore.py)
            return await mediastore.send(bot, persist.db_of(bot), d["media_type"], chat_id, d["media_id"], caption=body, **kw)
        return await bot.send_message(chat_id, body, link_preview_options=LinkPreviewOptions(is_disabled=not d["preview"]), **kw)
    return await _icon_retry(go)


def arm_auto_delete(bot, db, chat_id: int, msg_id: int | None, d) -> int:
    """올린 글에 자동삭제를 예약 (초안에 설정됐을 때만). 예약한 초를 돌려줌(0=안 함).
    persist.delete_later 가 DB(temp_msgs)에도 적어 재시작돼도 sweep 이 지운다."""
    secs = auto_del_of(d)
    if secs and msg_id:
        persist.delete_later(bot, chat_id, msg_id, secs, db=db)
    return secs


async def edit(bot, chat_id: int, msg_id: int, d) -> None:
    async def go():
        kb = markup(buttons(d), theme_of(d))
        try:
            if d["media_type"]:
                await bot.edit_message_caption(chat_id=chat_id, message_id=msg_id, caption=d["body"] or None,
                                               parse_mode="HTML", reply_markup=kb)
            else:
                await bot.edit_message_text(d["body"], chat_id=chat_id, message_id=msg_id, parse_mode="HTML", reply_markup=kb,
                                            link_preview_options=LinkPreviewOptions(is_disabled=not d["preview"]))
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
    await _icon_retry(go)
