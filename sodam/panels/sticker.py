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
import random
import time
from datetime import datetime

from telegram import InputFile
from telegram.constants import ChatAction
from telegram.error import TelegramError

from .. import featreq, hooks, stickerforge as SF, stickerlearn as L, tools
from ..stickerforge import examples as EX, recipes
from ..util import display_name, esc, user_name
from .avatar import PHOTO_OF, source_photo

log = logging.getLogger(__name__)
FREE_DAILY = 5
GUIDE = "팩 만들기: @Stickers → /newvideo → 팩 이름 → 이 파일(📎 파일로) → 이모지 → /publish"

COMPOSE = EX.COMPOSE


def _kind_str(kind: tuple) -> str:
    k = kind[0]
    if k in ("num", "int"):
        return f"{kind[1]:g}~{kind[2]:g}"
    if k in ("osc", "range"):
        return f"{kind[1]:g}~{kind[2]:g}|[a,b]"
    if k == "enum":
        return "|".join(kind[1])
    return {"color": "[r,g,b]", "colors": "[[r,g,b],…]", "xy": "[x,y]0~1", "times": "[t,…]0~1",
            "text": f"≤{kind[1]}자" if len(kind) > 1 else "글자", "bool": "true"}[k]


_STOP = {"하게", "느낌", "해줘", "되게", "나오", "오게", "지게", "멋지", "으로", "이게"}   # 말끝 — 예시 고르기에 안 셈


def _bigrams(t: str) -> set:
    t = "".join((t or "").split())
    return {t[i:i + 2] for i in range(len(t) - 1)} - _STOP


def parts_text() -> str:
    """부품(프리미티브)과 값 범위 — 코드(sanitize 의 layer_params·prims)에서 바로 뽑음 (문서와 어긋나지 않게)."""
    from ..stickerforge import motions, prims
    lines = ["[motion ≤2] keyframes{pivot:[x,y]축, keys:[{t 0~1, scale, sx, sy, rotate(도,+시계), x, y(화면 비율), opacity, ease}]≤16} "
             f"ease={'|'.join(prims.EASES)} · 이름 있는 움직임: {', '.join(sorted(motions.PRESETS))}"]
    for name, schema in SF.layer_params().items():
        lines.append(f"[{name}] " + " ".join(f"{k}:{_kind_str(v)}" for k, v in schema.items()))
    lines.append("공통 start·end(0~1 시간 창). particles angle 0=오른쪽 90=아래 -90=위 · blend add=빛(불티·네온) · 연기=smoke+blur 2~4+grow 2~3 · "
                 f"shape=char 면 char 에 글자(이모지는 비슷한 모양으로). 입자 합계 {SF.PARTICLE_BUDGET}, fx+layers ≤{SF.MAX_LAYERS}.")
    lines.append("transition = 앞에 그린 것 전체가 사라짐/나타남(direction out|in), to=[r,g,b] 면 그 색이 남음. "
                 "움프 loop:false = 6초 한 번(타서 없어지기 등), 기본 = 3초 반복×2.")
    named = sorted(n for n in SF.catalog()["fx"] if n not in prims.LAYERS)   # 같은 이름(flash)은 layers 에선 새 부품
    lines.append("[효과 이름도 layers 에 그대로] " + ", ".join(named) + " (설명은 section=effects)")
    return "\n".join(lines)


def examples_text(query: str, limit: int) -> str:
    """요청과 가장 닮은 예시부터 limit 글자까지."""
    import json
    from ..stickerforge.examples import EXAMPLES
    q = _bigrams(query)
    ranked = sorted(EXAMPLES, key=lambda e: -len(q & _bigrams(e["request"] + e["why"])))
    out, used = [], 0
    for e in ranked:
        line = f"'{e['request']}' → {json.dumps(e['spec'], ensure_ascii=False, separators=(',', ':'))} ({e['why']})"
        if e.get("wanted"):
            line += f" wanted='{e['wanted']}'"
        if out and used + len(line) > limit:
            continue
        out.append(line)
        used += len(line)
    return "\n".join(out)


