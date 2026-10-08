"""🎵 뮤직봇 2차 기능 (2026-10-08 오너 요청): python tests/run_all.py music_extras

섞기·대기열 비우기·대기열 전체 반복·자동 재생(믹스, 연속 상한)·음성채팅 제목·재생목록 링크·가사 기록(소담만 전문)·방 인기곡·
말로 전부(AI 도구 music). 자동 재생·재생목록·가사는 서버에서 실제로 돌려 보고 모양을 맞춤 (믹스는 곡 링크+list=RD 로만 열림 등).
"""
import asyncio
import json
import random
import tempfile
import time
from types import SimpleNamespace

from fakes import FakeQuery, make_db, runner
from test_music import (ADMIN, CHAT, MEMBER, OTHER, FakeSource, cmd, fast_player, make_worker, ready, run_job,
                        stop_all, world)

from sodam import tools
from sodam.menu import PanelCtx
from sodam.panels import music as M
from sodam.permissions import Role
from sodam.voice import music, musicmatch as mm, musicq

test, run_all = runner()


async def add(db, n, *, by=MEMBER.id, auto=False, title=None, vid=None):
    return (await musicq.add(db, CHAT, title=title or f"곡{n}", url="u", vid=vid or f"vid{n:08d}", duration=100,
                             by_id=None if auto else by, by_name="멤버", auto=auto, per_user=0))[0]


async def order(db):
    return [r["title"] for r in await musicq.waiting(db, CHAT)]


@test
async def shuffle_reorders_waiting_only_and_new_songs_go_last():
    db = await make_db()
    for i in range(1, 6):
        await add(db, i)
    cur = await musicq.start_next(db, CHAT)
    assert cur["title"] == "곡1"
    assert await musicq.shuffle(db, CHAT, random.Random(3)) == 4
    after = await order(db)
    assert sorted(after) == ["곡2", "곡3", "곡4", "곡5"] and after != ["곡2", "곡3", "곡4", "곡5"], after
    await add(db, 6)
    assert (await order(db))[-1] == "곡6", "섞은 뒤 새 신청은 맨 뒤"
    assert (await musicq.current(db, CHAT))["title"] == "곡1", "지금 곡은 그대로"
    nxt_title = after[0]
    await musicq.finish(db, cur["id"])
    assert (await musicq.start_next(db, CHAT))["title"] == nxt_title, "다음 곡도 섞인 순서"
    row, why = await musicq.remove_nth(db, CHAT, 1, 0, True)
    assert why == "ok" and row["title"] == after[1], "빼기 번호도 섞인 순서"
    assert await musicq.clear_waiting(db, CHAT) == 3 and (await musicq.current(db, CHAT))["title"] == nxt_title


@test
async def loop_queue_puts_finished_song_back_at_the_end():
    db = await make_db()
    await add(db, 1)
    await add(db, 2)
    await musicq.set_mode(db, CHAT, "loopq", True)
    pl, said = await fast_player(db)
    task = asyncio.create_task(pl.run())
    for _ in range(300):
        if said.count("now") >= 3:
            break
        await asyncio.sleep(0.01)
    pl.stop("end")
    await task
    titles = [r["title"] for r in await db._all("SELECT title FROM music_queue ORDER BY id")]
    assert said.count("now") >= 3 and titles[:3] == ["곡1", "곡2", "곡1"], (said, titles)
    await musicq.set_mode(db, CHAT, "loopq", False)
    db2 = await make_db()
    await add(db2, 1)
    pl, said = await fast_player(db2, idle_sec=1)
    await asyncio.wait_for(pl.run(), 5)
    assert said.count("now") == 1, "끄면 한 번만"


class AutoSource(FakeSource):
    def __init__(self, d, picks=None):
        super().__init__(d)
        self.picks = list(picks or [])
        self.seeds = []

    def related(self, seed, seen_v, seen_t):
        self.seeds.append(seed)
        for p in self.picks:
            if p["vid"] not in seen_v and mm.song_key(p["title"]) not in seen_t:
                return p
        return None


