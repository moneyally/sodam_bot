"""'누구 얘기인지' 판단 평가: 실제 OpenAI 로 30여 개 실전 상황을 돌려 정확도를 잰다.

    python tools/ai_eval_addressee.py            # 전체
    python tools/ai_eval_addressee.py 3 7        # 3·7번만
    python tools/ai_eval_addressee.py --repeat 2 # 같은 상황을 여러 번 (모델 답이 매번 달라서)

판정:
- mention: 멘션해야 할 사람 집합이 정확히 맞아야 통과
- clarify: 멘션 없이 되묻기(? 포함) 또는 멘션 없이 전체 인사 → 통과 (엉뚱한 사람 멘션은 실패)
- general: 특정인 멘션 없이 방 전체에게
- 모든 경우: '엉뚱한 사람 멘션' 수를 따로 센다 (목표 0)
"""
import asyncio
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

from fake_llm import Room, fast_timers, restore_timers  # noqa: E402
from fakes import close_open_dbs, fake_user  # noqa: E402

from sodam import handlers  # noqa: E402
from sodam.config import load_config  # noqa: E402
from sodam.llm import LLM  # noqa: E402

BOSS = fake_user(1, "방장", "boss")
P = {  # 등장인물
    "하나": fake_user(101, "하나", "hana_k"),
    "퍼스트": fake_user(102, "퍼스트코인 (구 바람코인)", "firstcoin"),
    "하늘": fake_user(103, "하늘코인 거래소", "sky_trade"),
    "김철수": fake_user(104, "김철수", "kimcs"),
    "김영희": fake_user(105, "김영희", "kimyh"),
    "박대표": fake_user(106, "박준호", "junho"),
    "새내기": fake_user(107, "새내기", "newbie"),
    "신입2": fake_user(108, "이서연", "seoyeon"),
    "민지": fake_user(109, "김민지", "minji"),
}


@dataclass
class Case:
    title: str
    request: str
    expect: str                          # mention / clarify / general / text
    want: set = field(default_factory=set)       # mention: 정확히 이 사람들
    reply_to: str | None = None          # 이 사람 메시지에 답장
    reply_to_bot: bool = False
    joins: dict = field(default_factory=dict)    # 이름 → 몇 분 전 입장
    spoke: dict = field(default_factory=dict)    # 이름 → 몇 분 전 말함
    caller_admin: bool = True
    style_room: str | None = None        # 방 말투가 이걸로 바뀌어야
    style_self: str | None = None        # 말한 사람 개인 말투가 이걸로
    text_has: str | None = None          # 답에 이 글자가 있어야 (text 판정)
    title_room: str = "대표님 소통방"


