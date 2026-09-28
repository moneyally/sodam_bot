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
import re
import time
from urllib.parse import urlsplit

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.error import BadRequest

from .db import register_schema

TEXT_LIMIT, CAPTION_LIMIT = 4096, 1024
MAX_ROWS, MAX_COLS, LABEL_MAX, URL_MAX = 8, 3, 40, 512
MEDIA = {"photo": "사진", "video": "영상"}
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
    updated    INTEGER NOT NULL
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
    return [[b for b in r if isinstance(b, dict) and b.get("t") and b.get("u")] for r in rows if r][:MAX_ROWS]


def markup(rows: list[list[dict]]) -> InlineKeyboardMarkup | None:
    rows = [[InlineKeyboardButton(b["t"], url=b["u"]) for b in r[:MAX_COLS]] for r in rows if r]
    return InlineKeyboardMarkup(rows) if rows else None


def limit_of(d) -> int:
    return CAPTION_LIMIT if d["media_type"] else TEXT_LIMIT


# ── 초안 (DB) ────────────────────────────────────────────
async def get(db, did: int):
    return await db._one("SELECT * FROM composer_drafts WHERE id=?", (did,))


async def create(db, uid: int, target: int, **fields) -> int:
    now = int(time.time())
    cols = ["user_id", "target", "updated", *fields]

    def run(c):
        c.execute("DELETE FROM composer_drafts WHERE state IN ('draft','posted') AND updated<?", (now - DRAFT_KEEP,))
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
    kw = dict(parse_mode="HTML", reply_markup=markup(buttons(d)),
              disable_notification=bool(d["silent"] if silent is None else silent))
    body = d["body"] or None
    if d["media_type"] == "photo":
        return await bot.send_photo(chat_id, d["media_id"], caption=body, **kw)
    if d["media_type"] == "video":
        return await bot.send_video(chat_id, d["media_id"], caption=body, **kw)
    return await bot.send_message(chat_id, body, link_preview_options=LinkPreviewOptions(is_disabled=not d["preview"]), **kw)


async def edit(bot, chat_id: int, msg_id: int, d) -> None:
    kb = markup(buttons(d))
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