def pick(n, title):
    return {"title": title, "url": "u", "vid": f"auto{n:07d}", "duration": 100}


@test
async def autoplay_adds_a_similar_song_when_the_queue_runs_out():
    db = await make_db()
    await add(db, 1, vid="seedAAAAAAA", title="아이유(IU) - 밤편지")
    src = AutoSource(tempfile.mkdtemp(), [pick(1, "아이유(IU) - 밤편지 [가사]"), pick(2, "AKMU - 오랜 날 오랜 밤")])
    await musicq.set_mode(db, CHAT, "autoplay", True)
    pl, said = await fast_player(db, src=src, idle_sec=1)
    await asyncio.wait_for(pl.run(), 5)
    rows = [dict(r) for r in await db._all("SELECT title, auto, by_name FROM music_queue ORDER BY id")]
    assert src.seeds and src.seeds[0] == "seedAAAAAAA"
    assert [r["title"] for r in rows] == ["아이유(IU) - 밤편지", "AKMU - 오랜 날 오랜 밤"], ("같은 곡(다른 영상)은 건너뜀", rows)
    assert rows[1]["auto"] == 1 and "자동" in rows[1]["by_name"] and said.count("now") == 2
    db2 = await make_db()
    await add(db2, 1)
    pl, said = await fast_player(db2, src=AutoSource(tempfile.mkdtemp(), [pick(1, "다른 곡")]), idle_sec=1)
    await asyncio.wait_for(pl.run(), 5)
    assert said.count("now") == 1, "꺼져 있으면 자동 재생 없음"


@test
async def autoplay_stops_after_max_songs_without_a_person_and_uses_original_id():
    db = await make_db()
    await add(db, 0, vid="humanAAAAAA")
    for i in range(musicq.AUTO_MAX):
        rid = await add(db, i + 1, auto=True)
        await musicq.finish(db, rid, "done")
    assert await musicq.auto_streak(db, CHAT) == musicq.AUTO_MAX
    src = AutoSource(tempfile.mkdtemp(), [pick(99, "새 곡")])
    await musicq.set_mode(db, CHAT, "autoplay", True)
    pl, _ = await fast_player(db, src=src)
    assert not await pl._autoplay() and not src.seeds, "연속 상한이면 묻지도 않음"
    await add(db, 50, title="사람 신청")
    assert await musicq.auto_streak(db, CHAT) == 0, "사람이 신청하면 다시 0"
    db2 = await make_db()
    rid = await add(db2, 1, vid="ytOrigAAAAA")
    await musicq.set_track(db2, rid, "대체", "u", "sc123", 100)
    src2 = AutoSource(tempfile.mkdtemp(), [])
    await musicq.set_mode(db2, CHAT, "autoplay", True)
    pl2, _ = await fast_player(db2, src=src2)
    await pl2._autoplay()
    assert src2.seeds == ["ytOrigAAAAA"], "대체 음원으로 바뀐 곡은 원래 ID 로 비슷한 곡 찾기"


class LyricsSource(FakeSource):
    def lyrics(self, title, duration=0):
        return {"track": "밤편지", "artist": "아이유", "plain": "\n".join(f"{i}번째 줄" for i in range(1, 21))}


@test
async def lyrics_are_recorded_for_sodam_and_the_room_sees_only_a_part():
    db, svc, bot = await world()
    pl, said = await fast_player(db, src=LyricsSource(tempfile.mkdtemp()), idle_sec=1)
    await add(db, 1, vid="lyricAAAAAA", title="아이유 - 밤편지")
    task = asyncio.create_task(pl.run())
    got = None
    for _ in range(300):
        got = await musicq.lyrics(db, "lyricAAAAAA")
        if got:
            break
        await asyncio.sleep(0.01)
    assert got and got["plain"].count("\n") == 19 and got["artist"] == "아이유", "가사 전문 기록"
    part = await M.lyrics_text(svc, CHAT)
    assert "1번째 줄" in part and f"{M.LYRICS_LINES}번째 줄" in part and f"{M.LYRICS_LINES + 1}번째 줄" not in part, part
    ctx = SimpleNamespace(svc=svc, bot=bot, chat_id=CHAT, caller=MEMBER, role=Role.MEMBER, tainted=False, quiet=False)
    full = await M.t_music(ctx, {"action": "lyrics"})
    assert "20번째 줄" in full and "통째로" in full and ctx.tainted, "소담(AI)은 전문을 읽되 바깥 자료로 표시"
    pl.stop("end")
    await task