CASES = [
    Case("답장 + 대표님 인사드려", "소담아 대표님 인사드려", "mention", {"하나"}, reply_to="하나", spoke={"퍼스트": 5}),
    Case("답장 + 이분 인사", "소담아 이분께 인사 좀 드려", "mention", {"하나"}, reply_to="하나"),
    Case("호칭으로 부름 (하늘대표님)", "소담아 하늘대표님 인사드려", "mention", {"하늘"}, spoke={"김철수": 2}),
    Case("방 이름과 비슷한 이름 (FIRST 방)", "소담아 퍼스트대표님 인사드려", "mention", {"퍼스트"}, title_room="FIRST"),
    Case("새로 온 분 (1명)", "소담아 새로 오신 분 인사드려", "mention", {"새내기"}, joins={"새내기": 3}, spoke={"하나": 1}),
    Case("새로 온 분 (2명)", "소담아 새로 오신 분들 환영해드려", "mention", {"새내기", "신입2"}, joins={"새내기": 5, "신입2": 2}),
    Case("새로 온 사람 없음", "소담아 새로 오신 분 인사드려", "general", spoke={"하나": 3}),
    Case("대표님만 (단서 없음)", "소담아 대표님 인사드려", "clarify"),
    Case("대표님만 (최근 말한 사람 2명)", "소담아 대표님 인사드려", "clarify", spoke={"하나": 2, "퍼스트": 4}),
    Case("@태그", "소담아 @hana_k 님한테 인사해줘", "mention", {"하나"}),
    Case("두 사람 이름", "소담아 김철수 대표님이랑 박준호 대표님 인사드려", "mention", {"김철수", "박대표"}),
    Case("같은 성 두 명 (김대표)", "소담아 김대표님 인사드려", "clarify", spoke={"김철수": 3, "김영희": 5}),
    Case("방 전체 인사", "소담아 방사람들한테 인사드려", "general", spoke={"하나": 2}),
    Case("답장 + 방 전체", "소담아 다들 인사드려", "general", reply_to="하나"),
    Case("봇 답장에 고맙다", "고마워 소담아", "general", reply_to_bot=True),
    Case("저분 누구셔 (답장)", "소담아 저분 누구셔?", "text", reply_to="하나", text_has="하나"),
    Case("방장 누구야", "소담아 이 방 관리자 누구야?", "text", text_has="방장"),
    Case("인사 + 말투 (관리자)", "소담아 대표님 인사드려 .말투 여친", "mention", {"하나"}, reply_to="하나",
         style_room="girlfriend"),
    Case("말투만 (관리자, 문장)", "소담아 이 방 말투 자유분방으로 바꿔줘", "general", style_room="free"),
    Case("말투 (일반 멤버)", "소담아 나한테는 츤데레 말투로 해줘", "general", caller_admin=False, style_self="tsundere"),
    Case("새내기 + 답장은 다른 사람", "소담아 새로 오신 분 환영해드려", "mention", {"새내기"}, reply_to="하나",
         joins={"새내기": 2}),
    Case("이름 일부 (민지)", "소담아 민지 대표님 인사드려", "mention", {"민지"}, spoke={"김철수": 1}),
    Case("없는 사람 이름", "소담아 홍길동 대표님 인사드려", "clarify", spoke={"하나": 1}),
    Case("아침 인사 요청", "소담아 좋은 아침 인사 한마디", "general", spoke={"하나": 1, "김철수": 2}),
    Case("멘션 없이 칭찬 (답장)", "소담아 이분 말 잘했지?", "text", reply_to="하나", text_has=""),
    Case("답장 + 축하", "소담아 축하해드려", "mention", {"하나"}, reply_to="하나"),
    Case("입장 오래전 (어제)", "소담아 새로 오신 분 인사드려", "general", joins={"새내기": 60 * 26}),
    Case("태그 + 다른 사람 답장", "소담아 @firstcoin 대표님께 인사드려", "mention", {"퍼스트"}, reply_to="하나"),
    Case("방금 말한 한 명 + 대표님", "소담아 대표님께 인사 좀", "clarify", spoke={"하나": 1}),
    Case("이름 부분 + 호칭 (철수형)", "소담아 철수형한테 인사해", "mention", {"김철수"}),
    Case("영어 아이디로", "소담아 junho 대표님 인사드려", "mention", {"박대표"}),
    Case("두 명 중 한 명 이름 (김영희)", "소담아 영희 대표님 인사드려", "mention", {"김영희"}, spoke={"김철수": 1}),
]


