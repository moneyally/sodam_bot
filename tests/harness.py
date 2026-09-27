"""검증 하네스: 버튼 메뉴를 사람처럼 전부 눌러 보고 실제 화면을 뽑는다 (네트워크 없음).

- 4명의 역할(오너 / 텔레그램 관리자 / .봇관리자 / 일반 멤버)로 /start·그룹 허브에서 시작해 보이는 m: 버튼을 BFS 로 전부 누른다.
- 검사: 예외 없음 · q.answer 정확히 1번 · callback_data 64바이트 이하 · 모르는 라우트 없음 · URL 버튼 https/tg 만 ·
  텔레그램 HTML 규칙(허용 태그·짝 맞춤·4096자) · 버튼 글자 비어있지 않음 ·
  막다른 버튼(⬅️ 가 화면 대신 팝업만 / 그 사람에게 보여 준 이 방·방 없는 버튼이 '권한 없음'으로 거절).
- 1:1 알림처럼 메뉴 밖으로 보내는 버튼은 시드가 add_buttons(실제 키보드) → 역할마다 시작점으로 누른다.
- 권한 퍼징: 관리자가 본 방 단위 버튼을 멤버가 그대로 눌러도 전부 거절되는지, TG_ADMIN 전용을 봇관리자가 눌러도 거절되는지.
- render_markdown() 은 사람이 읽는 화면 모음(docs/SCREENS.md) 을 만든다.

사용: tests/test_harness.py 가 자동으로 돌리고, `python tools/render_screens.py` 로 화면 파일 생성.
"""
from __future__ import annotations

import html
import re
import traceback
from collections import deque
from dataclasses import dataclass, field
from html.parser import HTMLParser
from types import SimpleNamespace

from fakes import FakeBot, cfg, fake_user, make_db, make_svc

from sodam import menu

CHAT = -1001234567890
OWNER, TG, BOTADM, MEMBER = 7, 1, 30, 20
PERSONAS = {OWNER: "오너", TG: "텔레그램 관리자", BOTADM: "봇관리자(.봇관리자)", MEMBER: "일반 멤버"}
MAX_PRESSES = 3000   # 역할 전체 합계. 넘으면 문제로 보고 (뒤 역할이 덜 눌리는 걸 모르고 지나가지 않게)
TG_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "tg-spoiler",
           "span", "blockquote", "tg-emoji"}
KNOWN_PREFIXES = ("m:", "pay:", "an:", "cap:", "qz:", "act:")


class HarnessPerms:
    """실제 Permissions 와 같은 판단 규칙 (오너 ⊃ 텔레그램 관리자 ⊃ 봇관리자)."""

    def __init__(self):
        self.tg, self.bot_admins, self.forgets = {TG}, {BOTADM}, 0

    async def owners(self):
        return {OWNER}

    async def candidate_chats(self, uid):
        return [CHAT]

    async def is_admin(self, bot, cid, uid):
        return uid == OWNER or (cid < 0 and uid in self.tg | self.bot_admins)

    async def is_tg_admin(self, bot, cid, uid):
        return uid == OWNER or (cid < 0 and uid in self.tg)

    async def protected(self, bot, cid, uid):
        return uid == bot.id or await self.is_admin(bot, cid, uid)

    async def role(self, bot, cid, uid):
        from sodam.permissions import Role
        if uid == OWNER:
            return Role.OWNER
        return Role.ADMIN if await self.is_admin(bot, cid, uid) else Role.MEMBER

    async def can(self, bot, cid, uid, right="restrict"):
        """세부 권한: 하네스의 TG 관리자는 차단·삭제 권한이 있고, 봇관리자는 그 관리자가 지정한 것으로 본다."""
        return await self.is_admin(bot, cid, uid)

    async def can_restrict(self, bot, cid, uid):
        return await self.can(bot, cid, uid, "restrict")

    def forget(self, cid):
        self.forgets += 1

    def forget_bot(self, cid):
        pass

    async def bot_can_moderate(self, bot, cid):
        return True

    async def admin_users(self, bot, cid):
        from fakes import fake_user
        return [fake_user(u, PERSONAS.get(u, "관리자")) for u in self.tg | {OWNER}]


