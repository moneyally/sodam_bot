"""🎚 게임 난이도 (2026-10-17 오너 '공정/60/70/80 을 메뉴·버튼으로, 지금은 모든 방 80 유지').

- 오너 전체 기본: 메인 [🎚 게임 난이도 (전체)] m:gdo → m:gdo:<0|60|70|80> = chat_state(0, casino_lose_pct). 방 설정이 '오너 기본'인 방 전부에 적용.
- 방마다: 그룹 허브 [🎚 게임 난이도] m:gdr:<방> → 방 설정 casino_difficulty 프리셋 (오너 기본 / 공정 / 60 / 70 / 80).
- 숨기지 않음: 손님은 `!난이도`·`!도움` 에서 이 방 난이도를 봄 (sodam/casino/core.c_difficulty). 배당표는 그대로.
"""
from __future__ import annotations

from .. import menu
from ..casino import core
from ..menu import ADMIN, OWNER, B, PanelCtx, Route, Screen

LEVELS = ("0", "60", "70", "80")
menu.register_preset("casino_difficulty", [("owner", "오너 기본"), ("0", "공정"), ("60", "60%"), ("70", "70%"), ("80", "80%")],
                     "gdr")


def _name(v: str) -> str:
    return "공정" if v == "0" else f"{v}%"


async def s_room(c: PanelCtx) -> Screen:
    s = await c.svc.db.get_settings(c.cid)
    g = await core.global_lose_pct(c.svc.db)
    now = await core.lose_pct(c.svc.db, c.cid)
    own = str(s["casino_difficulty"]) == "owner"
    lines = ["🎚 <b>게임 난이도</b>",
             "손님이 질 확률을 정해요. 이길 때 받는 배당은 그대로고, 이기는 판만 그만큼 줄어요.",
             "",
             f"지금 이 방: <b>{core.difficulty_label(now)}</b>" + (f" (오너 기본 {_name(str(g))} 따름)" if own else ""),
             "",
             "• 공정 = 원래 확률 · 60/70/80% = 손님이 그만큼 짐",
             "• 룰렛·사다리·바카라(혼자)·블랙잭·하이로우·그래프에 적용 (주사위·슬롯·경마·회차 바카라는 그대로)",
             "• 손님은 <code>!난이도</code> 로 이 방 난이도를 볼 수 있어요"]
    rows = [menu._preset_row(s, c.cid, "casino_difficulty"), menu._back(c.cid)]
    return Screen("\n".join(lines), menu._kb(rows))


async def s_owner(c: PanelCtx) -> Screen:
    db, v = c.svc.db, c.arg(0)
    if v in LEVELS:
        await db.set_state(0, core.LOSE_KEY, int(v))
        core._lose["at"] = -1e9                      # 30초 기억을 바로 비움 → 다음 판부터
        await db.log_mod(0, c.uid, None, "setting", f"casino_lose_pct={v}")
    g = await core.global_lose_pct(db)
    rows = await db._all("SELECT chat_id, settings FROM chats WHERE settings LIKE '%casino_difficulty%'")
    import json
    own = {}
    for r in rows:
        try:
            val = str(json.loads(r["settings"] or "{}").get("casino_difficulty", "owner"))
        except ValueError:
            continue
        if val != "owner":
            own[val] = own.get(val, 0) + 1
    lines = ["🎚 <b>게임 난이도 — 전체 기본</b>",
             f"지금: <b>{core.difficulty_label(g)}</b>",
             "방 설정이 '오너 기본'인 방은 전부 이 값을 따라요.",
             "방마다 따로 정한 방: " + (", ".join(f"{_name(k)} {n}곳" for k, n in sorted(own.items())) if own else "없음"),
             "",
             "손님은 <code>!난이도</code> 로 자기 방 난이도를 볼 수 있어요."]
    btns = [B(("● " if str(g) == lv else "") + _name(lv), f"m:gdo:{lv}") for lv in LEVELS]
    return Screen("\n".join(lines), menu._kb([btns, [B("⬅️ 처음으로", "m:home")]]),
                  toast=f"전체 기본을 {_name(v)} 로 바꿨어요" if v in LEVELS else None)


menu.register_screen("gdr", s_room, need=ADMIN)
menu.register_hub(menu.HubItem(36, "gdr", lambda s: "🎚 게임 난이도" + ("" if str(s.get("casino_difficulty", "owner")) == "owner"
                                                                    else f" ({_name(str(s['casino_difficulty']))})")))
menu.register_route("gdo", Route(s_owner, OWNER, scoped=False))
menu.register_main(92, "gdo", "🎚 게임 난이도 (전체)", need=OWNER)
