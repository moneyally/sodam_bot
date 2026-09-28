"""✍️ 글 편집기 (sodam/composer.py · panels/composer.py): HTML 허용 목록 · 서식 메시지 → HTML · URL 버튼 만들기
(8줄×3개·https/tg) · 미리보기 = 실제 모양 · 연타해도 한 번 게시 · 재시작 뒤 이어 쓰기 · 올린 글 고치기."""
import asyncio
import datetime
import json
import sys

from channel_world import BOSS, CH, SUB, World
from fakes import FakeMsg, fake_user, make_svc, runner
from harness import html_errors
from telegram import Chat, Message, MessageEntity as E

from sodam import composer
from sodam.composer import HtmlError, check_url, clean_html

test, run_all = runner()


def bad(src: str, limit: int = 4096) -> str:
    try:
        clean_html(src, limit)
    except HtmlError as e:
        return str(e)
    raise AssertionError(f"통과하면 안 됨: {src!r}")


@test
def html_allow_list():
    ok = lambda s: clean_html(s)[0]  # noqa: E731
    assert ok("<b>굵게 <i>기울임 <u>밑줄</u></i></b>") == "<b>굵게 <i>기울임 <u>밑줄</u></i></b>"          # 중첩
    assert ok("<strong>a</strong><em>b</em><del>c</del>") == "<b>a</b><i>b</i><s>c</s>"                     # 같은 뜻 태그
    assert clean_html("<div>안녕<span>하</span></div>x<br>y") == ("안녕하x\ny", ["div", "span"])              # 모르는 태그 = 글자만
    assert ok("3 < 5 & 2 > 1 &amp; &copy;") == "3 &lt; 5 &amp; 2 &gt; 1 &amp; &amp;copy;"                  # 짝 없는 기호
    assert ok('<b onclick="x()">a</b><a href=\'https://x.com/?a=1&b=2\' target=_blank>링크</a>') == \
        '<b>a</b><a href="https://x.com/?a=1&amp;b=2">링크</a>'                                              # 속성 정리
    assert ok('<pre><code class="py">x</code></pre>') == '<pre><code class="language-py">x</code></pre>'
    assert ok('<span class="tg-spoiler">숨김</span><tg-spoiler>숨김</tg-spoiler>') == "<tg-spoiler>숨김</tg-spoiler>" * 2
    assert ok("<blockquote expandable>긴 인용</blockquote>") == "<blockquote expandable>긴 인용</blockquote>"
    assert ok('<a href="tg://user?id=5">멘션</a>') == '<a href="tg://user?id=5">멘션</a>'
    assert "1번째 줄 1번째 글자" in bad("<b>열림") and "닫히지" in bad("<b>열림")                              # 짝 안 맞음 + 위치
    assert "2번째 줄" in bad("첫 줄\n<b><i>x</b></i>")                                                       # 엇갈림
    assert "열지 않은" in bad("x</b>")
    for evil in ('<a href="javascript:alert(1)">x</a>', '<a href=" JavaScript:alert(1)">x</a>', "<a>x</a>",
                 '<a href="data:text/html,x">x</a>', '<a href="https://a.com x">x</a>'):
        assert "링크 주소" in bad(evil), evil                                                                   # 위험한 링크는 거절
    assert "코드 칸" in bad("<pre><b>x</b></pre>") and "또 넣을" in bad('<a href="https://a.io"><a href="https://b.io">x</a></a>')
    assert "너무 길어요" in bad("가" * 4097) and clean_html("가" * 4096)
    assert "1024" in bad("😀" * 513, composer.CAPTION_LIMIT)                                                # 텔레그램은 UTF-16 로 셈
    assert bad('<a href="https://a.com/?q=<>">q</a>')                                                      # 속성 속 날 < 도 거절
    for src in ("<b>a<i>b</i></b> & <x>", '<a href="https://a.com/?q=&lt;&quot;">q</a>', "<div><b>x</b></div>"):
        assert not html_errors(clean_html(src)[0]), src                                                       # 결과는 항상 올바른 HTML


