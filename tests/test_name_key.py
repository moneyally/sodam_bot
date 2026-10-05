"""이름 찾기: 투명 글자·이모지·꾸밈 글꼴 이름 (util.name_key → tools._resolve·addressee): python tests/run_all.py name_key

실제 2026-10-04 베베방 #2347 '춘식팀장님한테 인사드려' → 실제 이름 'ㅤㅤ춘식이'(한글 채움 U+3164 두 개) 라서 못 찾음.
멤버 4,553명 중 167명이 이름에 투명 글자·이모지를 섞음. 꾸밈 글꼴(𝕊𝔼ℂ𝕆ℕ𝔻)도 흔함.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_llm import Room  # noqa: E402
from fakes import fake_user, runner  # noqa: E402

from sodam import tools  # noqa: E402
from sodam.permissions import Role  # noqa: E402
from sodam.tools import ToolCtx  # noqa: E402
from sodam.util import name_key  # noqa: E402

test, run_all = runner()
BOSS = fake_user(5, "방장")
CHUN = fake_user(10, "ㅤㅤ춘식이", "cb4885")
JEONG = fake_user(11, "ㅤㅤㅤㅤ정실장", "rx4885")
JIYOUNG = fake_user(12, "💗지영💗")
SECOND = fake_user(13, "𝕊𝔼ℂ𝕆ℕ𝔻")
ROI = fake_user(14, "ㅣ로이ㅣ")
NEW = fake_user(15, "𝐍𝐞𝐰 𝐰𝐨𝐫𝐥𝐝")


def test_keys():
    assert name_key("ㅤㅤ춘식이") == "춘식이" and name_key("ㅤㅤㅤㅤ정실장") == "정실장"
    assert name_key("💗지영💗") == "지영" and name_key("𝕊𝔼ℂ𝕆ℕ𝔻") == "second" and name_key("𝐍𝐞𝐰 𝐰𝐨𝐫𝐥𝐝") == "newworld"
    assert name_key("ㅣ로이ㅣ") == "로이" and name_key("ㅇㅇ") != "" and name_key("Kim_Lee") == "kimlee"
    assert name_key(None) == "" and name_key("ㅤㅤㅇㅇ") == name_key("ㅇㅇ")   # 낱자모뿐인 이름도 투명 글자는 뺌


@test
async def keys_strip_fillers_emoji_and_fancy_fonts():
    test_keys()


async def room():
    r = Room()
    await r.open(admins=(BOSS.id,), settings={"captcha_enabled": False})
    for u in (BOSS, CHUN, JEONG, JIYOUNG, SECOND, ROI, NEW):
        await r.join(u)
    return r, ToolCtx(r.svc, r.bot, Room.CHAT, BOSS, Role.ADMIN, await r.db.get_settings(Room.CHAT))


@test
async def decorated_names_are_found_for_greeting_and_lookup():
    r, ctx = await room()
    for said, uid in (("춘식팀장님", CHUN.id), ("춘식이", CHUN.id), ("정실장님", JEONG.id), ("지영", JIYOUNG.id),
                      ("지영님", JIYOUNG.id), ("second", SECOND.id), ("SECOND", SECOND.id), ("로이형", ROI.id),
                      ("new대표님", NEW.id),                                         # 꾸밈 글꼴 이름의 일부 (DB LIKE 로는 못 찾음)
                      ("춘식이(10)".replace("(10)", f"({CHUN.id:05d})"), CHUN.id),   # AI 가 단서 모양 '이름(ID)' 그대로
                      (f"ㅤㅤㅤㅤ({JEONG.id:05d})", JEONG.id)):
        row, err = await tools._resolve(ctx, said)
        assert row is not None and row["user_id"] == uid, (said, err)


@test
async def sanction_needs_the_whole_name_but_ignores_decoration():
    r, ctx = await room()
    row, err = await tools._resolve(ctx, "춘식이", for_sanction=True)       # 투명 글자만 다름 → 같은 이름
    assert row is not None and row["user_id"] == CHUN.id, err
    row, err = await tools._resolve(ctx, "춘식", for_sanction=True)         # 일부만 → 제재는 안 됨
    assert row is None and "찾을 수 없어요" in err, err


@test
async def two_people_with_same_core_are_asked_not_guessed():
    r, ctx = await room()
    await r.join(fake_user(20, "✨지영✨"))
    row, err = await tools._resolve(ctx, "지영님")
    assert row is None and "여러 명" in err, err


if __name__ == "__main__":
    asyncio.run(run_all())