@test
def lyrics_match_needs_the_same_song_and_length():
    src = music.Source(tempfile.mkdtemp())
    items = [{"artistName": "아이유", "trackName": "좋은 날", "duration": 254, "plainLyrics": "다른 노래"},
             {"artistName": "아이유", "trackName": "밤편지", "duration": 300, "plainLyrics": "길이 다름"},
             {"artistName": "아이유", "trackName": "밤편지", "duration": 253, "plainLyrics": "이 밤 그날의 반딧불을"},
             {"artistName": "IU", "trackName": "밤편지 (Inst.)", "duration": 254, "instrumental": True, "plainLyrics": ""}]
    import urllib.request
    real = urllib.request.urlopen

    class R:
        def __init__(self, data):
            self.data = data

        def read(self, n=-1):
            return self.data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    urllib.request.urlopen = lambda req, timeout=0: R(json.dumps(items).encode())
    try:
        got = src.lyrics("아이유(IU) - 밤편지 [가사/Lyrics]", 254)
    finally:
        urllib.request.urlopen = real
    assert got and got["plain"] == "이 밤 그날의 반딧불을", got


@test
def playlist_links_are_told_apart_from_song_links():
    assert music.playlist_id("https://www.youtube.com/playlist?list=PL4fGSI1pDJn6jXS_Tv_N9B8Z0HTRVJE0m") == \
        "PL4fGSI1pDJn6jXS_Tv_N9B8Z0HTRVJE0m"
    assert music.playlist_id("https://youtube.com/watch?v=dQw4w9WgXcQ&list=PL4fGSI1pDJn6jXS") is None, "곡+목록 공유 링크 = 그 곡만"
    assert music.playlist_id("아이유 밤편지") is None


class ListSource(FakeSource):
    def playlist(self, list_id, limit=music.PLAYLIST_MAX):
        return [{"title": f"목록곡{i}", "url": "u", "vid": f"list{i:07d}", "duration": 120} for i in range(limit)]


@test
async def playlist_link_adds_up_to_the_limit_and_skips_songs_already_there():
    from sodam.voice import worker as W
    db, w, _, _ = await make_worker()
    w.music_source = ListSource(tempfile.mkdtemp())
    await musicq.add(db, CHAT, title="목록곡0", url="u", vid="list0000000", duration=120, by_id=9, by_name="x")
    row = await run_job(db, w, "music_play", {"query": "https://www.youtube.com/playlist?list=PL4fGSI1pDJn6jXS_Tv",
                                              "by": 5, "by_name": "멤버", "status_msg": 3}, CHAT)
    assert row["result"] == "playlist", row
    rows = await db._all("SELECT vid FROM music_queue WHERE by_id=5")
    assert len(rows) == music.PLAYLIST_MAX - 1, "이미 있는 곡 빼고, 한 사람 5곡 한도 대신 목록 한도"
    edit = [c for c in w.bot.named("edit_text") if c[1] == CHAT][-1][2]
    assert "14곡" in edit and "1개 뺌" in edit, edit
    assert W.music.PLAYLIST_MAX == 15
    await stop_all(w)


class TitleClient:
    def __init__(self):
        self.titles = []

    async def get_input_entity(self, chat_id):
        return chat_id

    async def __call__(self, req):
        name = type(req).__name__
        if name == "GetFullChannelRequest":
            return SimpleNamespace(full_chat=SimpleNamespace(call="CALL"))
        if name == "GetGroupCallRequest":
            return SimpleNamespace(call=SimpleNamespace(title="원래 제목"))
        if name == "EditGroupCallTitleRequest":
            self.titles.append(req.title)


