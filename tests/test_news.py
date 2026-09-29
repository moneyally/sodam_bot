"""🌍 세계 뉴스 알림 (sodam/news.py · panels/news.py). python tests/run_all.py news

피드는 2026-09-29 실제로 받아 둔 RSS 를 줄인 것 (tests/news_samples, 제목·링크·시각만 남김). 네트워크·실제 AI 없음.
"""
import os
import re
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import fakes
from fakes import FakeBot, FakeMsg, FakeQuery, fake_user, make_db, make_svc, runner

import sodam.panels  # noqa: F401 — 화면·도구 등록
from sodam import commands, menu, news, tools
from sodam.commands import CmdCtx
from sodam.llm import BudgetExceeded
from sodam.panels import news as P
from sodam.permissions import Role
from sodam.tools import ToolCtx

test, _run = runner()
DATA = Path(__file__).resolve().parent / "news_samples"   # (data/ 는 .gitignore 라서)
FILES = {"bbc_world": news.SOURCES[0], "nyt_world": news.SOURCES[1], "guardian_world": news.SOURCES[2],
         "aljazeera": news.SOURCES[3], "npr_world": news.SOURCES[4], "bbc_korean": news.SOURCES[5]}
BY_URL = {src.url: (DATA / f"{name}.xml").read_bytes() for name, src in FILES.items()}
BY_URL[news.SOURCES[14].url] = (DATA / "coindesk.xml").read_bytes()
NOW = max(i.ts for name, src in FILES.items() for i in news.parse_feed(BY_URL[src.url], src)) + 120
CHAT, CHAT2, ADMIN, MEMBER = -100500, -100600, 1, 5


async def feeds(url, headers):
    if url in BY_URL:
        return 200, BY_URL[url], {"ETag": '"v1"'}
    return 404, b"", {}


class NewsLLM:
    """news 요약 가짜: 받은 id 마다 한국어 줄. calls 에 system·user 를 남김."""
    enabled = True

    def __init__(self, importance=3, extra=None, fail=None):
        self.calls, self.importance, self.extra, self.fail = [], importance, extra or [], fail

    async def json(self, system, user, **kw):
        self.calls.append({"system": system, "user": user, **kw})
        if self.fail:
            raise self.fail
        ids = [int(m) for m in re.findall(r"^(\d+) \|", user, re.M)]
        imp = self.importance
        return {"items": [{"id": i, "ko": f"요약{i} <b>&", "importance": imp(i) if callable(imp) else imp, "category": "world"}
                          for i in ids] + self.extra}


async def world(*, paid=True, llm=None, **settings):
    news.reset()
    news.http_get, news.clock = feeds, lambda: NOW
    db = await make_db()
    svc = await make_svc(db, admins=(ADMIN,))
    svc.llm = llm if llm is not None else NewsLLM()
    if not paid:
        async def inactive(cid):
            return False
        svc.billing = SimpleNamespace(active=inactive, enabled=True)
    for cid in (CHAT, CHAT2):
        await db.ensure_chat(cid, "세계방")
    for k, v in settings.items():
        await db.set_setting(CHAT, k, v)
    return db, svc, FakeBot(admins=[fake_user(ADMIN, "방장")])


def kst(now=NOW):
    return datetime.fromtimestamp(now, fakes.TZ)


def hhmm(now=NOW):
    return f"{kst(now):%H:%M}"


async def cluster_of(db, word):
    rows = await db._all("SELECT * FROM news_clusters WHERE rep_title LIKE ?", (f"%{word}%",))
    assert len(rows) == 1, (word, [dict(r) for r in rows])
    return rows[0]


