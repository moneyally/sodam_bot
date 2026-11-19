"""🖼 find_photo: 웹에서 사진 찾아 방에 올리기. python tests/run_all.py find_photo

2026-11-19 블리비 라운지 오너 '이채영 사진 줘 → 못 가져옴, 챗지피티처럼 소스 사진 가져오게'.
OpenAI web_search 이미지 결과(llm.image_search)를 받아 sendPhoto + [📄 출처] 버튼으로 올린다. 모든 방 기본 켬."""
import asyncio
import datetime as _dt
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeBot, fake_user, make_db, make_svc, runner  # noqa: E402
from telegram.error import BadRequest  # noqa: E402

from sodam import llm  # noqa: E402
from sodam.panels.findphoto import t_find_photo  # noqa: E402
from sodam.permissions import Role  # noqa: E402
from sodam.tools import ToolCtx  # noqa: E402

test, run_all = runner()
ROOM = -100555
MEM = fake_user(5, "멤버", "mem")


def wsc(results):
    return SimpleNamespace(type="web_search_call", action=SimpleNamespace(type="search", sources=[]), results=results)


def resp_with(results, usage_tok=10):
    return SimpleNamespace(output=[wsc(results)], output_text="",
                           usage=SimpleNamespace(input_tokens=usage_tok, output_tokens=0, total_tokens=usage_tok))


def img(url, src="https://src.example/page", cap="사진"):
    """OpenAI 원본 형식 (image_results 파서 시험용)."""
    return {"type": "image_result", "image_url": url, "source_website_url": src, "caption": cap}


def pic(url, src="https://src.example/page", cap="사진"):
    """image_search 가 돌려주는 파싱된 형식 (도구 시험용)."""
    return {"image_url": url, "source_url": src, "caption": cap}


async def world():
    db = await make_db()
    svc = await make_svc(db, admins=())
    await db.ensure_chat(ROOM, "사진방")
    await db.upsert_user(MEM)
    return db, svc, FakeBot()


def ctx(svc, bot, *, uid=MEM.id, role=Role.MEMBER, settings=None, req=777):
    c = ToolCtx(svc, bot, ROOM, fake_user(uid, "멤버", "mem"), role, settings or {"photo_daily": 10})
    c.request_msg = SimpleNamespace(message_id=req)
    return c


def scripted(svc, batches):
    """svc.llm.image_search 를 대본으로 — 호출마다 batches 에서 하나씩 돌려준다."""
    calls = []
    q = list(batches)

    async def fake(query, chat_id=None):
        calls.append(query)
        return q.pop(0) if q else []
    svc.llm = SimpleNamespace(image_search=fake)
    return calls


def photos(bot):
    return [c for c in bot.calls if c[0] == "send_photo"]


# ── llm.image_results 파서 (OpenAI web_search_call.results) ──────────────────
@test
async def image_results_parses_only_https_image_results():
    r = resp_with([
        img("https://a.com/1.jpg", "https://a.com", "cap1"),
        img("http://insecure.com/2.jpg"),                       # https 아님 → 버림
        {"type": "text_result", "image_url": "https://x.com/t"},  # image_result 아님 → 버림
        img("https://b.com/3.png", "ftp://bad", "cap3"),          # 이미지 OK, 출처 https 아님 → source 빈칸
    ])
    out = llm.image_results(r)
    assert [o["image_url"] for o in out] == ["https://a.com/1.jpg", "https://b.com/3.png"], out
    assert out[0]["source_url"] == "https://a.com" and out[1]["source_url"] == "", out


@test
async def image_results_reads_model_extra_fallback():
    """SDK 가 results 를 타입으로 안 내보내면 model_extra 에 있음 → 그것도 본다."""
    item = SimpleNamespace(type="web_search_call")
    item.model_extra = {"results": [img("https://m.com/x.jpg")]}
    out = llm.image_results(SimpleNamespace(output=[item]))
    assert len(out) == 1 and out[0]["image_url"] == "https://m.com/x.jpg", out


