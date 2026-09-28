"""🧩 스티커 AI 도구 make_sticker + sticker_catalog (sodam/stickerforge — 텔레그램 영상 스티커 공방).

소담(AI)이 사진을 보고 요청을 **아트 디렉터처럼** spec 으로 옮김: 원본 종류·배경·이미 있는 글자·얼굴 위치·분위기를 먼저 읽고,
sticker_catalog 로 요청에 맞는 검증된 조합(레시피, 계열이 서로 다른 후보) 과 쓸 수 있는 부품 전체를 받아 고르거나 직접 조합.
코드는 sanitize 로 이름·숫자·범위만 통과시키고, 엔진 검사표(규격) + qc 경고(자막 겹침·잘림·구멍·밋밋/요란·하얗게 날아감) 를 돌려줌.
경고가 있으면 한 번은 고쳐 다시 만들고(accept_warnings 없이 두 번째면 그대로 보냄), 규격 실패면 효과 하나를 덜고 한 번 더.
원본 = 붙은/답장한 사진, 없으면 요청자 프사. 사람마다 하루 FREE_DAILY 개.
보내기 = 스티커(방에서 바로 움직이는 걸 봄) + 파일(@Stickers 로 팩 등록용 — 영상으로 보내면 재압축돼 거절됨).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from telegram import InputFile
from telegram.constants import ChatAction
from telegram.error import TelegramError

from .. import featreq, hooks, stickerforge as SF, stickerlearn as L, tools
from ..stickerforge import recipes
from ..util import display_name, esc, user_name
from .avatar import _profile_photo

log = logging.getLogger(__name__)
FREE_DAILY = 5
GUIDE = "팩 만들기: @Stickers → /newvideo → 팩 이름 → 이 파일(📎 파일로) → 이모지 → /publish"

# 아트 디렉터 프롬프트 (make_sticker 설명). 긴 카탈로그는 sticker_catalog 도구·recipes.py 에 있으므로 여기선 판단 순서만.
DESIGN = (
    "순서: ①사진을 읽는다(종류: 단색배경 캐릭터=cutout·keying auto / 검은배경에 빛나는 그림=cutout·keying glow / 실사·꽉 찬 그림=photo, "
    "이미 박힌 글자(있으면 caption 없이), 얼굴 위치, 분위기) ②sticker_catalog(요청, 종류)로 후보 레시피 3개와 부품을 받는다 "
    "③하나를 고르거나 부품을 직접 조합해 spec 을 만든다: {recipe, seed} 만 줘도 되고 motion[1~2]·fx[1~3]·caption·framing 을 덮어쓸 수 있다. "
    "④결과의 경고(warnings)가 오면 경고가 말하는 것 하나만 고쳐 한 번 더 부른다(두 번째도 경고면 accept_warnings=true). "
    "카탈로그의 '학습 레시피'(이 방에서 👍 받은 조합)는 recipe 이름으로 바로 쓴다. 없는 효과('눈에서 레이저')를 원하면 가장 가까운 "
    "조합으로 만들고 wanted 에 그 말을 넣는다(기능 요청 접수) — 새 효과를 코드로 만들거나 ffmpeg 인자를 지어내지 않는다. request 에 요청 원문. "
    "판단 규칙: 요란함보다 사진에 맞는 조합(움직임 적을수록 화질↑). 같은 사람이 또 부탁하거나 '별로·다르게'면 계열(잔잔/강조/점프/임팩트/둥둥)을 바꾼다. "
    "seed 를 매번 다르게(사람·시각) 줘서 같은 요청도 조금씩 다르게. 한국어 표현: 강렬·쿵·임팩트=impact(slam·jab·recoil) / 화사·신남=bounce·rays / "
    "잔잔·잘자·편안=calm(breathe·idle·meteors·zzz) / 귀엽=wobble·hearts / 고급=sweep·silver·gold / 네온·번개=glow·aura·bolts / "
    "출근·전송·완료=punch·scan·outline / 흑백·고대비 그림엔 glitch 대신 slice_glitch. photo 는 framing auto(얼굴 우선)·blur(전체 담기)·top."
)


def catalog_text(query: str, kind: str | None, prefs: dict | None = None) -> str:
    """요청·종류에 맞는 레시피 후보(계열이 서로 다른 3개) + 부품 전체 (도구 결과라 매 호출에 붙지 않음).
    prefs(stickerlearn.preferences) 가 있으면 이 방·이 사람이 좋아한 조합을 앞에, 별로였던 건 뒤에, 최근 계열은 미룸."""
    kind = kind if kind in ("cutout", "photo", "glow", "mono") else None
    cands = [r for r in recipes.RECIPES if not kind or kind in r["kinds"]]
    cands.sort(key=lambda r: -sum(m in (query or "") for m in r["moods"]))     # 분위기 단어 일치 순 (안정 정렬)
    if prefs:
        cands = L.order_candidates(cands, prefs)
    picks, used = [], set()
    for r in cands:
        if recipes.family(r) not in used:
            picks.append(r)
            used.add(recipes.family(r))
        if len(picks) == 3:
            break
    lines = ["레시피 후보 (recipe 이름 · 계열 · 움직임+효과 · 자막색 · 어울림):"]
    for l in (prefs or {}).get("liked", [])[:3]:                       # 이 방에서 좋아했던 조합 (학습, recipe 이름으로 바로 씀)
        lines.append(f"- {l['name']} · {l['family']} · {l['signature'].replace('+', ' + ')} · 이 방에서 👍{l['good']} (학습 레시피)")
    for r in picks:
        combo = " + ".join([m["type"] for m in r["motion"]] + [f["type"] for f in r["fx"]])
        lines.append(f"- {r['name']} · {recipes.family(r)} · {combo} · {r['palette']} · {' '.join(r['moods'][:4])}")
    cat = SF.catalog()

    def fmt(d):
        return " · ".join(f"{n}({', '.join(f'{k}={v}' for k, v in e['params'].items())}): {e['doc']}" if e["params"] else f"{n}: {e['doc']}"
                          for n, e in d.items())
    lines.append("움직임(motion, 계열 " + ", ".join(sorted(set(recipes.FAMILY.values()))) + "): " + fmt(cat["motions"]))
    lines.append("효과(fx, 그리는 순서 = 목록 순서: 배경 → 빛 → 겹침 → 왜곡): " + fmt(cat["fx"]))
    lines.append("자막 anims(등장 하나 + 나머지, 최대 4): " + " · ".join(f"{k}: {v}" for k, v in cat["caption_anims"].items())
                 + ". palette " + "·".join(cat["palettes"]) + ". text 2~7자, position top/bottom.")
    lines.append("photo framing: auto·center·top·blur, radius 0~256. margin: 잔잔 0.06~0.10, wobble·pop·jab 0.12, hop·slam 0.16. "
                 "text 인자(sfx_text·bubble)는 8자, points(laser)는 [[x,y],...] 512 좌표.")
    lines.append(f"레시피 전체 이름: {', '.join(r['name'] for r in recipes.RECIPES)}")
    return "\n".join(lines)


async def t_sticker_catalog(ctx: tools.ToolCtx, a: dict) -> str:
    prefs = await L.preferences(ctx.svc.db, ctx.chat_id, ctx.caller.id, "ump" if a.get("for_video") else "sticker")
    return catalog_text(str(a.get("query") or ""), a.get("kind"), prefs)


async def resolve_spec(ctx: tools.ToolCtx, raw: dict, product: str) -> tuple[dict | None, str | None]:
    """spec.recipe 가 이 방의 학습 레시피 이름이면 그 spec 을 깔고 나머지 키로 덮는다 (정적 레시피는 sanitize 가 처리)."""
    raw = dict(raw) if isinstance(raw, dict) else {}
    name = raw.get("recipe")
    if name and name not in recipes.BY_NAME:
        learned = await L.learned_recipe(ctx.svc.db, ctx.chat_id, str(name), product)
        if not learned:
            return None, f"recipe '{name}' 없음 — sticker_catalog 의 이름만"
        raw = {**learned, **{k: v for k, v in raw.items() if k != "recipe"}}
        await L.touch_recipe(ctx.svc.db, ctx.chat_id, str(name), product)
    return SF.sanitize(raw)


async def note_wanted(ctx: tools.ToolCtx, wanted: str, used: str) -> str:
    """목록에 없는 효과를 부탁받은 경우: 가장 가까운 조합으로 만들었다고 남기고 feature_request 로 접수 (새 부품은 코드로만)."""
    wanted = " ".join(str(wanted).split())[:60]
    if not wanted:
        return ""
    await featreq.submit(ctx.svc.db, ctx.caller.id, user_name(ctx.caller), ctx.chat_id, f"스티커 효과: {wanted}", f"대신 쓴 조합: {used}")
    return f" '{wanted}' 는 아직 없는 효과라 가장 가까운 조합({used})으로 대신했고 운영자에게 기능 요청으로 접수했음 — 무엇으로 대신했는지 한마디."


async def _busy(ctx) -> None:
    while True:
        try:
            await ctx.bot.send_chat_action(ctx.chat_id, ChatAction.CHOOSE_STICKER)
        except TelegramError:
            pass
        await asyncio.sleep(4)


def _used(spec: dict) -> str:
    return " + ".join([m["type"] for m in spec["motion"]] + [f["type"] for f in spec["fx"]])


async def t_make_sticker(ctx: tools.ToolCtx, a: dict) -> str:
    spec, err = await resolve_spec(ctx, a.get("spec") or {}, "sticker")
    if err:
        return f"spec 오류: {err}. 고쳐서 다시 부를 것."
    from ..avatar import available
    if not available():
        return "지금 서버에 영상 도구(ffmpeg)가 없어 스티커를 못 만듦."
    uid, db = ctx.caller.id, ctx.svc.db
    src = ctx.image.data if ctx.image is not None else await _profile_photo(ctx.bot, uid)
    if not src:
        return "원본 사진이 없음 (붙은 사진·답장한 사진 없고 프사도 못 가져옴). 사진과 함께 다시 부탁하라고 안내."
    day = datetime.now(ctx.svc.cfg.tz).strftime("%Y-%m-%d")
    if await db.counter(day, 0, f"stk:{uid}") >= FREE_DAILY:
        return f"스티커는 한 사람 하루 {FREE_DAILY}개까지. 내일 다시 가능하다고 안내."
    busy = asyncio.create_task(_busy(ctx))
    outcome = "ok"
    try:
        res = await SF.forge(src, spec, icon=bool(a.get("icon")))
        if not res.ok and spec["fx"]:          # 크기·검사 실패 → 효과 하나 덜고 한 번 더 (SKILL: 움직임·효과를 줄여야 화질이 삼)
            spec = {**spec, "fx": spec["fx"][:-1]}
            res = await SF.forge(src, spec, icon=bool(a.get("icon")))
            outcome = "retry"
    finally:
        busy.cancel()
    request = str(a.get("request") or "")
    if not res.ok:
        log.warning("sticker failed: %s", res.summary())
        await L.log(db, chat_id=ctx.chat_id, user_id=uid, request=request, kind=res.keying, spec=spec, outcome="fail")
        return f"스티커가 텔레그램 규격 검사를 통과 못 함 ({res.summary()[:120]}). 효과를 줄이거나 다른 사진으로 다시 하자고 안내."
    if res.warnings and not a.get("accept_warnings"):   # 소담이는 결과를 못 보니 지표가 대신 말함 → 한 번 고쳐 다시
        return ("만들었지만 검수 경고 (아직 안 보냄): " + " / ".join(res.warnings)
                + f". 지표 {res.metrics}. 경고가 말하는 것 하나만 고쳐 다시 부를 것 — 그래도 경고면 accept_warnings=true 로 보냄.")
    name = display_name(ctx.caller.first_name, ctx.caller.last_name, ctx.caller.username)
    try:
        sent = await ctx.bot.send_sticker(ctx.chat_id, InputFile(res.webm, filename="sticker.webm"))
        await ctx.bot.send_document(ctx.chat_id, InputFile(res.webm, filename="sodam_sticker.webm"), parse_mode="HTML",
                                    caption=f"🧩 {esc(name)}님 스티커 파일\n{GUIDE}")
        if res.icon:
            await ctx.bot.send_document(ctx.chat_id, InputFile(res.icon, filename="pack_icon.webm"),
                                        caption="팩 아이콘 (100×100) — /publish 뒤 아이콘 물을 때 이 파일")
    except TelegramError as e:
        return f"스티커는 만들었는데 전송 실패: {e.message}"
    await db.bump(day, 0, f"stk:{uid}")
    await L.log(db, chat_id=ctx.chat_id, user_id=uid, request=request, kind=res.keying, spec=spec, outcome=outcome,
                msg_id=getattr(sent, "message_id", 0))
    note = f" (경고 안고 보냄: {'; '.join(res.warnings)})" if res.warnings else ""
    note += await note_wanted(ctx, a.get("wanted") or "", _used(spec))
    return f"스티커와 파일을 방에 보냈음 (배경: {res.keying}, 조합: {_used(spec)}){note}. 한마디만 짧게, 다른 느낌 원하면 말하라고."


tools.register_tool(tools.Tool(
    "sticker_catalog",
    "스티커·움프 디자인 카탈로그 검색(읽기만): 요청 문구와 원본 종류에 맞는 검증된 조합(레시피) 후보 3개(계열이 서로 다름) 와 "
    "쓸 수 있는 움직임·효과·자막 부품 전체를 준다. make_sticker / make_profile_video 의 spec 을 정하기 전에 부른다.",
    {"query": {"type": "string", "description": "요청 문구 그대로 (예: '출근완료 강렬하게', '잘자요 잔잔하게')"},
     "kind": {"type": "string", "enum": ["cutout", "photo", "glow", "mono"],
              "description": "원본 종류: cutout=단색 배경 캐릭터 · photo=실사·꽉 찬 그림 · glow=검은 배경 발광 · mono=흑백 고대비"},
     "for_video": {"type": "boolean", "description": "움프(프로필 영상)용이면 true"}},
    ["query"], t_sticker_catalog), read_only=True)

tools.register_tool(tools.Tool(
    "make_sticker",
    "텔레그램 움직이는 스티커(영상 스티커)를 만들어 방에 보낸다. 원본 = 붙은·답장한 사진, 없으면 요청자 프사. "
    "'이걸로 스티커 만들어줘', '배경 빼고 글리치 넣어서 출근완료', '잘자요 느낌으로 잔잔하게'. " + DESIGN,
    {"spec": {"type": "object", "description": "{recipe, seed} 또는/그리고 mode·keying·motion[{type,...}]·fx[{type,...}]·"
                                               "caption{text,palette,anims,position}·framing·margin·radius"},
     "icon": {"type": "boolean", "description": "팩 아이콘(100×100)도 같이 — 팩 만든다고 할 때만"},
     "accept_warnings": {"type": "boolean", "description": "검수 경고를 한 번 고친 뒤에도 남으면 true 로 그대로 보냄"},
     "request": {"type": "string", "description": "사용자 요청 원문 (200자, 학습 기록용)"},
     "wanted": {"type": "string", "description": "목록에 없는 효과를 원했을 때 그 말 그대로 (예: '눈에서 레이저') — 가까운 조합으로 만들고 기능 요청으로 접수됨"}},
    ["spec"], t_make_sticker))

hooks.add_group_message_hook(L.on_group_message)   # 우리 스티커에 '좋다/별로' 답장 → 점수
hooks.add_reaction_hook(L.on_reaction)             # 👍❤️🔥 반응 → 점수
hooks.add_tick_hook(L.tick)                        # 90일 안 쓴 학습 레시피 정리 (하루 한 번)
