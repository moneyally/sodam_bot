"""🎧 자동선곡(라디오) 2.0 — 방 재생 기록 기반 선곡 테스트."""
import asyncio

from fakes import make_db, runner

from sodam.voice import autodj, musicmatch as mm, musicq  # noqa: F401  (musicq: music_queue 스키마 등록)

test, run_all = runner()
CHAT = -1009

A = "아이유 (IU) - 좋은 날"          # 씨드 (가수 아이유)
B = "뉴진스 (NewJeans) - Hype Boy"  # 다른 가수
C = "아이유 (IU) - 밤편지"           # A 와 같은 가수
D = "방탄소년단 - Dynamite"          # 또 다른 가수
X = "에스파 (aespa) - Next Level"   # 다른 가수 (SR 테스트용)
Y = "세븐틴 (SEVENTEEN) - 손오공"    # 다른 가수, 아주 인기 (pop 테스트용)


def vid_of(title):
    return "v" + str(abs(hash(title)) % 10**9).rjust(10, "0")[:10]


async def seed_room(db, titles, chat=CHAT):
    """titles 순서대로 '튼' 기록을 music_queue 에 넣음 (오래된→최신)."""
    ts = 1000
    for t in titles:
        ts += 10
        await db._write(
            "INSERT INTO music_queue(chat_id,title,url,vid,duration,state,ts,started_ts,auto) "
            "VALUES(?,?,?,?,?,?,?,?,0)",
            (chat, t, f"https://y/{vid_of(t)}", vid_of(t), 180, "done", ts, ts))


async def pick(db, seed_title=A, recent=()):
    return await autodj.pick(db, CHAT, mm.song_key(seed_title), seed_title,
                             {mm.song_key(t) for t in recent})


@test
async def sr_beats_mere_popularity():
    db = await make_db()
    # Y 는 아주 많이 틀렸지만(인기) A 다음에 온 적 없고, X 는 늘 A 다음에 옴 → SR 이 인기를 이겨야 함
    await seed_room(db, [Y, Y, Y, Y, A, X, A, X, Y, Y])
    got = await pick(db, A)
    assert got is not None, "후보가 나와야 함"
    assert mm.song_key(got["title"]) == mm.song_key(X), ("A 다음 온 X 를 SR 로 골라야 함(인기곡 Y 아님)", got["title"])
    assert got["vid"] and got["url"], "재생 정보가 있어야 함"


@test
async def recently_played_is_excluded():
    db = await make_db()
    await seed_room(db, [A, B, A, B, A, B, D])   # A→B 강함, D 는 한 번
    got = await pick(db, A, recent=[B])           # B 를 최근곡으로 제외
    assert got is not None and mm.song_key(got["title"]) != mm.song_key(B), ("B 는 빠져야 함", got and got["title"])


@test
async def same_artist_gets_a_boost_on_a_tie():
    db = await make_db()
    # A 다음에 B(다른가수)가 SR 살짝 높지만, C(같은가수)가 보너스로 역전. 끝은 B 라 '2연속' 아님 → 보너스 ON
    await seed_room(db, [B, A, C, A, B, A, C, A, B])
    got = await pick(db, A)
    assert got is not None and mm.song_key(got["title"]) == mm.song_key(C), ("같은 가수 C 가 보너스로 뽑혀야 함", got and got["title"])


@test
async def two_same_artist_in_a_row_turns_off_the_boost():
    db = await make_db()
    # B·C SR 비슷(B 살짝↑). 끝이 A,C(둘 다 아이유) → 같은 가수 2연속 → 보너스 꺼짐 → B 가 이김
    # (보너스가 안 꺼지면 C 가 역전함 → 가드가 결과를 가름)
    await seed_room(db, [A, C, A, B, A, B, A, C])
    got = await pick(db, A)
    assert got is not None and mm.song_key(got["title"]) == mm.song_key(B), ("2연속이라 보너스 꺼지고 B", got and got["title"])


@test
async def other_rooms_help_a_thin_room():
    db = await make_db()
    await seed_room(db, [Y, Y], chat=CHAT)                 # 이 방은 얇음 (Y 만)
    await seed_room(db, [Y, X, Y, X, Y, X], chat=-7777)    # 다른 방: Y 다음에 X
    got = await pick(db, Y)                                # 씨드 Y
    assert got is not None and mm.song_key(got["title"]) == mm.song_key(X), ("다른 방 기록(Y→X)으로 X 골라야 함", got and got["title"])


@test
async def this_rooms_taste_beats_other_rooms():
    db = await make_db()
    await seed_room(db, [A, B, A, B, A, B], chat=CHAT)     # 이 방: A→B
    await seed_room(db, [A, X, A, X, A, X], chat=-7777)    # 다른 방: A→X
    got = await pick(db, A)                                # 씨드 A
    assert got is not None and mm.song_key(got["title"]) == mm.song_key(B), ("이 방 취향(B)이 다른 방(X)보다 우선", got and got["title"])


@test
async def thin_history_returns_none():
    db = await make_db()
    await seed_room(db, [A, B])                   # MIN_HISTORY 미만
    assert await pick(db, A) is None, "기록 너무 적으면 자동선곡 안 함 (유튜브 폴백)"


@test
async def no_candidates_returns_none():
    db = await make_db()
    await seed_room(db, [A, A, A, A])             # 씨드 말고 다른 곡이 없음
    assert await pick(db, A) is None, "다음 곡 후보가 없으면 None"


if __name__ == "__main__":
    asyncio.run(run_all())
