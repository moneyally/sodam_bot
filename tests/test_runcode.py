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


@test
def tool_is_registered_with_guide_and_cap():
    t = tools._BY_NAME["run_code"]
    assert "room.db" in t.description and "NanumGothic" in t.description
    assert "run_code" in tools.READ_ONLY
    from sodam.settings import OWNER_CAP, over_cap
    assert OWNER_CAP["run_code_daily"] == 30 and over_cap("run_code_daily", 31)


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