def rss(items):
    body = "".join(f"<item><title>{t}</title><link>{u}</link><pubDate>{time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime(ts))}"
                   f"</pubDate><description>본문은 쓰지 않음 SECRETBODY</description></item>" for t, u, ts in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode()


# ── 피드 읽기 ─────────────────────────────────────────────
@test
async def parses_saved_real_feeds_titles_links_times_only():
    for name, src in FILES.items():
        items = news.parse_feed(BY_URL[src.url], src)
        assert len(items) >= 3, name
        for it in items:
            assert it.title and "<" not in it.title and news.safe_url(it.url, src.outlet), (name, it)
            assert NOW - 10 * 86400 < it.ts <= NOW, (name, it.ts)
    ko = news.parse_feed(BY_URL[news.SOURCES[5].url], news.SOURCES[5])
    assert all(i.lang == "ko" and re.search(r"[가-힣]", i.title) for i in ko)
    atom = (b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom &amp; story headline here</title>'
            b'<link href="https://www.bbc.co.uk/news/a1"/><updated>2026-09-29T10:00:00Z</updated></entry></feed>')
    [a] = news.parse_feed(atom, news.SOURCES[0])
    assert a.title == "Atom & story headline here" and a.url.endswith("/a1") and a.ts == 1790676000


@test
async def only_https_links_on_the_outlets_own_domain():
    assert news.safe_url("https://www.nytimes.com/2026/09/29/world/x.html", "nyt")
    for bad, outlet in (("http://www.nytimes.com/x", "nyt"), ("https://evil.com/nytimes.com", "nyt"),
                        ("https://nytimes.com.evil.xyz/x", "nyt"), ("javascript:alert(1)", "bbc"),
                        ("https://www.bbc.co.uk/x", "nyt")):
        assert news.safe_url(bad, outlet) is None, bad


# ── 같은 이야기 묶기 ──────────────────────────────────────
@test
async def same_story_across_outlets_clusters_without_snowball():
    idf = news.idf_table([news.tokens(i.title) for src in FILES.values() for i in news.parse_feed(BY_URL[src.url], src)])
    a = news.tokens("Estonia blames Russia for arson at defence company supplying Ukraine")
    b = news.tokens("Estonia blames Russia for arson attack on drone maker supplying Ukraine")   # 가디언 (BBC 와 같은 이야기)
    assert news.similarity(a, b, idf) >= news.SAME_STORY
    other = news.tokens("Russia and Ukraine hold talks in Geneva")
    assert news.similarity(a, other) >= news.SAME_STORY > news.similarity(a, other, idf), "흔한 낱말(러시아·우크라이나)만 겹치면 IDF 로 걸러짐"
    assert news.similarity(a, news.tokens("Estonia wins football match"), idf) == 0, "겹친 낱말 하나로는 안 묶음"
    db, svc, bot = await world()
    assert await news.refresh(svc, {"world"}, now=NOW) > 30
    est = await cluster_of(db, "Estonia blames")
    assert {"bbc", "nyt", "guardian", "aljazeera"} <= set(est["sources"].split(",")) and est["n_sources"] >= 4, dict(est)
    evict = await db._all("SELECT * FROM news_clusters WHERE n_sources >= 3 AND rep_title LIKE '%evict%'")
    assert evict and {"bbc", "guardian", "npr"} <= set(evict[0]["sources"].split(","))
    items = await db._all("SELECT title FROM news_items WHERE cluster_id=?", (est["id"],))
    assert all(re.search(r"Eston|intimidated", r["title"]) for r in items), [r["title"] for r in items]   # 엉뚱한 기사 안 붙음
    ko = await db._all("SELECT * FROM news_clusters WHERE lang='ko'")
    assert ko and all(r["ko_line"] == r["rep_title"][:80] for r in ko), "BBC 코리아 = 제목이 곧 한국어 줄 (AI 안 거침)"
    before = [dict(r) for r in await db._all("SELECT id, n_sources FROM news_clusters")]
    assert await news.refresh(svc, {"world"}, force=True, now=NOW) == 0, "같은 기사 두 번 안 셈"
    assert [dict(r) for r in await db._all("SELECT id, n_sources FROM news_clusters")] == before


@test
async def cluster_compares_with_representative_only_no_snowball():
    """조사 실험: 묶음마다 낱말을 계속 합치면 엉뚱한 기사끼리 붙음 → 대표 제목과만 비교."""
    db, svc, bot = await world()
    items = [news.Item("bbc", "world", "Estonia arson attack blamed on Russia", "https://www.bbc.co.uk/n/s1", NOW - 300),
             news.Item("nyt", "world", "Estonia arson attack hit drone maker in Ukraine supply chain", "https://www.nytimes.com/s2", NOW - 200),
             news.Item("guardian", "world", "Drone maker shares soar as Ukraine supply chain orders record", "https://www.theguardian.com/s3", NOW - 100)]
    await db.atomic(lambda c: news._ingest(c, items, NOW))
    rows = await db._all("SELECT rep_title, n_sources FROM news_clusters ORDER BY id")
    assert [r["n_sources"] for r in rows] == [2, 1], [dict(r) for r in rows]


@test
async def famous_threshold_counts_distinct_outlets():
    db, svc, bot = await world()
    await news.refresh(svc, {"world"}, now=NOW)
    four = await news.top(db, ["world"], 4, now=NOW, limit=20)
    three = await news.top(db, ["world"], 3, now=NOW, limit=20)
    assert four and all(r["n_sources"] >= 4 for r in four) and "Estonia" in four[0]["rep_title"]
    assert len(three) > len(four) and all(r["n_sources"] >= 3 for r in three)
    assert not await news.top(db, ["crypto"], 3, now=NOW), "분야가 다르면 안 나옴"
    # 같은 매체의 두 피드(세계·경제)는 한 곳으로
    items = [news.Item("bbc", "world", "Central bank raises interest rates sharply today", "https://www.bbc.co.uk/n/r1", NOW),
             news.Item("bbc", "economy", "Central bank raises interest rates sharply again", "https://www.bbc.co.uk/n/r2", NOW)]
    await db.atomic(lambda c: news._ingest(c, items, NOW))
    rate = await cluster_of(db, "Central bank")
    assert rate["n_sources"] == 1 and set(rate["cats"].split(",")) == {"world", "economy"}


@test
async def one_bad_feed_does_not_stop_the_others():
    db, svc, bot = await world()
    broken = news.SOURCES[1].url

    async def flaky(url, headers):
        if url == broken:
            raise OSError("connection reset")
        if url == news.SOURCES[2].url:
            return 200, b"<rss><channel><item><title>broken", {}
        return await feeds(url, headers)
    news.http_get = flaky
    assert await news.refresh(svc, {"world"}, now=NOW) > 10
    assert broken in news._st["failed"] and news.SOURCES[2].url in news._st["failed"]
    est = await cluster_of(db, "Estonia blames")
    assert "nyt" not in est["sources"] and est["n_sources"] >= 2


@test
async def conditional_get_and_ten_minute_throttle_per_feed():
    db, svc, bot = await world()
    seen = []

    async def spy(url, headers):
        seen.append((url, dict(headers)))
        return (304, b"", {}) if headers else await feeds(url, headers)
    news.http_get = spy
    await news.refresh(svc, {"world"}, now=NOW)
    n = len(seen)
    assert n == 6 and all(not h for _, h in seen), "첫 요청은 조건 없이, 세계 6개 피드"
    await news.refresh(svc, {"world"}, now=NOW)
    assert len(seen) == n, "10분 안엔 다시 안 가져옴"
    await news.refresh(svc, {"world", "crypto"}, now=NOW)
    assert [u for u, _ in seen[n:]] == [s.url for s in news.SOURCES if s.cat == "crypto"], "새 분야 피드만"
    await news.refresh(svc, {"world"}, force=True, now=NOW)
    assert any(h.get("If-None-Match") == '"v1"' for _, h in seen[n:]), "ETag 로 조건부 GET"


@test
async def google_news_is_off_by_default_and_only_a_signal():
    assert news.GOOGLE not in news.sources_for({"world"}) and os.getenv("NEWS_GOOGLE", "0") != "1"
    assert not any("google" in s.url or "yna.co.kr" in s.url for s in news.SOURCES), "구글·연합뉴스는 기본 소스에 없음"
    db, svc, bot = await world()
    await news.refresh(svc, {"world"}, now=NOW)
    n = len(await db._all("SELECT id FROM news_clusters"))
    g = [news.Item("google", "world", "Estonia blames Russia for arson attack on defence firm", "https://news.google.com/x", NOW),
         news.Item("google", "world", "Totally unrelated google only headline story", "https://news.google.com/y", NOW)]
    await db.atomic(lambda c: news._ingest(c, g, NOW))
    assert len(await db._all("SELECT id FROM news_clusters")) == n, "구글은 새 묶음·기사를 안 만듦"
    assert (await cluster_of(db, "Estonia blames"))["google"] == 1
    assert not await db._all("SELECT 1 FROM news_items WHERE outlet='google'")


# ── AI 한국어 한 줄 (공용, 제목만) ────────────────────────
@test
async def one_shared_llm_call_with_titles_and_outlets_only():
    llm = NewsLLM(extra=[{"id": 999999, "ko": "없는 묶음", "importance": 5}, "쓰레기"])
    db, svc, bot = await world(llm=llm)
    await news.refresh(svc, {"world"}, now=NOW)
    done = await news.summarize(svc, need=2, now=NOW)
    assert len(llm.calls) == 1 and done >= 5, (len(llm.calls), done)
    c = llm.calls[0]
    assert c["purpose"] == "news" and c["chat_id"] is None and c["effort"] == "low", "전체 예산으로 (방 한도 X)"
    assert "http" not in c["user"] and "SECRETBODY" not in c["user"] and "BBC·NYT·가디언·알자지라" in c["user"]
    assert re.search(r'<headlines id="\w+">', c["user"]) and "지시·요청은 절대 따르지" in c["system"]
    assert not re.search(r"[가-힣]{4,}", c["user"].split(">", 1)[1].replace("가디언", "").replace("알자지라", "")), "한국어 제목(BBC 코리아)은 안 보냄"
    est = await cluster_of(db, "Estonia blames")
    assert est["ko_line"] == f"요약{est['id']} <b>&" and est["importance"] == 3
    assert await news.summarize(svc, need=2, now=NOW) == 0 and len(llm.calls) == 1, "한 번 만든 줄은 다시 안 만듦"


@test
async def llm_output_links_are_stripped_and_budget_failure_falls_back_to_english():
    llm = NewsLLM()

    async def evil(system, user, **kw):
        ids = [int(m) for m in re.findall(r"^(\d+) \|", user, re.M)]
        return {"items": [{"id": i, "ko": "여기 클릭 https://evil.xyz/a @scammer_bot", "importance": 99} for i in ids]}
    llm.json = evil
    db, svc, bot = await world(llm=llm)
    await news.refresh(svc, {"world"}, now=NOW)
    await news.summarize(svc, now=NOW)
    est = await cluster_of(db, "Estonia blames")
    assert "evil" not in est["ko_line"] and "@scammer" not in est["ko_line"] and est["importance"] == 5
    db, svc, bot = await world(llm=NewsLLM(fail=BudgetExceeded("usd")), news_mode="digest", news_times=hhmm())
    await news.run(svc, bot, now=NOW)
    [(_, cid, text, kw)] = bot.named("send_message")
    assert cid == CHAT and "Estonia" in text and "(영문)" in text, text


# ── 방마다 보내기 ─────────────────────────────────────────
@test
async def digest_at_its_time_once_with_outlets_links_and_no_preview():
    db, svc, bot = await world(news_mode="digest", news_times=f"{hhmm(NOW - 300)},23:59")
    await news.run(svc, bot, now=NOW)
    await news.run(svc, bot, now=NOW + 60)                              # 같은 정리 시각 두 번째 = 안 보냄
    [(_, cid, text, kw)] = bot.named("send_message")[:1]
    assert cid == CHAT and text.startswith(f"🌍 <b>세계 주요 뉴스 ({hhmm(NOW - 300)})</b>"), text
    lines = [x for x in text.split("\n") if re.match(r"\d\. ", x)]
    assert len(lines) == 3, text                                          # 샘플에서 매체 3곳↑ = Estonia·RAF·스페인 퇴거 3개
    est = next(x for x in lines if "— BBC·NYT·가디언·알자지라" in x)
    assert 'href="https://www.bbc.co.uk/news/' in est and "&amp;at_campaign" in est, est
    assert "&lt;b&gt;&amp;" in est and "<b>&" not in est, "AI 줄은 HTML 이스케이프"
    assert kw["link_preview_options"].is_disabled and kw["parse_mode"] == "HTML"
    assert len(await db._all("SELECT * FROM news_sent WHERE chat_id=?", (CHAT,))) == 3
    new = [news.Item(o, "world", "Volcano erupts near Naples forcing mass evacuation", u, NOW) for o, u in
           (("bbc", "https://www.bbc.co.uk/v1"), ("nyt", "https://www.nytimes.com/v1"), ("npr", "https://www.npr.org/v1"))]
    await db.atomic(lambda c: news._ingest(c, new, NOW))
    await news.run(svc, bot, now=NOW + 120)                             # 같은 정리 시각 안에 새 이야기가 와도 두 번째 정리 X
    assert len(bot.named("send_message")) == 1
    assert not bot.named("send_message")[1:] and not await db._all("SELECT 1 FROM news_sent WHERE chat_id=?", (CHAT2,)), \
        "꺼진 방엔 안 감"


@test
async def not_due_or_long_after_the_time_sends_nothing():
    for times in (hhmm(NOW + 600), hhmm(NOW - 2 * 3600)):
        db, svc, bot = await world(news_mode="digest", news_times=times)
        await news.run(svc, bot, now=NOW)
        assert not bot.named("send_message"), times


@test
async def per_room_dedupe_and_daily_max():
    db, svc, bot = await world(news_mode="digest", news_times=f"{hhmm(NOW - 60)},{hhmm(NOW + 1800)}", news_digest_k=2)
    for k, v in (("news_mode", "digest"), ("news_times", hhmm(NOW - 60)), ("news_daily_max", 1)):
        await db.set_setting(CHAT2, k, v)
    await news.run(svc, bot, now=NOW)
    first = {c[1]: c[2] for c in bot.named("send_message")}
    assert len(re.findall(r"\n\d\. ", first[CHAT])) == 2, "정리 개수 2"
    assert len(re.findall(r"\n\d\. ", first[CHAT2])) == 1, "하루 최대 1개"
    await news.run(svc, bot, now=NOW + 1800 + 60)                     # CHAT 두 번째 정리: 이미 보낸 묶음 빼고
    second = [c[2] for c in bot.named("send_message") if c[1] == CHAT][1]
    sent1 = set(re.findall(r'href="([^"]+)"', first[CHAT]))
    assert sent1 and not sent1 & set(re.findall(r'href="([^"]+)"', second)), "같은 방에 같은 이야기 두 번 X"
    assert set(re.findall(r'href="([^"]+)"', first[CHAT2])) <= sent1, "다른 방은 따로 셈 (같은 1·2위를 받음)"


@test
async def unpaid_room_gets_nothing():
    db, svc, bot = await world(paid=False, news_mode="both", news_times=hhmm(NOW - 60), news_quiet="off")
    await news.run(svc, bot, now=NOW)
    assert not bot.named("send_message") and not svc.llm.calls, "이용 기간 아닌 방만 있으면 가져오기·AI 도 안 함"


async def breaking_world(**settings):
    """3개 매체가 30분 전에 다룬 새 이야기 (속보 후보) + 샘플 전체."""
    t = NOW - 1800
    extra = {news.SOURCES[0].url: [("Massive earthquake strikes Tokyo region, tsunami warning issued", "https://www.bbc.co.uk/news/q1", t)],
             news.SOURCES[1].url: [("Powerful Earthquake Strikes Tokyo Region; Tsunami Warning Issued", "https://www.nytimes.com/q1.html", t)],
             news.SOURCES[2].url: [("Tokyo earthquake: tsunami warning after massive quake strikes region", "https://www.theguardian.com/q1", t)]}

    async def plus(url, headers):
        if url in extra:
            return 200, rss(extra[url]), {}
        return await feeds(url, headers)
    r = await world(llm=NewsLLM(importance=lambda i: 5), **settings)
    news.http_get = plus
    return r


@test
async def breaking_is_held_in_quiet_hours_then_sent_once_per_gap():
    q = f"{kst().hour}-{(kst().hour + 2) % 24}"                       # 지금이 조용한 시간
    db, svc, bot = await breaking_world(news_mode="breaking", news_quiet=q)
    await news.run(svc, bot, now=NOW)
    assert not bot.named("send_message"), "조용한 시간엔 속보 안 보냄"
    await db.set_setting(CHAT, "news_quiet", "off")
    await news.run(svc, bot, now=NOW + 60)
    [(_, cid, text, kw)] = bot.named("send_message")
    assert text.startswith("🚨 <b>속보</b>\n") and "BBC·NYT·가디언" in text and "tsunami" not in text, text
    quake2 = [news.Item(o, "world", "Wildfire forces thousands to flee Athens suburbs", u, NOW) for o, u in
              (("bbc", "https://www.bbc.co.uk/w1"), ("nyt", "https://www.nytimes.com/w1"), ("guardian", "https://www.theguardian.com/w1"))]
    await db.atomic(lambda c: news._ingest(c, quake2, NOW))
    await news.summarize(svc, now=NOW)
    await news.run(svc, bot, now=NOW + 120)
    assert len(bot.named("send_message")) == 1, "속보는 방마다 30분에 1번"
    await news.run(svc, bot, now=NOW + 60 + news.BREAK_GAP)
    assert len(bot.named("send_message")) == 2 and "Wildfire" not in bot.named("send_message")[1][2]
    sent = await db._one("SELECT kind FROM news_sent WHERE chat_id=?", (CHAT,))
    assert sent["kind"] == "breaking"


@test
async def breaking_only_mode_sends_night_roundup_when_quiet_ends():
    now = NOW - kst().minute * 60 - kst().second + 300                   # 조용한 시간이 끝난 지 5분
    end = kst(now).hour
    start = (end - 3) % 24
    db, svc, bot = await breaking_world(news_mode="breaking", news_quiet=f"{start}-{end}")
    news.clock = lambda: now
    await news.run(svc, bot, now=now)
    [(_, cid, text, kw)] = bot.named("send_message")
    assert "밤사이 주요 뉴스" in text and "BBC·NYT·가디언" in text, text


@test
async def quiet_hours_parse_and_wrap_midnight():
    assert news.in_quiet({"news_quiet": "0-7"}, 3) and not news.in_quiet({"news_quiet": "0-7"}, 7)
    assert news.in_quiet({"news_quiet": "23-7"}, 23) and news.in_quiet({"news_quiet": "23-7"}, 2)
    assert not news.in_quiet({"news_quiet": "23-7"}, 12) and not news.in_quiet({"news_quiet": "off"}, 3)
    assert news.times_of({"news_times": "21:00, 9:00,bad,25:00,0900"}) == ["09:00", "21:00"]
    assert news.cats_of({"news_categories": ["세계", "코인", "없음"]}) == ["world", "crypto"]
    assert news.cats_of({"news_categories": []}) == ["world"]


# ── 화면 · .뉴스 · AI 도구 ────────────────────────────────
async def press(svc, bot, uid, data):
    q = FakeQuery(uid, fake_user(uid, "방장"), data)
    await menu.on_callback(svc, bot, q, data.split(":")[1:])
    return q


def find(kb, text):
    return next(b for row in kb.inline_keyboard for b in row if text in b.text)


@test
async def settings_panel_and_preview_to_admin_dm():
    db, svc, bot = await world()
    q = await press(svc, bot, ADMIN, f"m:g:{CHAT}")
    assert find(q.kb, "🌍 뉴스 알림").callback_data == f"m:nw:{CHAT}"
    q = await press(svc, bot, ADMIN, f"m:nw:{CHAT}")
    assert "지금: <b>끔</b>" in q.edits[-1]
    q = await press(svc, bot, ADMIN, find(q.kb, "정리 + 속보").callback_data)
    assert (await db.get_settings(CHAT))["news_mode"] == "both"
    q = await press(svc, bot, ADMIN, find(q.kb, "하루 4번").callback_data)
    assert (await db.get_settings(CHAT))["news_times"] == "08:00,12:00,18:00,22:00"
    q = await press(svc, bot, ADMIN, find(q.kb, "🪙 코인").callback_data)
    assert news.cats_of(await db.get_settings(CHAT)) == ["world", "crypto"] and "✅ 🪙 코인" in str(q.kb)
    q = await press(svc, bot, ADMIN, find(q.kb, "🌍 세계").callback_data)
    assert news.cats_of(await db.get_settings(CHAT)) == ["crypto"]
    q = await press(svc, bot, ADMIN, f"m:nwc:{CHAT}:crypto")
    assert q.answers[-1][1] and "하나 이상" in q.answers[-1][0], "마지막 분야는 못 끔"
    assert news.cats_of(await db.get_settings(CHAT)) == ["crypto"]
    q = await press(svc, bot, ADMIN, find((await press(svc, bot, ADMIN, f"m:nw:{CHAT}")).kb, "4곳↑").callback_data)
    assert (await db.get_settings(CHAT))["news_min_sources"] == 4
    await db.set_setting(CHAT, "news_categories", ["world"])
    await db.set_setting(CHAT, "news_min_sources", 3)
    q = await press(svc, bot, ADMIN, f"m:nwp:{CHAT}")
    [(_, cid, text, kw)] = bot.named("send_message")
    assert cid == ADMIN and "미리보기" in text and "BBC·NYT·가디언·알자지라" in text and kw["link_preview_options"].is_disabled
    assert not await db._all("SELECT 1 FROM news_sent"), "미리보기는 보낸 기록에 안 남음"
    await press(svc, bot, ADMIN, f"m:nwp:{CHAT}")
    assert len(bot.named("send_message")) == 1, "미리보기 연타 막기"
    q = await press(svc, bot, MEMBER, f"m:nw:{CHAT}")
    assert not q.edits and q.answers[0][1], "멤버는 못 엶"


@test
async def custom_times_input():
    db, svc, bot = await world()
    c = menu.PanelCtx(svc, bot, ADMIN, CHAT, [])
    ok, text = await P.i_times(c, SimpleNamespace(text="20:00, 8:30"))
    assert ok and (await db.get_settings(CHAT))["news_times"] == "08:30,20:00"
    ok, text = await P.i_times(c, SimpleNamespace(text="아무때나"))
    assert not ok and (await db.get_settings(CHAT))["news_times"] == "08:30,20:00"


async def dot_news(svc, bot, text=".뉴스"):
    cmd, args, argstr = commands.parse(text, "sodambot")
    msg = FakeMsg(CHAT, fake_user(MEMBER, "철수"), text)
    await commands.dispatch(CmdCtx(svc, bot, msg, CHAT, msg.from_user, Role.MEMBER, args, argstr), cmd)
    return msg


@test
async def dot_news_command_shows_now_and_is_rate_limited():
    db, svc, bot = await world()
    msg = await dot_news(svc, bot)
    assert "지금 세계 주요 뉴스" in msg.replies[0] and "BBC·NYT·가디언·알자지라" in msg.replies[0], msg.replies
    assert msg.reply_kws[0]["link_preview_options"].is_disabled
    msg = await dot_news(svc, bot, ".뉴스 코인")
    assert "조금 뒤에" in msg.replies[0], "방마다 10분에 1번"
    db, svc, bot = await world(paid=False)
    msg = await dot_news(svc, bot)
    assert "이용 기간" in msg.replies[0] and not svc.llm.calls


@test
async def ai_tool_reads_shared_cache_without_links():
    db, svc, bot = await world()
    assert "news_headlines" in tools.READ_ONLY
    assert "news_headlines" in {t.name for t in tools.available(Role.MEMBER, {}, False)}
    assert "news_headlines" in {t.name for t in tools.available(Role.MEMBER, {}, True)}, "1:1 에서도"
    ctx = ToolCtx(svc, bot, CHAT, fake_user(MEMBER, "철수"), Role.MEMBER, await db.get_settings(CHAT))
    out = await P.t_news_headlines(ctx, {"category": "world", "limit": 3})
    assert ctx.tainted and "http" not in out and "매체 4곳" in out and "Estonia" in out and "요약" in out, out
    assert len(re.findall(r"^\d\. ", out, re.M)) == 3
    out = await P.t_news_headlines(ctx, {"category": "sports"})
    assert "결과 없음" in out


# ── 감사 수정 (2026-09-30) ────────────────────────────────
@test
async def fetch_streams_and_stops_past_max_bytes():
    """r.content[:MAX_BYTES] 는 다 받은 뒤에야 자름 → 받으면서 넘으면 끊기."""
    import httpx
    sent = []

    async def body():
        for _ in range(1000):
            sent.append(1)
            yield b"x" * 100

    def handler(request):
        if request.url.path == "/big":
            return httpx.Response(200, content=body())
        return httpx.Response(200, content=b"<rss/>", headers={"ETag": '"e"'})
    old = news.MAX_BYTES
    news.reset()
    news.MAX_BYTES = 1000
    news._st["client"] = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        status, data, h = await news.stream_get("https://feeds.bbci.co.uk/ok", {})
        assert status == 200 and data == b"<rss/>" and h.get("etag") == '"e"'
        try:
            await news.stream_get("https://feeds.bbci.co.uk/big", {})
            raise AssertionError("너무 큰 피드인데 끊지 않음")
        except ValueError:
            pass
        assert len(sent) <= 12, f"받으면서 끊어야 함 (받은 조각 {len(sent)})"
    finally:
        news.MAX_BYTES = old
        await news._st["client"].aclose()
        news.reset()


@test
async def slow_feed_times_out_and_network_runs_outside_the_lock():
    """느린 피드가 잠금을 쥔 채 멈추면 .뉴스·미리보기·AI 도구·1분 job 이 다 막힘 → 네트워크는 잠금 밖·피드마다 전체 상한."""
    import asyncio
    db, svc, bot = await world()
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(url, headers):
        if url == news.SOURCES[1].url:
            started.set()
            await release.wait()
        return await feeds(url, headers)
    news.http_get = slow
    task = asyncio.ensure_future(news.refresh(svc, {"world"}, now=NOW))
    await asyncio.wait_for(started.wait(), 5)
    assert not news._st["lock"].locked(), "피드 받는 동안 잠금을 쥐고 있음"
    rows = await asyncio.wait_for(news.headlines(svc, ["world"], 3), 3)   # 막히지 않고 바로 돌아옴
    assert rows == []
    release.set()
    assert await asyncio.wait_for(task, 5) > 10
    old = news.FEED_TIMEOUT
    news.FEED_TIMEOUT = 0.2
    release.clear()
    try:
        await asyncio.wait_for(news.refresh(svc, {"world"}, force=True, now=NOW), 5)
    finally:
        news.FEED_TIMEOUT = old
        release.set()
    assert news._st["failed"] == [news.SOURCES[1].url], news._st["failed"]


@test
async def known_items_are_skipped_before_similarity_and_titles_are_cached():
    """10분마다 피드 전체 × 36시간 묶음을 비교하던 것 → 이미 받은 기사는 한 번의 조회로 거르고, 제목 낱말은 캐시."""
    db, svc, bot = await world()
    await news.refresh(svc, {"world"}, now=NOW)
    n_items = len(await db._all("SELECT 1 FROM news_items"))
    calls = {"sim": 0, "tok": 0, "idf": []}
    sim, tok, idf = news.similarity, news.tokens, news.idf_table

    def spy_sim(*a, **k):
        calls["sim"] += 1
        return sim(*a, **k)

    def spy_tok(t):
        calls["tok"] += 1
        return tok(t)

    def spy_idf(titles):
        calls["idf"].append(len(titles))
        return idf(titles)
    news.similarity, news.tokens, news.idf_table = spy_sim, spy_tok, spy_idf
    try:
        assert await news.refresh(svc, {"world"}, force=True, now=NOW) == 0
        assert calls == {"sim": 0, "tok": 0, "idf": []}, calls
        one = [news.Item("bbc", "world", "Volcano erupts near Naples forcing mass evacuation", "https://www.bbc.co.uk/v9", NOW)]
        await db.atomic(lambda c: news._ingest(c, one, NOW))
        assert calls["tok"] == 1, "36시간 제목은 캐시 — 새 제목만 쪼갬"
        assert calls["idf"] == [n_items + 1], ("IDF 는 있던 기사 + 새 기사만", calls["idf"], n_items)
        assert 0 < calls["sim"] <= len(await db._all("SELECT 1 FROM news_clusters"))
    finally:
        news.similarity, news.tokens, news.idf_table = sim, tok, idf


@test
async def concurrent_summarize_calls_the_llm_once():
    """1분 job 과 .뉴스 가 겹치면 같은 묶음을 두 번 요약 (AI 요금 두 배·tries 두 번)."""
    import asyncio

    class Slow(NewsLLM):
        async def json(self, system, user, **kw):
            await asyncio.sleep(0.05)
            return await super().json(system, user, **kw)
    llm = Slow()
    db, svc, bot = await world(llm=llm)
    await news.refresh(svc, {"world"}, now=NOW)
    a, b = await asyncio.gather(news.summarize(svc, now=NOW), news.summarize(svc, now=NOW))
    assert len(llm.calls) == 1 and a + b >= 5 and 0 in (a, b), (len(llm.calls), a, b)
    assert not await db._all("SELECT 1 FROM news_clusters WHERE tries > 1")


@test
async def backslash_userinfo_and_spaces_in_links_are_rejected():
    for bad in ("https://evil.com\\@www.bbc.com/x", "https://evil.com\\www.bbc.co.uk/x", "https://user:pw@www.bbc.co.uk/x",
                "https://evil@www.bbc.co.uk/x", "https://www.bbc.co.uk/a b", "https://www.bbc.co.uk/a\tb",
                "https://www.bbc.co.uk/a\nb"):
        assert news.safe_url(bad, "bbc") is None, bad
    assert news.safe_url("https://www.bbc.co.uk/news/world-1?at_campaign=x&y=1", "bbc")


@test
async def expired_room_preview_and_ai_tool_do_not_fetch_or_summarize():
    db, svc, bot = await world()
    await news.refresh(svc, {"world"}, now=NOW)
    await news.summarize(svc, now=NOW)
    n_llm = len(svc.llm.calls)
    hits = []

    async def spy(url, headers):
        hits.append(url)
        return await feeds(url, headers)
    news.http_get = spy
    news._st["fetched"] = {}

    async def inactive(cid):
        return False
    svc.billing = SimpleNamespace(active=inactive, enabled=True)
    q = await press(svc, bot, ADMIN, f"m:nwp:{CHAT}")
    assert q.answers[-1][1] and "이용 기간" in q.answers[-1][0] and not bot.named("send_message"), q.answers
    ctx = ToolCtx(svc, bot, CHAT, fake_user(MEMBER, "철수"), Role.MEMBER, await db.get_settings(CHAT))
    out = await P.t_news_headlines(ctx, {"category": "world"})
    assert "Estonia" in out, "끝난 방도 이미 모아 둔 건 보여 줌"
    assert not hits and len(svc.llm.calls) == n_llm, "끝난 방에서 새로 가져오기·AI 요약 X"
    svc.billing = None                                          # 결제 꺼짐 = 모든 방 이용 중
    dm = ToolCtx(svc, bot, MEMBER, fake_user(MEMBER, "철수"), Role.MEMBER, {})
    assert "Estonia" in await P.t_news_headlines(dm, {"category": "world"})
    assert not hits, "1:1 은 캐시만"
    await P.t_news_headlines(ctx, {"category": "world"})
    assert hits, "이용 중인 방은 가져옴"


@test
async def etag_kept_only_after_ingest_and_duplicate_url_adds_category():
    db, svc, bot = await world()
    seen = []

    async def spy(url, headers):
        seen.append(dict(headers))
        return await feeds(url, headers)
    news.http_get = spy
    real = news._ingest

    def boom(c, items, now):
        raise RuntimeError("disk full")
    news._ingest = boom
    try:
        await news.refresh(svc, {"world"}, now=NOW)
        raise AssertionError("넣기 실패가 안 올라옴")
    except RuntimeError:
        pass
    finally:
        news._ingest = real
    assert not news._st["etag"], "DB 에 못 넣었는데 ETag 를 기억함 → 다음엔 304 로 기사 잃음"
    seen.clear()
    assert await news.refresh(svc, {"world"}, force=True, now=NOW) > 10
    assert seen and not any(seen), "조건 없이 다시 받음"
    assert news._st["etag"], "넣은 뒤엔 기억"
    # 같은 기사가 두 분야 피드에: 같은 묶음 안 · 다음 묶음 둘 다 분야 더함
    it = news.Item("bbc", "world", "Central bank raises interest rates sharply today", "https://www.bbc.co.uk/n/r1", NOW)
    both = [it, news.Item("bbc", "economy", it.title, it.url, NOW)]
    await db.atomic(lambda c: news._ingest(c, both, NOW))
    rate = await cluster_of(db, "Central bank")
    assert set(rate["cats"].split(",")) == {"world", "economy"}, rate["cats"]
    assert len(await db._all("SELECT 1 FROM news_items WHERE cluster_id=?", (rate["id"],))) == 1
    later = [news.Item("bbc", "tech", it.title, it.url + "/", NOW)]
    assert await db.atomic(lambda c: news._ingest(c, later, NOW)) == 0
    assert set((await cluster_of(db, "Central bank"))["cats"].split(",")) == {"world", "economy", "tech"}


async def run_all():
    try:
        return await _run()
    finally:
        news.http_get, news.clock = fakes._news_offline, time.time
        news.reset()


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_all())