class _Checker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in TG_TAGS:
            self.errors.append(f"텔레그램이 모르는 태그 <{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"태그 짝이 안 맞음 </{tag}> (열린 것: {self.stack})")
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
            return
        self.stack.pop()


def html_errors(text: str) -> list[str]:
    if text is None:
        return []
    c = _Checker()
    c.feed(text)
    c.close()
    errs = c.errors + ([f"닫히지 않은 태그 {c.stack}"] if c.stack else [])
    plain = re.sub(r"<[^>]+>", "", text)
    if len(html.unescape(plain)) > 4096:
        errs.append(f"메시지 4096자 초과 ({len(plain)})")
    if re.search(r"&(?![a-zA-Z]+;|#\d+;|#x[0-9a-fA-F]+;)", text):
        errs.append("이스케이프 안 된 & (esc() 누락)")
    if re.search(r"&amp;(amp|lt|gt|quot|#\d+);", text):
        errs.append("두 번 이스케이프됨 (esc 를 두 번 적용 → 사용자에게 &lt; 가 그대로 보임)")
    return errs


class HQuery:
    def __init__(self, uid, data):
        self.message = SimpleNamespace(chat_id=uid)
        self.from_user = fake_user(uid, PERSONAS.get(uid, "사용자"))
        self.data = data
        self.answers: list[tuple] = []
        self.edits: list[tuple[str, object]] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kw):
        self.edits.append((text, kw.get("reply_markup")))

    async def edit_message_reply_markup(self, markup=None):
        self.edits.append(("<markup removed>", markup))


@dataclass
class Shot:
    persona: int
    data: str
    text: str
    kb: object
    toast: str | None


@dataclass
class Report:
    shots: list[Shot] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    presses: int = 0
    scoped_seen: dict[str, str] = field(default_factory=dict)  # callback → 처음 본 역할의 이름