@test
async def voice_chat_title_follows_the_song_and_goes_back_after():
    db, w, _, _ = await make_worker()
    real = w.client
    w.client = TitleClient()
    await w._vc_title(CHAT, "아이유(IU) - 밤편지 " + "가" * 80)
    await w._vc_title(CHAT, "두 번째 곡")
    await w._vc_title(CHAT, None)
    t = w.client.titles
    assert t[0].startswith("🎵 아이유") and len(t[0]) <= 64 and t[1] == "🎵 두 번째 곡" and t[2] == "원래 제목", t
    await musicq.set_mode(db, CHAT, "vc_title", False)
    w.client.titles.clear()
    await w._vc_title(CHAT, "세 번째")
    assert not w.client.titles, "방에서 끄면 안 바꿈"
    w.client = real


@test
async def top_songs_count_people_requests_not_autoplay():
    db, svc, bot = await world()
    for vid, n in (("hitAAAAAAAA", 3), ("twoAAAAAAAA", 2)):
        for _ in range(n):
            rid = await add(db, 0, vid=vid, title=f"제목 {vid[:3]}")
            await db._write("UPDATE music_queue SET started_ts=?, state='done' WHERE id=?", (int(time.time()), rid))
    for _ in range(5):
        rid = await add(db, 0, vid="autoAAAAAAA", auto=True, title="자동곡")
        await db._write("UPDATE music_queue SET started_ts=?, state='done' WHERE id=?", (int(time.time()), rid))
    rid = await add(db, 0, vid="sc999", title="대체로 튼 hit")
    await db._write("UPDATE music_queue SET started_ts=?, state='done', orig_vid='hitAAAAAAAA' WHERE id=?", (int(time.time()), rid))
    text = await M.top_text(svc, CHAT, 30)
    assert "1. 대체로 튼 hit — 4번" in text and "자동곡" not in text and "2. 제목 two — 2번" in text, text


@test
async def commands_and_buttons_change_room_modes_with_the_right_people():
    db, svc, bot = await world()
    msg = await cmd(svc, bot, ".자동재생 켜기")
    assert "관리자만" in msg.replies[0] and not (await musicq.modes(db, CHAT))["autoplay"]
    msg = await cmd(svc, bot, ".자동재생 켜기", ADMIN, Role.ADMIN)
    assert "켰어요" in msg.replies[0] and (await musicq.modes(db, CHAT))["autoplay"]
    msg = await cmd(svc, bot, ".자동재생 아마", ADMIN, Role.ADMIN)
    assert "켜기/끄기" in msg.replies[0]
    for i in range(3):
        await add(db, i, by=OTHER.id)
    await musicq.start_next(db, CHAT)
    msg = await cmd(svc, bot, ".섞기")
    assert "신청한 분만" in msg.replies[0], "남이 신청한 곡 중이면 일반 멤버는 못 섞음"
    msg = await cmd(svc, bot, ".대기열비우기")
    assert "관리자만" in msg.replies[0] and len(await musicq.waiting(db, CHAT)) == 2
    msg = await cmd(svc, bot, ".대기열비우기", ADMIN, Role.ADMIN)
    assert "2곡" in msg.replies[0] and not await musicq.waiting(db, CHAT)
    c = PanelCtx(svc, bot, ADMIN.id, CHAT, ["vc_title"])
    screen = await M.r_mode(c)
    assert not (await musicq.modes(db, CHAT))["vc_title"] and "껐어요" in screen.toast
    assert "자동 재생" in screen.text and "musm" in str(screen.kb.inline_keyboard)


