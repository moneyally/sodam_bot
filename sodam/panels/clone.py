"""🏷 화이트라벨 클론봇 마법사 (오너 1:1 전용) — sodam/clone.py 엔진 위의 버튼 화면.

흐름: [➕ 새 클론] → 토큰 붙여넣기(getMe 검증·즉시 삭제) → 봇 이름 → 프사 사진/영상(선택)
     → 구매자 ID(선택) → 미리보기 → [✅ 만들기] → 봇 API 로 이름·프사 적용 + .env.clone 생성 + 등록.
켜기는 systemd 인스턴스 유닛(deploy/sodam@.service). 자동 켜기 데몬(sodam-clone-apply) 전엔 ssh 반자동.
"""
from __future__ import annotations

from telegram import Message
from telegram.error import TelegramError

from .. import clone, menu
from ..announce import extract_media
from ..menu import OWNER, B, PanelCtx, Route, Screen
from ..util import esc

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
        [B("✏️ 봇 이름", "m:in:0:clname"), B("🖼 프사/영상", "m:in:0:clpic")],
        [B("👤 구매자 ID", "m:in:0:clbuyer")],
        [B("✅ 만들기", f"m:k:{tok}"), B("🗑 취소", "m:clcancel")],
    ]
    return Screen("\n".join(lines), menu._kb(kb))


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


async def i_buyer(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    d = await _get_draft(c)
    if not d:
        return False, "진행 중인 클론이 없어."
    raw = (msg.text or "").strip()
    from ..util import to_int
    bid = to_int(raw)
    if bid is None or bid <= 0:
        return False, "구매자 텔레그램 숫자 ID 를 보내줘 (🆔 로 확인). 없이 갈 거면 그대로 둬도 돼."
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
    kb = [[B("🗑 클론 삭제", f"m:k:{tok}")], [B("⬅️ 목록", "m:cl")]]
    return Screen("\n".join(lines), menu._kb(kb))


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
for _code, _fn in (("cl", s_main), ("clnew", r_new), ("cls", s_clone), ("clcancel", r_cancel)):
    menu.register_route(_code, Route(_fn, OWNER, scoped=False))
menu.register_input("cltok", "", "cl", i_token, s_draft, need=OWNER)
menu.register_input("clname", "", "cl", i_name, s_draft, need=OWNER)
menu.register_input("clpic", "", "cl", i_pic, s_draft, media=True, need=OWNER)
menu.register_input("clbuyer", "", "cl", i_buyer, s_draft, need=OWNER)
menu.register_token_action("clmake", t_make, fresh=True, need=OWNER)
menu.register_token_action("clremove", t_remove, fresh=True, need=OWNER)