# ── 도구: 찾아서 방에 올리기 ──────────────────────────────────────────────
@test
async def posts_photo_with_source_button_and_quiet():
    db, svc, bot = await world()
    calls = scripted(svc, [[pic("https://p.com/a.jpg", "https://p.com/page", "이채영")]])
    c = ctx(svc, bot)
    out = await t_find_photo(c, {"query": "이채영 사진"})
    assert calls == ["이채영 사진"], calls
    ph = photos(bot)
    assert len(ph) == 1 and ph[0][2] == "https://p.com/a.jpg", ph        # 그 url 로 sendPhoto
    kw = ph[0][4]
    assert kw.get("reply_to_message_id") == 777, kw                      # 요청에 답장
    btn = kw["reply_markup"].inline_keyboard[0][0]
    assert btn.text == "📄 출처" and btn.url == "https://p.com/page", btn  # 출처 버튼
    assert c.quiet is True and "올렸" in out, out                        # 사진이 답 → AI 는 짧게 (ctx.quiet)
    day = _dt.datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")
    assert await db.counter(day, 0, f"photo:{MEM.id}") == 1              # 하루 한도 셈


@test
async def count_two_posts_two_photos():
    db, svc, bot = await world()
    scripted(svc, [[pic("https://p.com/a.jpg"), pic("https://p.com/b.jpg"), pic("https://p.com/c.jpg")]])
    out = await t_find_photo(ctx(svc, bot), {"query": "강아지", "count": 2})
    assert len(photos(bot)) == 2, out                                   # count 만큼만 (최대 3)


@test
async def sexual_query_refused_without_search():
    db, svc, bot = await world()
    calls = scripted(svc, [[pic("https://x/1.jpg")]])
    out = await t_find_photo(ctx(svc, bot), {"query": "이채영 야한 사진"})
    assert calls == [] and not photos(bot), (calls, out)                 # 검색도 안 함
    assert "거절" in out or "못 찾아" in out, out


@test
async def photo_daily_zero_off_for_member_on_for_owner():
    db, svc, bot = await world()
    scripted(svc, [[pic("https://x/1.jpg")], [pic("https://x/2.jpg")]])
    off = await t_find_photo(ctx(svc, bot, settings={"photo_daily": 0}), {"query": "고양이"})
    assert not photos(bot) and "꺼져" in off, off                        # 멤버는 꺼진 방에서 못 씀
    await t_find_photo(ctx(svc, bot, role=Role.OWNER, settings={"photo_daily": 0}), {"query": "고양이"})
    assert len(photos(bot)) == 1                                        # 오너는 한도·끔 면제


@test
async def daily_cap_blocks_after_limit():
    db, svc, bot = await world()
    day = _dt.datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")
    await db.bump(day, 0, f"photo:{MEM.id}", 10)                         # 이미 10번 썼음
    scripted(svc, [[pic("https://x/1.jpg")]])
    out = await t_find_photo(ctx(svc, bot, settings={"photo_daily": 10}), {"query": "노을"})
    assert not photos(bot) and "하루" in out, out


@test
async def dead_url_falls_back_to_next_candidate():
    """첫 사진 url 을 텔레그램이 못 받으면(죽은 링크) 다음 후보로 — 포기하지 않음."""
    db, svc, bot = await world()

    async def send_photo(chat_id, photo, caption=None, **kw):
        if photo == "https://dead/1.jpg":
            raise BadRequest("wrong file identifier/HTTP URL specified")
        return await bot._send_media("send_photo", chat_id, photo, caption, **kw)
    bot.send_photo = send_photo
    scripted(svc, [[pic("https://dead/1.jpg"), pic("https://ok/2.jpg")]])
    out = await t_find_photo(ctx(svc, bot), {"query": "에펠탑"})
    ph = photos(bot)
    assert len(ph) == 1 and ph[0][2] == "https://ok/2.jpg", (ph, out)    # 죽은 1번 건너뛰고 2번 올림


@test
async def no_results_asks_to_retry_not_claims():
    db, svc, bot = await world()
    scripted(svc, [[]])                                                 # 아무것도 못 찾음
    c = ctx(svc, bot)
    out = await t_find_photo(c, {"query": "없는거12345"})
    assert not photos(bot) and c.quiet is False and "못 찾" in out, out  # 못 찾으면 '올렸다' 안 함
    day = _dt.datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")
    assert await db.counter(day, 0, f"photo:{MEM.id}") == 0              # 못 찾으면 한도 안 셈


if __name__ == "__main__":
    asyncio.run(run_all())