@test
async def ai_tool_does_everything_by_words():
    db, svc, bot = await world()
    await ready(db)
    ctx = SimpleNamespace(svc=svc, bot=bot, chat_id=CHAT, caller=ADMIN, role=Role.ADMIN, tainted=False, quiet=False)
    for i in range(4):
        await add(db, i)
    assert "섞었" in await M.t_music(ctx, {"action": "shuffle"})
    assert "켰어요" in await M.t_music(ctx, {"action": "autoplay", "on": True}) and (await musicq.modes(db, CHAT))["autoplay"]
    assert "껐어요" in await M.t_music(ctx, {"action": "loop_queue", "on": False})
    assert "뺌" in await M.t_music(ctx, {"action": "remove", "value": 1})
    assert "뺐어요" in await M.t_music(ctx, {"action": "clear"}) and not await musicq.waiting(db, CHAT)
    assert "인기곡" in await M.t_music(ctx, {"action": "top"}) or "튼 노래가 없어요" in await M.t_music(ctx, {"action": "top"})
    desc = tools._BY_NAME["music"].description
    assert "전체 꺼줘" in desc and "clear" in desc and "autoplay" in desc and "lyrics" in desc
    enum = tools._BY_NAME["music"].params["action"]["enum"]
    assert {"shuffle", "clear", "loop_queue", "autoplay", "lyrics", "top", "remove", "seek"} <= set(enum)
    q = FakeQuery(CHAT, MEMBER, "mu:queue")
    await M.on_button(svc, bot, q, ["queue"])
    assert "자동 재생" in q.answers[0][0], "대기열에 켜진 모드 표시"


@test
def mood_requests_pick_songs_from_playlists_skipping_long_mixes_and_other_artists():
    """실제 사례 2026-10-08 자이 '소담아 잔잔한 플리 하나 틀어줘' → '노래 모음은 못 틀어요'. 서버 실측: 첫 재생목록이 1시간 모음 영상뿐."""
    src = music.Source(tempfile.mkdtemp())
    lists = {
        "search": [{"id": "PLmix"}, {"id": "PLgood"}, {"id": "PLmore"}],
        "PLmix": [{"id": f"mixAAAAAA{i:02d}", "title": "발라드 노래모음 1시간", "duration": 5555} for i in range(5)],
        "PLgood": [{"id": "goodAAAAA01", "title": "최유리 - 숲", "duration": 229},
                   {"id": "goodAAAAA02", "title": "최유리 - 숲 [가사]", "duration": 230},
                   {"id": "goodAAAAA03", "title": "숲 (Live) - 최유리", "duration": 240},
                   {"id": "goodAAAAA04", "title": "아이유 - 밤편지", "duration": 254},
                   {"id": "goodAAAAA05", "title": "최유리 - 잘 지내자, 우리", "duration": 257}],
        "PLmore": [{"id": f"moreAAAAA{i:02d}", "title": f"최유리 - 노래{i}", "duration": 200} for i in range(5)]
                  + [{"id": "moreAAAAA99", "title": "잔잔한 밤 - 다른가수", "duration": 200}],   # 분위기 말이 제목에 한 번 → 가수로 보면 안 됨
    }
    seen_urls = []

    def entries(url):
        seen_urls.append(url)
        if "results?search_query=" in url:
            return lists["search"]
        return lists.get(url.rsplit("=", 1)[-1], [])
    src._entries = entries
    got = src.mix_for("최유리 노래모음")
    titles = [g["title"] for g in got]
    assert titles[:2] == ["최유리 - 숲", "최유리 - 잘 지내자, 우리"], ("모음 영상·같은 곡·라이브·다른 가수 빼고", titles)
    assert len(got) >= music.MIX_MIN and all("1시간" not in t for t in titles)
    import urllib.parse
    assert urllib.parse.unquote(seen_urls[0]).split("search_query=")[1].startswith("최유리 노래&"), seen_urls[0]
    seen_urls.clear()
    got = src.mix_for("소담아 잔잔한 플리 하나 틀어줘")
    q = urllib.parse.unquote(seen_urls[0]).split("search_query=")[1]
    assert q.startswith("잔잔한 노래&"), ("'소담아·하나·틀어줘' 는 검색어에서 뺌", q)
    assert any("밤편지" in g["title"] for g in got), "분위기 말('잔잔한')은 가수로 보지 않음 (실측: 가수 필터에 걸려 0곡)"
    src._entries = lambda url: [] if "results" in url else []
    try:
        src.mix_for("잔잔한 노래")
        raise AssertionError("못 찾으면 안내")
    except music.MusicError as e:
        assert e.code == "not_found" and "분위기" in str(e)


