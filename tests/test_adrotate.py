"""📢 채널 광고 자동순환 — python tests/run_all.py adrotate

연결된 채널 글을 방에 간격마다 forward 로 순환. 순수함수 pick·틱·upsert·패널·AI 도구 + 뮤테이션."""
from types import SimpleNamespace

from fake_llm import Room
from fakes import fake_user, runner

import sodam.panels.adrotate  # noqa: F401  (schema·tool 등록)
from sodam import adrotate
from sodam.menu import PanelCtx
from sodam.panels import adrotate as panel
from sodam.permissions import Role
from sodam.tools import ToolCtx

test, run_all = runner()
ADMIN = fake_user(1, "관리자")
CH = -100999
ROOM = Room.CHAT


async def world(posts=(11, 12, 13), link=True):
    r = await Room().open(admins={1})
    r.svc.billing = SimpleNamespace(enabled=False)   # covered() → True (이용 기간 통과)
    if link:
        await r.db._write("INSERT INTO channels(chat_id,title,active,room_id) VALUES(?,?,1,?)",
                          (CH, "공지채널", ROOM))
        for m in posts:
            await r.db._write("INSERT INTO channel_msgs(chat_id,msg_id,ts,text) VALUES(?,?,?,?)",
                              (CH, m, 1000 + m, f"광고{m}"))
    return r


def fwd(bot):
    return [c for c in bot.calls if c[0] == "forward_message"]


# ── 순수함수 pick ────────────────────────────────────────
@test
async def pick_seq_starts_and_advances_and_wraps():
    ads = [11, 12, 13]
    assert adrotate.pick(ads, None, 1, "seq") == [11]     # 처음 = 첫 글
    assert adrotate.pick(ads, 11, 1, "seq") == [12]       # 지난 11 → 다음 12
    assert adrotate.pick(ads, 13, 1, "seq") == [11]       # 끝 → 처음으로 순환
    assert adrotate.pick(ads, 999, 1, "seq") == [11]      # 모르는(지워진) 지난 글 → 처음부터


@test
async def pick_per_tick_multiple():
    assert adrotate.pick([11, 12, 13], 11, 2, "seq") == [12, 13]
    assert adrotate.pick([11, 12, 13], 12, 2, "seq") == [13, 11]   # 넘어가며 순환
    assert adrotate.pick([11, 12], 11, 9, "seq") == [12, 11]       # per 가 개수보다 커도 글 수까지


@test
async def pick_random_excludes_last_and_count():
    import random
    random.seed(3)
    for _ in range(20):
        got = adrotate.pick([11, 12, 13], 12, 1, "random")
        assert len(got) == 1 and got[0] != 12, got        # 지난 것 안 뽑음
    assert adrotate.pick([11], 11, 1, "random") == [11]   # 하나뿐이면 어쩔 수 없이 그것


@test
async def pick_empty():
    assert adrotate.pick([], None, 1, "seq") == []
    assert adrotate.pick([], 5, 2, "random") == []


# ── 저장(upsert) ─────────────────────────────────────────
@test
async def save_upsert_creates_then_updates():
    r = await world()
    assert await adrotate.get(r.svc, ROOM) is None
    await adrotate.save(r.svc, ROOM, enabled=1, interval_min=45)
    row = await adrotate.get(r.svc, ROOM)
    assert row["enabled"] == 1 and row["interval_min"] == 45 and row["per_tick"] == 1
    await adrotate.save(r.svc, ROOM, interval_min=30)       # 한 칸만 바꿔도 나머지 유지
    row = await adrotate.get(r.svc, ROOM)
    assert row["interval_min"] == 30 and row["enabled"] == 1


# ── 틱 ───────────────────────────────────────────────────
@test
async def tick_forwards_due_room_and_advances():
    r = await world()
    await adrotate.save(r.svc, ROOM, enabled=1, interval_min=30)   # last_sent=0 → 바로 때가 됨
    await adrotate.tick(r.svc, r.bot)
    sent = fwd(r.bot)
    assert len(sent) == 1 and sent[0][2] == CH and sent[0][3] == 11, sent   # 채널에서 첫 글 forward
    row = await adrotate.get(r.svc, ROOM)
    assert row["last_ad"] == 11 and row["last_sent"] > 0
    # 아직 간격 안 참 → 두 번째 틱은 아무것도 안 보냄
    await adrotate.tick(r.svc, r.bot)
    assert len(fwd(r.bot)) == 1
    # 때 지난 걸로 만들고 다시 → 다음 글(12)
    await adrotate.save(r.svc, ROOM, last_sent=0)
    await adrotate.tick(r.svc, r.bot)
    sent = fwd(r.bot)
    assert len(sent) == 2 and sent[1][3] == 12, sent
    assert (await adrotate.get(r.svc, ROOM))["last_ad"] == 12


@test
async def tick_not_due_does_nothing():
    r = await world()
    import time
    await adrotate.save(r.svc, ROOM, enabled=1, interval_min=30, last_sent=int(time.time()))
    await adrotate.tick(r.svc, r.bot)
    assert fwd(r.bot) == []          # 방금 보냈으니 30분 안 지남


