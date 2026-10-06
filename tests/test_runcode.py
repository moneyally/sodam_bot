"""🧪 run_code 도구 (sodam/panels/runcode.py): python tests/run_all.py runcode

진짜 작업실 서버(tests/test_workshop.py 가 띄우는 것)에 요청한다. 권한·한도·방 사본·결과 전송·작업실 꺼짐.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeBot, fake_user, make_db, make_svc, runner  # noqa: E402

import test_workshop as tw  # noqa: E402  (서버 띄우기 재사용)

from sodam import tools  # noqa: E402
from sodam.panels import runcode  # noqa: E402
from sodam.permissions import Role  # noqa: E402
from sodam.workshop import client, snapshot  # noqa: E402

test, run_all = runner()
ROOM, OWNER, ADMIN, MEMBER = -100777, 1, 2, 3


async def _setup():
    client.SOCKET = tw.server()
    snapshot._cache.clear()
    db = await make_db()
    svc = await make_svc(db)
    for u in (fake_user(ADMIN, "관리"), fake_user(MEMBER, "멤버")):
        await db.upsert_user(u)
        await db._write("INSERT INTO members(chat_id, user_id, joined_at, last_seen) VALUES(?,?,?,?)",
                        (ROOM, u.id, 1, int(time.time())))
    await db.log_message(ROOM, MEMBER, 1, "비밀 원문 하나")
    await db.log_message(ROOM, MEMBER, 2, "비밀 원문 둘")
    return svc, FakeBot()


async def run(svc, bot, uid, role, code, chat=ROOM, **settings):
    c = tools.ToolCtx(svc, bot, chat, fake_user(uid, f"사람{uid}"), role, {})
    c.settings = await svc.db.get_settings(chat) | settings
    out = await tools.execute("run_code", json.dumps({"code": code}, ensure_ascii=False), c)
    return out, c


COUNT = "import sqlite3\nc = sqlite3.connect('room.db')\nprint('N=', c.execute('select count(*) from messages').fetchone()[0])\n" \
        "print('T=', [r[0] for r in c.execute('select text from messages')])"


@test
async def admin_reads_room_copy_with_text_and_files_go_to_the_room():
    svc, bot = await _setup()
    out, c = await run(svc, bot, ADMIN, Role.ADMIN, COUNT)
    assert "실행 완료" in out and "N= 2" in out and "비밀 원문 하나" in out, out
    assert c.room_read, "방 사본을 읽은 답변 = 멤버 글을 읽은 것 (카드 없는 쓰기 도구 막힘)"
    code = ("open('차트.png','wb').write(b'\\x89PNG fake')\n"     # 테스트 환경엔 matplotlib 없음 — 확장자로 사진/파일 나눔
            "open('표.csv','w').write('a,b\\n1,2')\nprint('done')")
    out, _ = await run(svc, bot, ADMIN, Role.ADMIN, code)
    assert "차트.png" in out and "표.csv" in out, out
    assert len(bot.named("send_photo")) == 1 and len(bot.named("send_document")) == 1
    assert bot.named("send_document")[0][1] == ROOM


@test
async def src_image_becomes_sticker_source_not_posted():
    """코드로 그린 그림(src_*.png) = 스티커·움프 원본: 방에 안 올리고 ctx.image 로, 방 사본을 읽었어도 make_sticker 는 됨 (2026-10-07)."""
    svc, bot = await _setup()
    code = "open('src_chart.png','wb').write(b'\\x89PNG src')\nopen('보기.png','wb').write(b'\\x89PNG v')\nprint('ok')"
    out, c = await run(svc, bot, ADMIN, Role.ADMIN, code)
    assert c.image is not None and c.image.data == b"\x89PNG src" and "src_chart.png" in out and "원본" in out, out
    assert len(bot.named("send_photo")) == 1, "src_ 그림은 방에 안 올림 (보기.png 만)"
    assert c.room_read
    res = await tools.execute("make_sticker", json.dumps({"spec": {"recipe": "없는레시피"}}), c)
    assert res != tools.ROOM_READ_REFUSED and "spec 오류" in res, res
    res = await tools.execute("change_setting", json.dumps({"key": "ai_enabled", "value": "off"}), c)
    assert res == tools.ROOM_READ_REFUSED or "못" in res, res              # 다른 쓰기 도구는 그대로 막힘


@test
async def members_need_the_room_switch_and_never_see_text():
    svc, bot = await _setup()
    out, _ = await run(svc, bot, MEMBER, Role.MEMBER, "print(1)")
    assert "관리자만" in out and not bot.calls, out
    out, _ = await run(svc, bot, MEMBER, Role.MEMBER, COUNT, run_code_members=True)
    assert "N= 2" in out and "비밀 원문" not in out and "T= [None, None]" in out, out   # 멤버 요청 = 숫자만


@test
async def dm_is_owner_only_and_without_room_data():
    svc, bot = await _setup()
    out, _ = await run(svc, bot, ADMIN, Role.ADMIN, "print(1)", chat=ADMIN)
    assert "1:1" in out and "실행 완료" not in out, out
    out, _ = await run(svc, bot, OWNER, Role.OWNER, "import os\nprint(sorted(os.listdir('.')))", chat=OWNER)
    assert "실행 완료" in out and "room.db" not in out, out


@test
async def daily_cap_and_workshop_down():
    svc, bot = await _setup()
    for _ in range(2):
        out, _ = await run(svc, bot, ADMIN, Role.ADMIN, "print(1)", run_code_daily=2)
        assert "실행 완료" in out
    out, _ = await run(svc, bot, ADMIN, Role.ADMIN, "print(1)", run_code_daily=2)
    assert "한도(2번)" in out, out
    out, _ = await run(svc, bot, ADMIN, Role.ADMIN, "print(1)", run_code_daily=99)
    assert "실행 완료" in out, out                       # 저장값 99 → 상한 30 으로 자름, 지금 2번 썼으니 아직 됨
    client.SOCKET = "/nonexistent/sock"
    out, _ = await run(svc, bot, OWNER, Role.OWNER, "print(1)")
    assert "꺼져 있음" in out, out


CHART = ("import sqlite3\nc = sqlite3.connect('room.db')\nn = c.execute('select count(*) from messages').fetchone()[0]\n"
         "print('오늘 메시지', n)\nopen('chart.png','wb').write(b'\\x89PNG fake')")


@test
async def recipe_previews_before_saving_and_runs_without_ai():
    from sodam import cron
    svc, bot = await _setup()
    svc.perms.admins.add(ADMIN)
    c = tools.ToolCtx(svc, bot, ROOM, fake_user(ADMIN, "관리"), Role.ADMIN, await svc.db.get_settings(ROOM))
    args = {"when": "매일 23:00", "action": "ai", "skill": "code", "text": "1/0"}
    out = await tools.execute("schedule_task", json.dumps(args), c)
    assert "미리 돌려 보니 실패" in out and "ZeroDivisionError" in out and not bot.named("send_message"), out
    args["text"] = CHART
    out = await tools.execute("schedule_task", json.dumps(args), c)
    card = bot.named("send_message")[-1]
    assert "확인 버튼" in out and "미리보기" in card[2] and "오늘 메시지 2" in card[2] and "chart.png" in card[2], card[2]
    assert not bot.named("send_photo"), "미리보기는 방에 파일을 안 올림"
    sid = (await cron.create(svc, [ROOM], uid=ADMIN, when=("daily", "23:00", None, None), action="ai", skill="code",
                             text=CHART, title="밤 차트"))[0]
    row = await svc.db.get_schedule(ROOM, sid)
    svc.llm = None   # 그 시각엔 AI 를 안 씀 (부르면 터짐)
    assert await cron.fire(svc, bot, row) is True
    assert "오늘 메시지 2" in bot.named("send_message")[-1][2] and len(bot.named("send_photo")) == 1
    c2 = tools.ToolCtx(svc, bot, ROOM, fake_user(ADMIN, "관리"), Role.ADMIN, await svc.db.get_settings(ROOM))
    out = await tools.execute("manage_schedule", json.dumps({"target": "schedule", "op": "run", "id": sid}), c2)
    assert "지금 한 번 실행" in out and len(bot.named("send_photo")) == 2, out          # ▶️ 지금 실행
    svc.perms.admins.discard(ADMIN)
    assert await cron.fire(svc, bot, row) is False                                    # 만든 사람이 관리자가 아니면 끄고 안 함
    assert len(bot.named("send_photo")) == 2


@test
def tool_is_registered_with_guide_and_cap():
    t = tools._BY_NAME["run_code"]
    assert "room.db" in t.description and "NanumGothic" in t.description and "pandas.read_sql" in t.description   # duckdb 는 sqlite 확장을 못 받음 (인터넷 없음, 서버 실측)
    assert "run_code" in tools.READ_ONLY
    from sodam.settings import OWNER_CAP, over_cap
    assert OWNER_CAP["run_code_daily"] == 30 and over_cap("run_code_daily", 31)


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
