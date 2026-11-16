"""🏷 화이트라벨 클론봇 마법사 (오너 1:1 전용) — sodam/clone.py 엔진 위의 버튼 화면.

흐름: [➕ 새 클론] → 토큰 붙여넣기(getMe 검증·즉시 삭제) → 봇 이름 → 프사 사진/영상(선택)
     → 구매자 ID(선택) → 미리보기 → [✅ 만들기] → 봇 API 로 이름·프사 적용 + .env.clone 생성 + 등록.
켜기는 systemd 인스턴스 유닛(deploy/sodam@.service). 자동 켜기 데몬(sodam-clone-apply) 전엔 ssh 반자동.
"""
from __future__ import annotations

import re

from telegram import Message
from telegram.error import TelegramError

from .. import clone, clone_migrate, menu, namehist
from ..announce import extract_media
from ..menu import OWNER, B, PanelCtx, Route, Screen
from ..util import esc, to_int

NOT_OWNER = Screen(None, toast="봇 오너만 쓸 수 있어요.", alert=True)
DRAFT_KEY = "clone:draft"       # chat_state(0, "clone:draft:<uid>")


async def _is_owner(c: PanelCtx) -> bool:
    return c.uid in await c.svc.perms.owners()


def _dkey(uid: int) -> str:
    return f"{DRAFT_KEY}:{uid}"


async def _get_draft(c: PanelCtx) -> dict | None:
    return await c.svc.db.get_state(0, _dkey(c.uid), None)


async def _set_draft(c: PanelCtx, draft: dict) -> None:
    await c.svc.db.set_state(0, _dkey(c.uid), draft)


async def _clear_draft(c: PanelCtx) -> None:
    await c.svc.db.set_state(0, _dkey(c.uid), None)


# ── 메인 화면 (클론 목록) ────────────────────────────────────────
async def s_main(c: PanelCtx) -> Screen:
    if not await _is_owner(c):
        return NOT_OWNER
    rows = await clone.list_clones(c.svc.db)
    lines = ["🏷 <b>화이트라벨 클론봇</b>",
             "구매자 봇(@아이디·이름·사진은 구매자가 BotFather 로)에 같은 소담 두뇌를 얹어 팔아요.", ""]
    if rows:
        lines.append(f"지금 클론 <b>{len(rows)}</b>개:")
        mark = {"active": "🟢", "pending": "🟡", "stopped": "⚪"}
        for r in rows:
            lines.append(f"{mark.get(r['status'], '⚪')} <b>{esc(r['name'])}</b> "
                         f"(@{esc(r['username'])}) · {esc(r['status'])}")
    else:
        lines.append("아직 클론이 없어요. 아래로 첫 봇을 연결해 봐.")
    kb = [[B("➕ 새 클론 만들기", "m:clnew")]]
    for r in rows:
        kb.append([B(f"⚙️ {r['name']} (@{r['username']})", f"m:cls:{r['slug']}")])
    kb.append([B("🔄 새로고침", "m:cl")])
    return Screen("\n".join(lines), menu._kb(kb))


# ── 새 클론: 토큰부터 ───────────────────────────────────────────
async def r_new(c: PanelCtx) -> Screen:
    if not await _is_owner(c):
        return NOT_OWNER
    await _set_draft(c, {})
    c.svc.inputs[c.uid] = menu.PendingInput("cltok", 0)
    return Screen(
        "① <b>봇 토큰</b>을 붙여넣어 줘.\n\n"
        "구매자가 @BotFather 에서 자기 봇을 만들고 (이름·@아이디·프사 직접 설정) "
        "받은 <code>123456789:AA...</code> 토큰이야.\n\n"
        "여기 붙이면 바로 지울게 (안 새어). 5분 안에. 그만두려면 <code>취소</code>.",
        menu._kb([[B("❌ 취소", "m:cl")]]))