@test
async def tick_disabled_room_skipped():
    r = await world()
    await adrotate.save(r.svc, ROOM, enabled=0, interval_min=30)
    await adrotate.tick(r.svc, r.bot)
    assert fwd(r.bot) == []


@test
async def tick_no_linked_channel_skipped():
    r = await world(link=False)
    await adrotate.save(r.svc, ROOM, enabled=1)
    await adrotate.tick(r.svc, r.bot)
    assert fwd(r.bot) == []


@test
async def tick_del_prev_deletes_then_forwards():
    r = await world()
    import json
    await adrotate.save(r.svc, ROOM, enabled=1, del_prev=1, last_msgs=json.dumps([555, 556]))
    await adrotate.tick(r.svc, r.bot)
    dels = [c for c in r.bot.calls if c[0] == "delete"]
    assert ("delete", ROOM, 555) in dels and ("delete", ROOM, 556) in dels, dels
    assert len(fwd(r.bot)) == 1


@test
async def tick_per_tick_sends_multiple():
    r = await world()
    await adrotate.save(r.svc, ROOM, enabled=1, per_tick=2)
    await adrotate.tick(r.svc, r.bot)
    sent = [c[3] for c in fwd(r.bot)]
    assert sent == [11, 12], sent
    assert (await adrotate.get(r.svc, ROOM))["last_ad"] == 12


@test
async def tick_no_ads_waits():
    r = await world(posts=())
    await adrotate.save(r.svc, ROOM, enabled=1)
    await adrotate.tick(r.svc, r.bot)
    assert fwd(r.bot) == []
    assert (await adrotate.get(r.svc, ROOM))["last_sent"] > 0   # 쉬도록 last_sent 찍음


# ── 패널 ─────────────────────────────────────────────────
def pctx(r, *args):
    return PanelCtx(r.svc, r.bot, ADMIN.id, ROOM, list(args))


@test
async def panel_toggle_needs_channel():
    r = await world(link=False)
    await panel.r_toggle(pctx(r, "1"))
    row = await adrotate.get(r.svc, ROOM)
    assert row is None or row["enabled"] == 0          # 연결 채널 없으면 안 켜짐


@test
async def panel_sets_values():
    r = await world()
    await panel.r_toggle(pctx(r, "1"))
    assert (await adrotate.get(r.svc, ROOM))["enabled"] == 1
    await panel.r_interval(pctx(r, "60"))
    await panel.r_per(pctx(r, "3"))
    await panel.r_order(pctx(r, "random"))
    await panel.r_delprev(pctx(r, "1"))
    row = await adrotate.get(r.svc, ROOM)
    assert row["interval_min"] == 60 and row["per_tick"] == 3
    assert row["order_mode"] == "random" and row["del_prev"] == 1


@test
async def panel_interval_clamped():
    r = await world()
    await panel.r_interval(pctx(r, "99999"))
    assert (await adrotate.get(r.svc, ROOM))["interval_min"] == adrotate.MAX_MIN
    await panel.r_interval(pctx(r, "1"))
    assert (await adrotate.get(r.svc, ROOM))["interval_min"] == adrotate.MIN_MIN


@test
async def panel_preview_forwards_without_advancing():
    r = await world()
    scr = await panel.r_preview(pctx(r))
    assert len(fwd(r.bot)) == 1 and fwd(r.bot)[0][3] == 11
    assert "보냈" in (scr.toast or "")
    # 상태 안 바뀜 → 미리보기가 순환을 건너뛰지 않음
    row = await adrotate.get(r.svc, ROOM)
    assert row is None or row["last_ad"] is None


# ── AI 도구 ──────────────────────────────────────────────
def tctx(r):
    return ToolCtx(r.svc, r.bot, ROOM, ADMIN, Role.ADMIN, {})


@test
async def tool_on_requires_channel():
    r = await world(link=False)
    out = await panel.t_ad_rotate(tctx(r), {"action": "on"})
    assert "연결" in out
    assert await adrotate.get(r.svc, ROOM) is None


@test
async def tool_on_enables_with_params():
    r = await world()
    out = await panel.t_ad_rotate(tctx(r), {"action": "on", "interval_min": 60, "per_tick": 2, "order": "random"})
    assert "켰" in out
    row = await adrotate.get(r.svc, ROOM)
    assert row["enabled"] == 1 and row["interval_min"] == 60 and row["per_tick"] == 2 and row["order_mode"] == "random"


@test
async def tool_off_and_status():
    r = await world()
    await panel.t_ad_rotate(tctx(r), {"action": "on"})
    off = await panel.t_ad_rotate(tctx(r), {"action": "off"})
    assert "껐" in off and (await adrotate.get(r.svc, ROOM))["enabled"] == 0
    st = await panel.t_ad_rotate(tctx(r), {"action": "status"})
    assert "꺼져" in st


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_all())
