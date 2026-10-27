"""set_setting 동시변경 유실 수정 (2026-10-27 버그 조사): 캐시 복사본을 고쳐 쓰면 두 코루틴이
동시에 다른 키를 바꿀 때 나중 쓰기가 앞 변경을 덮었다 (돈 한도 설정 등 조용히 원복). atomic 안에서
DB 를 다시 읽어 merge 하도록 고침. python tests/run_all.py fix_setting_race"""
import json

from fakes import make_db, runner

test, run_all = runner()
CHAT = -1009999


@test
async def set_setting_rereads_db_not_stale_cache():
    db = await make_db()
    await db.set_setting(CHAT, "aaa", 1)
    await db.get_settings(CHAT)                 # 캐시 데움
    # 다른 코루틴/프로세스가 DB 를 직접 바꾼 상황 흉내 (캐시 우회)
    await db._write("UPDATE chats SET settings=? WHERE chat_id=?",
                    (json.dumps({"aaa": 1, "bbb": 2}), CHAT))
    # 이제 또 다른 키를 set_setting
    await db.set_setting(CHAT, "ccc", 3)
    s = await db.get_settings(CHAT)
    assert s["aaa"] == 1 and s["ccc"] == 3, s
    assert s.get("bbb") == 2, "DB 에 있던 다른 변경(bbb)을 덮지 말아야 — atomic 재읽기"   # 옛 코드면 유실
    await db.close()


if __name__ == "__main__":
    run_all()