@test
def formatted_message_to_html():
    t = "굵게 링크 인용 숨김 <x>&"
    m = Message(1, datetime.datetime.now(), Chat(1, "private"), text=t,
                entities=[E("bold", 0, 2), E("text_link", 3, 2, url="https://b.com/?a=1&b=2"), E("blockquote", 6, 2),
                          E("spoiler", 9, 2)])
    body, dropped = clean_html(composer.msg_html(m))
    assert body == ('<b>굵게</b> <a href="https://b.com/?a=1&amp;b=2">링크</a> <blockquote>인용</blockquote> '
                    "<tg-spoiler>숨김</tg-spoiler> &lt;x&gt;&amp;") and not dropped
    m = Message(1, datetime.datetime.now(), Chat(1, "private"), text="print(1)", entities=[E("pre", 0, 8, language="python")])
    assert clean_html(composer.msg_html(m))[0] == '<pre><code class="language-python">print(1)</code></pre>'
    raw = Message(1, datetime.datetime.now(), Chat(1, "private"), text="<b>직접</b> https://a.com", entities=[E("url", 11, 13)])
    assert composer.msg_html(raw) == "<b>직접</b> https://a.com"          # 서식 없는 글(주소 자동 링크만) = 직접 쓴 HTML
    cap = Message(1, datetime.datetime.now(), Chat(1, "private"), caption="사진 설명", caption_entities=[E("italic", 0, 2)])
    assert composer.msg_html(cap) == "<i>사진</i> 설명"


@test
def button_urls():
    assert check_url("https://t.me/boss") == "https://t.me/boss" and check_url("tg://resolve?domain=x")
    assert check_url("naver.com") == "https://naver.com" and check_url("t.me/boss") == "https://t.me/boss"
    for u in ("http://a.com", "javascript:alert(1)", "https://", "https://a b.com", "ftp://a.com", "", "https://nodot",
              "https://" + "a" * 600 + ".com"):
        assert check_url(u) is None, u


@test
async def body_input_and_formatted_message():
    w = await World().open()
    did = await w.draft()
    q = await w.press(BOSS, f"m:cp:{did}:b")
    assert "본문" in q.edits[-1] and BOSS in w.svc.inputs
    m = await w.say(BOSS, "<b>열림")                                        # 깨진 HTML → 위치와 함께 거절, 입력은 계속
    assert "1번째 줄" in m.replies[-1] and BOSS in w.svc.inputs
    assert (await composer.get(w.db, did))["body"] == ""

    class Formatted(FakeMsg):                                             # 텔레그램 서식을 넣어 보낸 메시지
        pass
    f = Formatted(BOSS, fake_user(BOSS), "공지 안내")
    f.entities, f.text_html, f.forward_origin = (E("bold", 0, 2),), "<b>공지</b> 안내 <script>", None
    from types import SimpleNamespace
    from sodam import handlers
    await handlers.on_private(SimpleNamespace(message=f), w.ctx)
    d = await composer.get(w.db, did)
    assert d["body"] == "<b>공지</b> 안내" and "본문을 넣었어요" in f.replies[-1] and "script" in f.replies[-1]
    assert BOSS not in w.svc.inputs
    # 사진 + 설명 = 둘 다 한 번에, 설명 한도 1024
    await w.press(BOSS, f"m:cp:{did}:b")
    photo = await w.say(BOSS, None, caption="가" * 1100, photo=[SimpleNamespace(file_id="P1")])
    assert "1024" in photo.replies[-1]
    await w.say(BOSS, None, caption="<i>사진</i> 설명", photo=[SimpleNamespace(file_id="P1")])
    d = await composer.get(w.db, did)
    assert (d["media_type"], d["media_id"], d["body"]) == ("photo", "P1", "<i>사진</i> 설명")