async def run_case(i: int, c: Case) -> dict:
    r = Room()
    await r.open(admins=(BOSS.id,), settings={"captcha_enabled": False})
    cfg = load_config()
    r.svc.cfg = r.svc.cfg.__class__(**{**r.svc.cfg.__dict__, "openai_api_key": cfg.openai_api_key, "model": cfg.model,
                                       "guard_model": cfg.guard_model, "reasoning_effort": cfg.reasoning_effort})
    r.svc.llm = LLM(r.svc.cfg, r.db)
    await r.db.ensure_chat(Room.CHAT, c.title_room)
    now = int(time.time())
    for key, u in P.items():
        await r.join(u)
        joined = c.joins.get(key)
        await r.db._write("UPDATE members SET joined_at=?, last_seen=? WHERE user_id=?",
                          (now - (joined * 60 if joined else 40 * 86400), now - 3600, u.id))
    await r.join(BOSS)
    for key, minutes in c.spoke.items():
        await r.db._write("INSERT INTO messages(chat_id, user_id, msg_id, text, ts, is_bot, flagged) VALUES(?,?,?,?,?,0,0)",
                          (Room.CHAT, P[key].id, 5000 + P[key].id, "네 좋네요 ㅎㅎ", now - minutes * 60))
    caller = BOSS if c.caller_admin else P["민지"]
    reply = None
    if c.reply_to:
        reply = SimpleNamespace(from_user=P[c.reply_to], text="잘 만들었네요", caption=None, message_id=77,
                                forward_origin=None)
    elif c.reply_to_bot:
        reply = SimpleNamespace(from_user=r.bot_user(), text="방금 안내드렸어요", caption=None, message_id=78,
                                forward_origin=None)
    m = r.msg(caller, c.request, reply_to=reply)
    out: list[str] = []
    orig = m.reply_text

    async def cap(t, **kw):
        out.append(t)
        return await orig(t, **kw)
    m.reply_text = cap
    await handlers.on_group_message(SimpleNamespace(message=m), r.ctx)
    await r.settle()
    text = " ".join(out)
    mentioned = {int(x) for x in re.findall(r"tg://user\?id=(\d+)", text)}
    ids = {k: u.id for k, u in P.items()}
    want = {ids[k] for k in c.want}
    wrong = mentioned - want if c.expect == "mention" else mentioned - ({ids[c.reply_to]} if c.reply_to and c.expect == "text" else set())
    plain = re.sub(r"<[^>]+>", "", text)
    if c.expect == "mention":
        ok = mentioned == want
    elif c.expect == "clarify":
        ok = not mentioned
    elif c.expect == "general":
        ok = not mentioned
    else:
        ok = (c.text_has in plain) and not wrong
    s = await r.db.get_settings(Room.CHAT)
    if c.style_room:
        ok = ok and s["style"] == c.style_room
    if c.style_self:
        mem = await r.db.get_member(Room.CHAT, caller.id)
        ok = ok and (mem["style"] == c.style_self) and s["style"] != c.style_self
    names = {v: k for k, v in ids.items()}
    print(f"{'✅' if ok else '❌'} {i:2d}. {c.title} — 멘션 {sorted(names.get(x, x) for x in mentioned) or '없음'}"
          + (f" · 엉뚱한 멘션 {sorted(names.get(x, x) for x in wrong)}" if wrong else "")
          + (f" · 방말투 {s['style']}" if c.style_room else ""))
    print(f"      👤 {c.request}{' (↩ ' + c.reply_to + ')' if c.reply_to else ''}")
    print(f"      💬 {plain[:160]}")
    return {"ok": ok, "wrong": len(wrong)}


async def main() -> int:
    args = sys.argv[1:]
    repeat = int(args[args.index("--repeat") + 1]) if "--repeat" in args else 1
    only = {int(a) for a in args if a.isdecimal() and (not args or args[args.index(a) - 1] != "--repeat")}
    old = fast_timers()
    results = []
    t0 = time.time()
    try:
        for _ in range(repeat):
            for i, c in enumerate(CASES, 1):
                if only and i not in only:
                    continue
                try:
                    results.append(await run_case(i, c))
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    results.append({"ok": False, "wrong": 0, "err": str(e)})
                finally:
                    await close_open_dbs()
    finally:
        restore_timers(old)
    ok = sum(r["ok"] for r in results)
    wrong = sum(r["wrong"] for r in results)
    print(f"\n정확도 {ok}/{len(results)} ({ok * 100 // max(1, len(results))}%) · 엉뚱한 사람 멘션 {wrong}건 · {time.time() - t0:.0f}초")
    return 0 if wrong == 0 and ok >= len(results) * 0.95 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