def catalog_text(query: str, kind: str | None = None, prefs: dict | None = None, section: str = "parts") -> str:
    """sticker_catalog 결과. 도구 결과는 4000자(agent.TOOL_RESULT_CHARS)에서 가운데가 잘리므로 section 으로 나눔
    (예전엔 6,900자라 부품 설명 가운데가 잘려 AI 가 못 봤음).
    parts(기본) = 부품·값 범위 + 요청과 닮은 예시 · examples = 예시 전체 · effects = 이름 있는 움직임·효과 설명 · recipes = 옛 레시피."""
    head = ["조합: 사용자 말 → 부품 값 (예시는 베끼지 말고 말에 맞게)."]
    for l in (prefs or {}).get("liked", [])[:3]:                       # 이 방에서 좋아했던 조합 (학습, recipe 이름으로 바로 씀)
        head.append(f"- {l['name']} · {l['family']} · {l['signature'].replace('+', ' + ')} · 이 방에서 👍{l['good']} (학습 레시피)")
    if section == "examples":
        return "\n".join(head + ["예시:", examples_text(query, 3700)])
    if section == "effects":
        cat = SF.catalog()

        def fmt(d):
            return " · ".join(f"{n}: {e['doc']}" for n, e in d.items())
        return "\n".join(head + ["움직임: " + fmt(cat["motions"]), "효과: " + fmt(cat["fx"]),
                                 "자막 anims: " + ", ".join(cat["caption_anims"]) + " · palette " + "·".join(cat["palettes"])])
    if section == "recipes":
        kind = kind if kind in ("cutout", "photo", "glow", "mono") else None
        cands = [r for r in recipes.RECIPES if not kind or kind in r["kinds"]]
        cands.sort(key=lambda r: -sum(m in (query or "") for m in r["moods"]))
        if prefs:
            cands = L.order_candidates(cands, prefs)
        return "\n".join(head + [f"- {r['name']} · " + " + ".join([m["type"] for m in r["motion"]] + [f["type"] for f in r["fx"]])
                                 for r in cands[:12]])
    body = parts_text()
    ex = examples_text(query, max(600, 3850 - len(body) - sum(len(h) + 1 for h in head)))
    return "\n".join(head + [body, "예시 (section=examples 로 전체):", ex])


async def t_sticker_catalog(ctx: tools.ToolCtx, a: dict) -> str:
    prefs = await L.preferences(ctx.svc.db, ctx.chat_id, ctx.caller.id, "ump" if a.get("for_video") else "sticker")
    return catalog_text(str(a.get("query") or ""), a.get("kind"), prefs, str(a.get("section") or "parts"))


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
    if not isinstance(raw.get("seed"), int) or isinstance(raw.get("seed"), bool):
        raw["seed"] = random.randrange(1, 1000)            # 매번 다른 입자 배치·변주 (같은 요청도 똑같이 안 나오게)
    return SF.sanitize(raw)


async def same_as_last(ctx: tools.ToolCtx, spec: dict, request: str, product: str) -> str | None:
    """직전(하루 안)에 이 사람에게 만든 것과 조합이 값까지 똑같은데(fingerprint, seed 빼고) 요청 글은 다르면 → 다시 짜라는 안내.
    실제 사례: 17건 중 8건이 '줌+네온+별빛' — 무슨 말을 하든 같은 조합이 나옴."""
    request = " ".join((request or "").split())[:L.REQUEST_CHARS]
    if not request:
        return None
    row = await ctx.svc.db._one("SELECT spec, request FROM sticker_log WHERE chat_id=? AND user_id=? AND product=? AND outcome IN ('ok','retry') "
                                "AND ts>? ORDER BY id DESC LIMIT 1", (ctx.chat_id, ctx.caller.id, product, int(time.time()) - 86400))
    if not row or not row["request"] or row["request"] == request:
        return None
    import json
    if L.fingerprint(json.loads(row["spec"])) != L.fingerprint(spec):   # 이름만 같고 값이 다르면 다른 연출 → 통과
        return None
    return (f"직전 것과 똑같은 조합({L.describe(spec)})이라 안 만들었음. 이번 요청 '{request[:60]}' 의 낱말대로 부품을 바꿔서 다시 "
            "(모양·색·움직임·전환 중 최소 하나). 사용자가 정말 똑같이 원했으면 accept_warnings=true.")


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
    return L.describe(spec)