@test
async def button_builder_limits_and_validation():
    w = await World().open()
    did = await w.draft()
    await w.press(BOSS, f"m:cp:{did}:ba")
    await w.say(BOSS, "📞 문의하기")
    assert w.svc.inputs[BOSS].kind == "cpu"                                # 글자 → 주소 입력으로 이어짐
    m = await w.say(BOSS, "javascript:alert(1)")
    assert "https://" in m.replies[-1] and w.svc.inputs[BOSS].kind == "cpu"   # 위험한 주소 거절, 다시 받기
    m = await w.say(BOSS, "t.me/boss")
    assert "어느 줄" in m.replies[-1]
    q = await w.press(BOSS, f"m:cp:{did}:br:n")
    assert composer.buttons(await composer.get(w.db, did)) == [[{"t": "📞 문의하기", "u": "https://t.me/boss"}]]
    q2 = await w.press(BOSS, f"m:cp:{did}:br:n")                           # 같은 버튼 연타 → 두 번 안 들어감
    assert len(composer.buttons(await composer.get(w.db, did))) == 1 and q2.answers
    await w.press(BOSS, f"m:cp:{did}:ba")
    await w.say(BOSS, "사이트")
    await w.say(BOSS, "https://example.com")
    await asyncio.gather(*[w.press(BOSS, f"m:cp:{did}:br:0") for _ in range(3)])   # 동시에 눌러도 한 번
    assert [b["t"] for b in composer.buttons(await composer.get(w.db, did))[0]] == ["📞 문의하기", "사이트"]
    rev = (await composer.get(w.db, did))["rev"]                          # 같은 판(rev)으로 두 번 → 두 번째는 적용 안 됨
    assert await composer.update(w.db, did, rev, silent=1) and not await composer.update(w.db, did, rev, silent=0)
    assert (await composer.get(w.db, did))["silent"] == 1
    full = [[{"t": f"{r}{c}", "u": "https://a.com"} for c in range(3)] for r in range(8)]
    await composer.update(w.db, did, buttons=json.dumps(full))
    q = await w.press(BOSS, f"m:cp:{did}:ba")
    assert "8줄 × 3개" in q.answers[0][0] and q.answers[0][1] and BOSS not in w.svc.inputs
    # 버튼 정리: 옮기기·지우기, 옛 화면(rev 다름)은 다시 그림
    await composer.update(w.db, did, buttons=json.dumps([[{"t": "A", "u": "https://a.com"}, {"t": "B", "u": "https://b.com"}]]))
    rev = (await composer.get(w.db, did))["rev"]
    await w.press(BOSS, f"m:cp:{did}:bo:{rev}:0:1:l")
    assert [b["t"] for b in composer.buttons(await composer.get(w.db, did))[0]] == ["B", "A"]
    q = await w.press(BOSS, f"m:cp:{did}:bo:{rev}:0:0:x")                  # 옛 rev → 아무것도 안 지움
    assert "화면이 바뀌어서" in q.edits[-1] and len(composer.buttons(await composer.get(w.db, did))[0]) == 2
    rev += 1
    await w.press(BOSS, f"m:cp:{did}:bo:{rev}:0:0:d")                      # 아랫줄(새 줄)로
    assert [[b["t"] for b in r] for r in composer.buttons(await composer.get(w.db, did))] == [["A"], ["B"]]
    await w.press(BOSS, f"m:cp:{did}:bo:{rev + 1}:1:0:x")
    assert composer.buttons(await composer.get(w.db, did)) == [[{"t": "A", "u": "https://a.com"}]]


@test
async def preview_is_exact_and_post_once():
    w = await World().open()
    did = await w.draft()
    await composer.update(w.db, did, body="<b>공지</b>", silent=1,
                          buttons=json.dumps([[{"t": "문의", "u": "https://t.me/boss"}]]))
    q = await w.press(BOSS, f"m:cp:{did}:pr")
    prev = [c for c in w.sent_to(BOSS) if c[2] == "<b>공지</b>"]
    assert prev and prev[0][3]["reply_markup"].inline_keyboard[0][0].url == "https://t.me/boss" and "미리보기" in q.answers[0][0]
    q = await w.press(BOSS, f"m:cp:{did}:go")                             # 확인 화면만, 아직 안 올림
    assert "올릴까요" in q.edits[-1] and not w.sent_to(CH)
    qs = await asyncio.gather(*[w.press(BOSS, f"m:cp:{did}:go1") for _ in range(3)])   # 동시에 세 번
    posts = w.sent_to(CH)
    assert len(posts) == 1 and posts[0][2] == "<b>공지</b>" and posts[0][3]["disable_notification"] is True
    assert posts[0][3]["parse_mode"] == "HTML" and posts[0][3]["reply_markup"].inline_keyboard[0][0].text == "문의"
    assert sum("올렸어요" in (q.edits[-1] if q.edits else "") for q in qs) == 1
    assert (await composer.get(w.db, did))["state"] == "posted"
    row = await w.db._one("SELECT * FROM channel_msgs WHERE chat_id=?", (CH,))
    assert row["text"] == "공지" and json.loads(row["draft"])["body"] == "<b>공지</b>"
    await w.press(BOSS, f"m:cp:{did}:go1")                                  # 나중에 옛 버튼을 또 눌러도
    assert len(w.sent_to(CH)) == 1