async def make_world():
    """하네스용 방·데이터 (결제 켜짐, 금지어·도메인·자료 등 목록이 비어있지 않게)."""
    db = await make_db()
    svc = await make_svc(db)
    svc.cfg = cfg(db.path, pay_address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", owner_ids=frozenset({OWNER}))
    from sodam.billing import Billing
    svc.billing = Billing(svc.cfg, db)
    svc.perms = HarnessPerms()
    svc.mod.perms = svc.perms
    await db.ensure_chat(CHAT, "대표님 소통방 <테스트> & 친구들")  # 제목 이스케이프까지 검사
    await svc.billing.ensure_trial(CHAT, TG)
    for w in ("스팸", "도박사이트", "a&b<c>"):
        await db.set_banned_word(CHAT, w, True)
    await db.set_setting(CHAT, "whitelist_domains", ["youtube.com", "naver.com"])
    for uid in PERSONAS:
        await db.upsert_user(fake_user(uid, PERSONAS[uid]))
        await db.touch_member(CHAT, uid)
    EXTRA_STARTS.clear()
    for fn in SEEDERS:  # 패널 모듈이 자기 데이터(예약공지·자료 등)를 넣을 수 있게
        await fn(svc)
    return db, svc, FakeBot()


SEEDERS: list = []
# 메뉴 밖(1:1 알림·하루 요약 등)으로 보내는 버튼: 시드가 add_buttons(실제 알림의 키보드)로 넣으면 시작점으로 누른다
EXTRA_STARTS: list[str] = []


def add_buttons(kb) -> None:
    EXTRA_STARTS.extend(b.callback_data for b in _buttons(kb) if (b.callback_data or "").startswith("m:"))


def _buttons(kb):
    return [b for row in getattr(kb, "inline_keyboard", ()) for b in row] if kb else []


def _check_screen(rep: Report, who: str, data: str, text, kb) -> None:
    for e in html_errors(text):
        rep.issues.append(f"[{who}] {data}: {e}")
    for b in _buttons(kb):
        if not (b.text or "").strip():
            rep.issues.append(f"[{who}] {data}: 빈 버튼 글자")
        if re.search(r"&(amp|lt|gt|quot|#\d+);", b.text or ""):
            rep.issues.append(f"[{who}] {data}: 버튼 글자는 HTML 이 아닌데 이스케이프됨 {b.text!r}")
        if b.callback_data is not None:
            cd = b.callback_data
            if len(cd.encode()) > 64:
                rep.issues.append(f"[{who}] {data}: callback 64바이트 초과 {cd!r}")
            if not cd.startswith(KNOWN_PREFIXES):
                rep.issues.append(f"[{who}] {data}: 모르는 콜백 접두어 {cd!r}")
            if cd.startswith("m:") and cd.split(":")[1] not in menu.ROUTES:
                rep.issues.append(f"[{who}] {data}: 없는 라우트 {cd!r}")
        elif b.url is not None and not b.url.startswith(("https://", "tg://")):
            rep.issues.append(f"[{who}] {data}: URL 버튼이 https/tg 아님 {b.url!r}")


async def press(svc, bot, uid, data) -> HQuery:
    q = HQuery(uid, data)
    await menu.on_callback(svc, bot, q, data.split(":")[1:])
    return q


async def crawl_as(svc, bot, uid, rep: Report, starts: list[str]) -> None:
    who = PERSONAS[uid]
    seen: set[str] = set()
    shown: dict[str, str] = {}   # 화면에 보인 버튼 → 글자
    dead: dict[str, str] = {}    # 눌렀더니 화면 없이 경고 팝업만 → 팝업 글
    queue = deque(starts)
    while queue and rep.presses < MAX_PRESSES:
        data = queue.popleft()
        if data in seen:
            continue
        seen.add(data)
        svc.menu_limiter._hits.clear()  # 크롤러는 레이트리밋 대상이 아님
        before_sent = len(bot.calls)
        try:
            q = await press(svc, bot, uid, data)
        except Exception:
            rep.issues.append(f"[{who}] {data}: 예외\n{traceback.format_exc(limit=6)}")
            continue
        rep.presses += 1
        if len(q.answers) != 1:
            rep.issues.append(f"[{who}] {data}: q.answer {len(q.answers)}번 (정확히 1번이어야 함)")
        toast = q.answers[0][0] if q.answers else None
        screens = [(t, kb) for t, kb in q.edits]
        screens += [(c[2], c[3].get("reply_markup")) for c in bot.calls[before_sent:]
                    if c[0] == "send_message" and c[1] == uid]
        # 사진·영상·GIF·파일로 보낸 화면(인사 미리보기 등): 캡션 + 버튼. 캡션은 1024자 제한
        for c in bot.calls[before_sent:]:
            if c[0] in ("send_photo", "send_video", "send_animation", "send_document") and c[1] == uid:
                cap = c[3] or ""
                if len(re.sub(r"<[^>]+>", "", cap)) > 1024:
                    rep.issues.append(f"[{who}] {data}: 캡션 1024자 초과")
                screens.append((f"[{c[0][5:]}] " + cap, c[4].get("reply_markup")))
        if not screens and q.answers and q.answers[0][1]:
            dead[data] = toast or ""
        for text, kb in screens:
            _check_screen(rep, who, data, text, kb)
            rep.shots.append(Shot(uid, data, text, kb, toast))
            for b in _buttons(kb):
                cd = b.callback_data
                if cd and cd.startswith("m:"):
                    shown.setdefault(cd, b.text or "")
                if cd and cd.startswith("m:") and cd not in seen:
                    if len(cd.split(":")) > 2 and menu.CID_RE.fullmatch(cd.split(":")[2] or "x"):
                        rep.scoped_seen.setdefault(cd, who)
                    queue.append(cd)
    # 막다른 버튼: 보여 준 버튼을 그 사람이 눌렀는데 화면 없이 팝업만 (⬅️ 되돌아가기 전부 · 이 방/방 없는 버튼의 '권한 없음')
    for cd, why in dead.items():
        label = shown.get(cd)
        if label is None:           # 시작점(1:1 알림 버튼)으로만 누른 것
            continue
        parts = cd.split(":")
        here = len(parts) < 3 or not menu.CID_RE.fullmatch(parts[2]) or int(parts[2]) == CHAT
        if label.startswith("⬅️"):
            rep.issues.append(f"[{who}] {cd}: ⬅️ 버튼이 화면으로 안 돌아감 ({why})")
        elif here and DENIED.search(why):
            rep.issues.append(f"[{who}] {cd}: 보여 준 버튼 [{label}] 인데 누르면 거절 ({why})")


DENIED = re.compile(r"관리자만|오너만|권한")


def load_seeders() -> None:
    """tests/test_*.py 가 모듈 import 때 SEEDERS.append 하므로, render_screens 처럼 하네스만 쓸 때도 전부 불러온다."""
    import importlib
    from pathlib import Path
    for p in sorted(Path(__file__).resolve().parent.glob("test_*.py")):
        importlib.import_module(p.stem)


async def crawl() -> tuple[Report, object, object]:
    load_seeders()
    rep = Report()
    for uid in (OWNER, TG, BOTADM, MEMBER):  # 역할마다 새 세상 (앞 역할이 지운 목록을 다음 역할이 못 보는 일 없게)
        db, svc, bot = await make_world()
        await crawl_as(svc, bot, uid, rep, ["m:home", "m:groups", f"m:g:{CHAT}", *EXTRA_STARTS])
    if rep.presses >= MAX_PRESSES:
        rep.issues.append(f"[하네스] 버튼을 {MAX_PRESSES}번 눌러서 중간에 멈춤 — MAX_PRESSES 를 늘리거나 시드 데이터를 줄일 것")
    db, svc, bot = await make_world()
    # 권한 퍼징: 관리자 화면에서 본 방 단위 버튼을 멤버가 누르면 전부 거절돼야 함
    for data in list(rep.scoped_seen):
        if data.startswith("m:k:"):
            continue
        svc.menu_limiter._hits.clear()
        q = await press(svc, bot, MEMBER, data)
        if q.edits or not q.answers or not q.answers[0][1]:
            rep.issues.append(f"[권한] 일반 멤버가 {data} 를 눌렀는데 거절되지 않음: {q.answers} {len(q.edits)}")
    # 오너 전용(방 없는) 버튼을 다른 역할이 누르면 거절
    owner_only = {d for s in rep.shots if s.persona == OWNER for b in _buttons(s.kb)
                  if (d := b.callback_data or "").startswith("m:") and not d.startswith("m:k:")
                  and (r := menu.ROUTES.get(d.split(":")[1])) and r.need == menu.OWNER}
    for data in owner_only:
        for uid in (TG, BOTADM, MEMBER):
            svc.menu_limiter._hits.clear()
            q = await press(svc, bot, uid, data)
            if q.edits:
                rep.issues.append(f"[권한] {PERSONAS[uid]} 가 오너 전용 {data} 화면을 봄")
    for data in [d for d in rep.scoped_seen if menu.ROUTES.get(d.split(":")[1], menu.Route(None)).need >= menu.TG_ADMIN]:
        svc.menu_limiter._hits.clear()
        q = await press(svc, bot, BOTADM, data)
        if q.edits:
            rep.issues.append(f"[권한] 봇관리자가 TG_ADMIN 전용 {data} 화면을 봄")
    return rep, db, svc


# ── 사람이 읽는 화면 모음 ─────────────────────────────────
def _plain(text: str) -> str:
    t = re.sub(r"<a href=\"[^\"]*\">", "", text or "")
    t = re.sub(r"</?(b|strong)>", "**", t)
    t = re.sub(r"</?code>", "`", t)
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(t)


def render_markdown(rep: Report) -> str:
    out = ["# 소담 버튼 메뉴 화면 모음 (자동 생성)", "",
           "`python tools/render_screens.py` 로 다시 만든다. 하네스가 실제로 버튼을 눌러서 나온 화면 그대로다.", "",
           f"- 누른 버튼 수: {rep.presses}", f"- 발견된 문제: {len(rep.issues)}", ""]
    if rep.issues:
        out += ["## ⚠️ 문제", ""] + [f"- {i}" for i in rep.issues] + [""]
    done: set[tuple] = set()
    for uid in PERSONAS:
        out += [f"## {PERSONAS[uid]} 로 본 화면", ""]
        for s in [s for s in rep.shots if s.persona == uid]:
            code = re.sub(r"^m:k:.*", "m:k:<토큰>", s.data)
            sig = (uid, (s.text or "").split("\n")[0], tuple(b.text for b in _buttons(s.kb)))
            if sig in done:
                continue
            done.add(sig)
            out.append(f"### `{code}`" + (f" — 토스트: {s.toast}" if s.toast else ""))
            out.append("```")
            out.append(_plain(s.text))
            if s.kb:
                out.append("─" * 30)
                for row in s.kb.inline_keyboard:
                    out.append("  ".join(f"[{b.text}]" for b in row))
            out.append("```")
            out.append("")
    return "\n".join(out)


# 패널별 시드 파일 tests/seed_*.py 를 자동으로 불러온다 (각 파일이 harness.SEEDERS.append).
# sodam/ 쪽 패널 모듈은 tests 를 import 할 수 없어서(순환·배포) 데이터는 tests/ 에 둔다.
def _load_seed_files() -> None:
    import importlib
    from pathlib import Path
    for p in sorted(Path(__file__).resolve().parent.glob("seed_*.py")):
        importlib.import_module(p.stem)


_load_seed_files()