def lighter(spec: dict) -> dict | None:
    """규격 실패 때 한 번 더: 마지막 레이어(없으면 마지막 fx)를 뺀 spec (SKILL: 움직임·효과를 줄여야 화질이 삶)."""
    if spec.get("layers"):
        return {**spec, "layers": spec["layers"][:-1]}
    if spec.get("fx"):
        return {**spec, "fx": spec["fx"][:-1]}
    return None


async def t_make_sticker(ctx: tools.ToolCtx, a: dict) -> str:
    spec, err = await resolve_spec(ctx, a.get("spec") or {}, "sticker")
    if err:
        return f"spec 오류: {err}. 고쳐서 다시 부를 것."
    if not a.get("accept_warnings"):
        same = await same_as_last(ctx, spec, str(a.get("request") or ""), "sticker")
        if same:
            return same
    from ..avatar import available
    if not available():
        return "지금 서버에 영상 도구(ffmpeg)가 없어 스티커를 못 만듦."
    uid, db = ctx.caller.id, ctx.svc.db
    src, err = await source_photo(ctx, a)
    if not src:
        return err
    day = datetime.now(ctx.svc.cfg.tz).strftime("%Y-%m-%d")
    if await db.counter(day, 0, f"stk:{uid}") >= FREE_DAILY:
        return f"스티커는 한 사람 하루 {FREE_DAILY}개까지. 내일 다시 가능하다고 안내."
    busy = asyncio.create_task(_busy(ctx))
    outcome = "ok"
    try:
        res = await SF.forge(src, spec, icon=bool(a.get("icon")))
        if not res.ok and lighter(spec):       # 크기·검사 실패 → 효과 하나 덜고 한 번 더 (SKILL: 움직임·효과를 줄여야 화질이 삼)
            spec = lighter(spec)
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
    "움프·스티커 부품 목록(읽기): 쓸 수 있는 부품·값 범위와 요청→spec 예시. make_sticker·make_profile_video 전에 요청 원문으로 부른다.",
    {"query": {"type": "string", "description": "요청 원문 그대로 (닮은 예시를 먼저 보여 줌)"},
     "section": {"type": "string", "enum": ["parts", "examples", "effects", "recipes"],
                 "description": "parts(기본: 부품·범위·닮은 예시) · examples(예시 전체) · effects(이름 있는 움직임·효과 설명) · recipes(옛 레시피)"},
     "kind": {"type": "string", "enum": ["cutout", "photo", "glow", "mono"], "description": "recipes 볼 때 원본 종류"},
     "for_video": {"type": "boolean", "description": "움프면 true (이 방 학습 조합)"}},
    ["query"], t_sticker_catalog), read_only=True)

tools.register_tool(tools.Tool(
    "make_sticker",
    "텔레그램 움직이는 스티커(512 WebM)를 만들어 방에 보낸다. 원본 = photo_of > 붙은·답장한 사진 > 요청자 프사. "
    "mode: 단색 배경 캐릭터=cutout, 실사·꽉 찬 그림=photo(framing auto). " + COMPOSE,
    {"spec": {"type": "object", "description": "{mode, keying, motion:[…], layers:[{type,…,start,end}], fx(옛), caption{text,palette,anims,position}, "
                                               "framing, margin, radius, seed} 또는 {recipe, seed}"},
     "photo_of": PHOTO_OF,
     "icon": {"type": "boolean", "description": "팩 아이콘(100×100)도 같이 — 팩 만든다고 할 때만"},
     "accept_warnings": {"type": "boolean", "description": "검수 경고를 한 번 고친 뒤에도 남으면 true"},
     "request": {"type": "string", "description": "사용자 요청 원문"},
     "wanted": {"type": "string", "description": "정말 못 하는 연출을 원했을 때 그 말 그대로 (기능 요청 접수)"}},
    ["spec"], t_make_sticker))

hooks.add_group_message_hook(L.on_group_message)   # 우리 스티커에 '좋다/별로' 답장 → 점수
hooks.add_reaction_hook(L.on_reaction)             # 👍❤️🔥 반응 → 점수
hooks.add_tick_hook(L.tick)                        # 90일 안 쓴 학습 레시피 정리 (하루 한 번)