@test
async def restart_mid_draft_and_other_admin():
    w = await World().open()
    did = await w.draft()
    await w.press(BOSS, f"m:cp:{did}:b")
    await w.say(BOSS, "<i>쓰던 글</i>")
    w.svc = await make_svc(w.db)                                          # 재시작: 메모리(입력 대기·캐시) 전부 새로
    from sodam.permissions import Permissions
    w.svc.perms = Permissions(w.svc.cfg, w.db)
    w.ctx.bot_data["svc"] = w.svc
    q = await w.press(BOSS, f"m:ch:{CH}")
    assert any(b.text == "📝 쓰던 글 이어서" for r in q.kb.inline_keyboard for b in r)
    q = await w.press(BOSS, f"m:cp:{did}")
    assert "쓰던 글" in q.edits[-1]
    q = await w.press(SUB, f"m:cp:{did}:go1")                              # 같은 채널 다른 관리자도 남의 초안은 못 올림
    assert q.answers[0][1] and not q.edits and not w.sent_to(CH)
    await w.press(BOSS, f"m:cp:{did}:go1")
    assert [c[2] for c in w.sent_to(CH)] == ["<i>쓰던 글</i>"]


@test
async def edit_posted_message():
    w = await World().open()
    did = await w.draft()
    await composer.update(w.db, did, body="처음 글")
    await w.press(BOSS, f"m:cp:{did}:go1")
    msg_id = (await w.db._one("SELECT msg_id FROM channel_msgs"))["msg_id"]
    seen = []
    orig = w.bot.edit_message_text

    async def spy(text, chat_id=None, message_id=None, **kw):
        seen.append(message_id)
        return await orig(text, chat_id=chat_id, message_id=message_id, **kw)
    w.bot.edit_message_text = spy
    q = await w.press(BOSS, f"m:chp:{CH}")
    assert f"m:chpe:{CH}:{msg_id}" in [b.callback_data for r in q.kb.inline_keyboard for b in r]
    q = await w.press(BOSS, f"m:chpe:{CH}:{msg_id}")
    edit_id = int(q.kb.inline_keyboard[0][0].callback_data.split(":")[2])
    assert edit_id != did and "고치기" in q.edits[-1] and "지금 올리기" not in str(q.kb)
    await w.press(BOSS, f"m:cp:{edit_id}:b")
    await w.say(BOSS, "<b>고친 글</b>")
    await w.press(BOSS, f"m:cp:{edit_id}:ba")
    await w.say(BOSS, "사이트")
    await w.say(BOSS, "https://example.com")
    await w.press(BOSS, f"m:cp:{edit_id}:br:0")
    await asyncio.gather(w.press(BOSS, f"m:cp:{edit_id}:sv"), w.press(BOSS, f"m:cp:{edit_id}:sv"))
    edits = w.bot.named("edit_text")
    assert len(edits) == 1 and edits[0][1] == CH and edits[0][2] == "<b>고친 글</b>"
    assert seen == [msg_id] and edits[0][3]["reply_markup"].inline_keyboard[0][0].url == "https://example.com"
    row = await w.db._one("SELECT * FROM channel_msgs WHERE msg_id=?", (msg_id,))
    assert row["text"] == "고친 글" and json.loads(row["draft"])["body"] == "<b>고친 글</b>"
    assert len(w.sent_to(CH)) == 1                                        # 고치기는 새 글을 안 올림


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