@test
def mood_skips_compilation_titles_and_fills_from_mix_and_scans_far():
    """서버 실측: '☕ 하루종일 듣기 좋은 카페음악' 한 개만 나옴 · 앞 8개 목록이 전부 1시간 모음 영상이라 0곡."""
    src = music.Source(tempfile.mkdtemp())
    search = [{"id": f"PLjunk{i}"} for i in range(9)] + [{"id": "PLone"}]
    lists = {f"PLjunk{i}": [{"id": f"junkAAAA{i:03d}", "title": "모음", "duration": 5000}] for i in range(9)}
    lists["PLone"] = [{"id": "cafeAAAAA01", "title": "☕ 하루종일 듣기 좋은 카페음악", "duration": 300},
                      {"id": "seedAAAAA01", "title": "아이유 - 밤편지", "duration": 254}]
    mix = [{"id": "seedAAAAA01", "title": "아이유 - 밤편지", "duration": 254}] + \
          [{"id": f"mixxAAAAA{i:02d}", "title": f"가수{i} - 노래{i}", "duration": 200} for i in range(6)]

    calls = []

    def entries(url):
        if "results?search_query=" in url:
            calls.append(url)
            return search[:8] if len(calls) == 1 else search[2:]   # 두 번째 검색어에서 새 목록이 나옴
        if "list=RD" in url:
            return mix
        return lists[url.rsplit("=", 1)[-1]]
    src._entries = entries
    got = [g["title"] for g in src.mix_for("잔잔한 플리")]
    assert not any("하루종일" in t for t in got), got
    assert got[0] == "아이유 - 밤편지" and len(got) >= music.MIX_MIN, ("몇 곡뿐이면 믹스로 채움", got)
    assert len(got) == len(set(got)), "같은 곡 두 번 X"


class MixSource(FakeSource):
    def resolve(self, query):
        if "플리" in query:
            raise music.MusicError("mix", "분위기 신청")
        return super().resolve(query)

    def mix_for(self, text, limit=music.PLAYLIST_MAX):
        return [{"title": f"잔잔곡{i}", "url": "u", "vid": f"calm{i:07d}", "duration": 200} for i in range(6)]


@test
async def mood_request_fills_the_queue_and_says_so():
    db, w, _, _ = await make_worker()
    w.music_source = MixSource(tempfile.mkdtemp())
    row = await run_job(db, w, "music_play", {"query": "잔잔한 플리 하나 틀어줘", "by": 5, "by_name": "자이", "status_msg": 9}, CHAT)
    assert row["result"] == "playlist", row
    assert len(await db._all("SELECT id FROM music_queue WHERE by_id=5")) == 6
    edit = [c for c in w.bot.named("edit_text") if c[1] == CHAT][-1][2]
    assert "분위기에 맞는" in edit and "6곡" in edit and "못 틀어요" not in edit, edit
    await stop_all(w)


if __name__ == "__main__":
    run_all()


@test
def mood_word_seen_once_is_not_treated_as_artist():
    """모르는 말이 제목에 한두 번만 → 가수로 보고 거르면 1곡만 남음. MIX_MIN 곡 넘을 때만 가수로."""
    src = music.Source(tempfile.mkdtemp())
    items = [{"id": f"onceAAAAA{i:02d}", "title": f"가수{i} - 노래{i}", "duration": 200} for i in range(6)]
    items.append({"id": "onceAAAAA99", "title": "몽글몽글 - 누구", "duration": 200})
    src._entries = lambda url: [{"id": "PLx"}] if "results" in url else (items if url.endswith("PLx") else [])
    got = src.mix_for("몽글몽글 노래")
    assert len(got) == 7, [g["title"] for g in got]