async def i_token(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    raw = msg.text or ""
    try:
        await msg.delete()                 # 토큰은 민감 → 읽자마자 삭제
    except TelegramError:
        pass
    try:
        tok = clone.parse_token(raw)
        info = await clone.fetch_bot_info(tok)
    except clone.CloneError as e:
        return False, str(e)
    if await clone.by_bot_id(c.svc.db, info.bot_id):
        return False, f"@{esc(info.username)} 는 이미 클론으로 등록돼 있어."
    await _set_draft(c, {"token": tok, "bot_id": info.bot_id, "username": info.username,
                         "cur_name": info.name, "name": info.name,
                         "photo_id": None, "photo_kind": None, "buyer": 0})
    return True, (f"✅ <b>@{esc(info.username)}</b> 확인! (봇 이름: {esc(info.name)})\n"
                  "이름·프사를 정하고 [✅ 만들기] 를 눌러.")


# ── 조립 화면 (이름·프사·구매자) ─────────────────────────────────
async def s_draft(c: PanelCtx) -> Screen:
    if not await _is_owner(c):
        return NOT_OWNER
    d = await _get_draft(c)
    if not d or not d.get("token"):
        return await s_main(c)
    photo = {"photo": "🖼 사진", "video": "🎬 영상프사", "animation": "🎬 영상프사"}.get(d.get("photo_kind"), "없음(그대로)")
    lines = [
        "🏷 <b>새 클론 조립</b>", "",
        f"봇: <b>@{esc(d['username'])}</b>  (@아이디는 BotFather 에서만 바꿀 수 있어)",
        f"이름: <b>{esc(d['name'])}</b>" + ("" if d["name"] == d["cur_name"] else f"  (원래 {esc(d['cur_name'])})"),
        f"프사: {photo}",
        f"구매자(오너로 추가): {d['buyer'] or '없음 (나만 오너)'}", "",
        "맞으면 [✅ 만들기]. 누르면 봇 이름·프사를 텔레그램에 적용하고 클론을 준비해.",
    ]
    tok = menu.token(c.svc, c.uid, 0, "clmake", c.uid)
    kb = [
        [B("✏️ 봇 이름", "m:clein:name"), B("🖼 프사/영상", "m:clein:pic")],
        [B("👤 구매자 ID", "m:clein:buyer")],
        [B("✅ 만들기", f"m:k:{tok}"), B("🗑 취소", "m:clcancel")],
    ]
    return Screen("\n".join(lines), menu._kb(kb))


# 글자 입력 시작 — 오너 1:1 전용이라 방(scoped "in" 라우트)을 안 쓰고 svc.inputs 를 직접 세팅.
# (m:in:0:... 은 scoped 라서 has_chat(0) 실패 → "없는 그룹이에요" 였음. r_new 의 cltok 과 같은 방식)
_PROMPTS = {
    "clname": "✏️ 새 <b>봇 이름</b>을 글자로 보내줘 (최대 32자).",
    "clpic": ("🖼 프사로 쓸 <b>사진</b>이나 <b>영상(움짤)</b>을 보내줘. 영상이면 움직이는 프사가 돼.\n\n"
              "안 바꿀 거면 안 보내도 돼 — 구매자가 BotFather 에서 넣은 프사 그대로 둬 (<code>취소</code>)."),
    "clbuyer": "👤 구매자 <b>@아이디</b>나 <b>숫자 ID</b>를 보내줘. 없이 갈 거면 안 보내도 돼.",
}


async def r_startinput(c: PanelCtx) -> Screen:
    if not await _is_owner(c):
        return NOT_OWNER
    kind = "cl" + c.arg(0)
    if kind not in _PROMPTS:
        return await s_draft(c)
    c.svc.inputs[c.uid] = menu.PendingInput(kind, 0)
    return Screen(_PROMPTS[kind] + "\n\n5분 안에. 그만두려면 <code>취소</code>.",
                  menu._kb([[B("❌ 취소", "m:cl")]]))


async def i_name(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    d = await _get_draft(c)
    if not d:
        return False, "진행 중인 클론이 없어. 처음부터 다시."
    name = (msg.text or "").strip()
    if not name:
        return False, "이름을 글자로 보내줘."
    d["name"] = name[:clone.MAX_NAME]
    await _set_draft(c, d)
    return True, f"✅ 봇 이름을 <b>{esc(d['name'])}</b> 로."


async def i_pic(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    d = await _get_draft(c)
    if not d:
        return False, "진행 중인 클론이 없어."
    kind, fid = extract_media(msg)
    if not fid or kind not in ("photo", "video", "animation"):
        return False, "사진이나 영상(움짤)을 보내줘. 영상이면 움직이는 프사가 돼."
    d["photo_id"], d["photo_kind"] = fid, kind
    await _set_draft(c, d)
    label = "영상프사" if kind in ("video", "animation") else "사진"
    return True, f"✅ {label} 받았어."


async def _resolve_handle(c: PanelCtx, raw: str) -> int | None:
    """@아이디 → 숫자 ID: 소담 기록(users) 먼저, 없으면 텔레그램 직접(namehist.resolve_remote)."""
    handle = raw.strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{4,32}", handle):
        return None
    row = await c.svc.db._one("SELECT user_id FROM users WHERE username=? COLLATE NOCASE", (handle,))
    if row:
        return row["user_id"]
    try:
        uid, _ = await namehist.resolve_remote(c.svc, "@" + handle, c.uid)
    except Exception:
        return None
    return uid or None


async def i_buyer(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    d = await _get_draft(c)
    if not d:
        return False, "진행 중인 클론이 없어."
    raw = (msg.text or "").strip()
    bid = to_int(raw)
    if bid is None or bid <= 0:
        bid = await _resolve_handle(c, raw)   # @아이디도 받음 (예전엔 숫자만 받아 '@Zzaggung' 을 거절, 2026-11-16)
    if not bid:
        return False, ("구매자를 못 찾았어. 숫자 ID 나 @아이디 로 보내줘 (🆔 로 확인). "
                       "소담이 한 번도 못 본 아이디면 숫자 ID 가 확실해.")
    d["buyer"] = bid
    await _set_draft(c, d)
    return True, f"✅ 구매자 {bid} 를 그 봇의 오너로 추가할게 (나도 계속 총괄 오너)."


async def r_cancel(c: PanelCtx) -> Screen:
    if not await _is_owner(c):
        return NOT_OWNER
    await _clear_draft(c)
    return await s_main(c)


# ── 만들기 (적용 + env + 등록) ───────────────────────────────────
async def _download(c: PanelCtx, fid: str) -> bytes | None:
    try:
        f = await c.bot.get_file(fid)
        return bytes(await f.download_as_bytearray())
    except TelegramError:
        return None


async def t_make(c: PanelCtx, arg) -> Screen:
    if not await _is_owner(c):
        return NOT_OWNER
    d = await _get_draft(c)
    if not d or not d.get("token"):
        return await s_main(c)

    photo = video = None
    if d.get("photo_id"):
        data = await _download(c, d["photo_id"])
        if d["photo_kind"] in ("video", "animation"):
            video = data
        else:
            photo = data

    errs = await clone.apply_branding(d["token"], name=d["name"], photo=photo, video=video)

    db = c.svc.db
    slug = await clone.next_slug(db, d["username"])
    port = await clone.next_port(db)
    owner_ids = set(c.svc.cfg.owner_ids) | ({d["buyer"]} if d.get("buyer") else set())
    info = clone.BotInfo(bot_id=d["bot_id"], username=d["username"], name=d["name"])
    try:
        clone.write_env(c.svc.cfg.db_path, slug, d["token"],
                        owner_ids, port, bot_name=d["name"], buyer_id=d.get("buyer", 0),
                        trial_days=c.svc.cfg.trial_days)
    except clone.CloneError as e:
        return Screen(None, toast=str(e)[:190], alert=True)
    await clone.register(db, slug=slug, info=info, name=d["name"], port=port, buyer_id=d.get("buyer", 0))
    await _clear_draft(c)

    note = "" if not errs else "\n⚠️ 일부 적용 실패: " + "; ".join(esc(e) for e in errs)
    screen = await s_main(c)
    screen.text = (
        f"✅ <b>클론 준비됨!</b>  @{esc(d['username'])} · <code>{esc(slug)}</code>{note}\n\n"
        "이름·프사는 텔레그램에 적용했어. 서버에서 켜면 바로 돌아가 "
        f"(지금은 반자동 — 내가 <code>sodam@{esc(slug)}</code> 켤게).\n\n" + (screen.text or ""))
    return screen


# ── 클론 상세 (끄기 등) ─────────────────────────────────────────
async def s_clone(c: PanelCtx) -> Screen:
    if not await _is_owner(c):
        return NOT_OWNER
    r = await clone.get_clone(c.svc.db, c.arg(0))
    if not r:
        return await s_main(c)
    lines = [f"⚙️ <b>{esc(r['name'])}</b> (@{esc(r['username'])})",
             f"상태: {esc(r['status'])} · 포트 {r['port']} · 구매자 {r['buyer_id'] or '없음'}",
             f"이름(slug): <code>{esc(r['slug'])}</code>"]
    tok = menu.token(c.svc, c.uid, 0, "clremove", r["slug"])
    kb = [[B("📦 이 봇으로 기존 방 넘기기", f"m:cltx:{r['slug']}")],
          [B("🗑 클론 삭제", f"m:k:{tok}")], [B("⬅️ 목록", "m:cl")]]
    return Screen("\n".join(lines), menu._kb(kb))


# ── 기존 방 → 이 클론으로 이관 (설계 docs/WHITELABEL_ROOM_MIGRATION_DESIGN.md) ─────────
TX_KEY = "clone:tx"   # chat_state(0, "clone:tx:<uid>") = {slug, chat_id}


async def r_transfer_start(c: PanelCtx) -> Screen:
    if not await _is_owner(c):
        return NOT_OWNER
    slug = c.arg(0)
    if not await clone.get_clone(c.svc.db, slug):
        return await s_main(c)
    c.svc.inputs[c.uid] = menu.PendingInput("cltxroom", 0, args=[slug])
    return Screen(
        "📦 <b>기존 방 넘기기</b>\n\n이 클론봇으로 넘길 <b>기존 방의 숫자 ID</b>를 보내줘.\n"
        "(그 방 구독·손님 포인트·설정·기억·기록이 이 봇 DB 로 복사돼. 방에서 봇 교체는 복사 뒤 손으로)\n\n"
        "5분 안에. 그만두려면 <code>취소</code>.",
        menu._kb([[B("❌ 취소", f"m:cls:{slug}")]]))


async def i_transfer_room(c: PanelCtx, msg) -> tuple[bool, str]:
    slug = c.args[0] if c.args else ""
    cid = to_int((msg.text or "").strip())
    if not cid or not await c.svc.db.has_chat(cid):
        return False, "그 방 ID 를 못 찾았어. 봇이 들어가 있는 방의 숫자 ID 를 보내줘."
    await c.svc.db.set_state(0, f"{TX_KEY}:{c.uid}", {"slug": slug, "chat_id": cid})
    return True, ""


async def s_tx_preview(c: PanelCtx) -> Screen:
    if not await _is_owner(c):
        return NOT_OWNER
    d = await c.svc.db.get_state(0, f"{TX_KEY}:{c.uid}", None)
    if not d:
        return await s_main(c)
    cid, slug = d["chat_id"], d["slug"]
    r = await clone.get_clone(c.svc.db, slug)
    if not r:
        return await s_main(c)
    bets = await clone_migrate.open_bets(c.svc.db, cid)
    pts = await clone_migrate.wallet_sum(c.svc.db, cid)
    mrow = await c.svc.db._one("SELECT COUNT(*) AS n FROM members WHERE chat_id=?", (cid,))
    sub = await c.svc.db.get_subscription(cid)
    crow = await c.svc.db._one("SELECT title FROM chats WHERE chat_id=?", (cid,))
    title = (crow["title"] if crow and crow["title"] else str(cid))
    lines = [
        "📦 <b>방 이관 확인</b>", "",
        f"넘길 방: <b>{esc(title)}</b> (<code>{cid}</code>)",
        f"받을 봇: <b>{esc(r['name'])}</b> (@{esc(r['username'])})",
        f"멤버: {mrow['n'] if mrow else 0}명 · 포인트 합: {pts:,}",
        f"구독: {'있음' if sub and sub.get('paid_until') else '없음'}", "",
    ]
    if bets:
        lines.append(f"🎰 지금 열린 베팅 {bets}건 — 게임 끝나고 해야 돈이 안 꼬여. 지금은 못 함.")
        return Screen("\n".join(lines), menu._kb([[B("🔄 다시 확인", "m:cltxp"), B("❌ 취소", f"m:cls:{slug}")]]))
    lines.append("손님 포인트·구독·설정·기억·기록을 이 봇 DB 로 복사해. "
                 "복사 뒤 방에서 본체 소담 내보내고 클론 봇을 관리자로 넣어.")
    tok = menu.token(c.svc, c.uid, cid, "cltxgo", slug)
    kb = [[B("✅ 이관 실행", f"m:k:{tok}")], [B("❌ 취소", f"m:cls:{slug}")]]
    return Screen("\n".join(lines), menu._kb(kb))


async def t_transfer_go(c: PanelCtx, slug) -> Screen:
    if not await _is_owner(c):
        return NOT_OWNER
    cid = c.cid
    ok, note, _counts = await clone_migrate.transfer_room(c.svc, cid, str(slug))
    if ok:
        await c.svc.db.set_state(cid, "migrated_to", str(slug))   # 본체: 이 방은 클론으로 넘어감(기록)
        await c.svc.db.set_state(0, f"{TX_KEY}:{c.uid}", None)
    screen = await s_clone(PanelCtx(c.svc, c.bot, c.uid, None, [str(slug)]))
    screen.text = note + "\n\n" + (screen.text or "")
    return screen


async def t_remove(c: PanelCtx, slug) -> Screen:
    if not await _is_owner(c):
        return NOT_OWNER
    slug = str(slug)
    clone.remove_env(c.svc.cfg.db_path, slug)        # .env 지우면 자동 켜기 데몬이 끔
    await clone.unregister(c.svc.db, slug)
    screen = await s_main(c)
    screen.toast = f"🗑 {slug} 삭제 — 서버에서 그 봇을 끌게."
    return screen


# ── 등록 ────────────────────────────────────────────────────────
menu.register_main(97, "cl", "🏷 화이트라벨", need=OWNER)
for _code, _fn in (("cl", s_main), ("clnew", r_new), ("cls", s_clone),
                   ("clcancel", r_cancel), ("clein", r_startinput),
                   ("cltx", r_transfer_start), ("cltxp", s_tx_preview)):
    menu.register_route(_code, Route(_fn, OWNER, scoped=False))
menu.register_input("cltok", "", "cl", i_token, s_draft, need=OWNER)
menu.register_input("clname", "", "cl", i_name, s_draft, need=OWNER)
menu.register_input("clpic", "", "cl", i_pic, s_draft, media=True, need=OWNER)
menu.register_input("clbuyer", "", "cl", i_buyer, s_draft, need=OWNER)
menu.register_input("cltxroom", "", "cl", i_transfer_room, s_tx_preview, need=OWNER)
menu.register_token_action("clmake", t_make, fresh=True, need=OWNER)
menu.register_token_action("clremove", t_remove, fresh=True, need=OWNER)
menu.register_token_action("cltxgo", t_transfer_go, fresh=True, need=OWNER)
