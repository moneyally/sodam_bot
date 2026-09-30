"""🔑 오너가 1:1 에 붙인 API 키 저장 (sodam/apikeys.py): python tests/run_all.py apikeys

실제 사례 2026-09-30: 오너가 xAI 키를 받아 "서버 .env 에 넣어줘" — SSH 없이 폰으로 넣게.
키 메시지는 늘 지우고, 기록(messages)·AI 로 안 샘. 오너만 저장. 방에 붙인 키도 지움.
"""
import dataclasses
import os
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace

from fake_llm import Room
from fakes import FakeBot, FakeJobQueue, FakeMsg, fake_user, make_db, make_svc, runner

from sodam import apikeys, handlers

test, run_all = runner()
OWNER, OTHER = fake_user(1, "오너"), fake_user(2, "남")
XAI = "xai-" + "A1b2C3d4" * 6


async def setup():
    db = await make_db()
    svc = await make_svc(db)
    tmp = tempfile.mkdtemp()
    svc.cfg = dataclasses.replace(svc.cfg, db_path=os.path.join(tmp, "sodam.db"))
    svc.perms.owner_ids = {1}
    bot = FakeBot()
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(), bot_data={"svc": svc})

    async def dm(user, text):
        m = FakeMsg(user.id, user, text)
        await handlers.on_private(SimpleNamespace(message=m), ctx)
        return m
    return db, svc, bot, dm, Path(tmp) / "keys.env"


def sent(bot, chat):
    return [c[2] for c in bot.named("send_message") if c[1] == chat]


@test
async def owner_bare_key_is_saved_deleted_and_never_logged():
    db, svc, bot, dm, path = await setup()
    os.environ.pop("XAI_API_KEY", None)
    m = await dm(OWNER, XAI)
    assert m.deleted and path.exists() and f"XAI_API_KEY={XAI}" in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert os.environ["XAI_API_KEY"] == XAI, "재시작 없이 바로"
    reply = sent(bot, OWNER.id)[-1]
    assert "저장했어요" in reply and XAI not in reply and XAI[-4:] in reply
    assert not await db._all("SELECT * FROM messages WHERE text LIKE '%xai-%'"), "기록에 안 남음"
    await dm(OWNER, ".키 XAI_API_KEY " + XAI[:-4] + "ZZZZ")      # 바꾸면 한 줄로 교체
    assert path.read_text().count("XAI_API_KEY=") == 1 and path.read_text().strip().endswith("ZZZZ")
    os.environ.pop("XAI_API_KEY", None)


@test
async def non_owner_and_bad_names_are_deleted_not_saved():
    db, svc, bot, dm, path = await setup()
    m = await dm(OTHER, XAI)
    assert m.deleted and not path.exists() and "운영자만" in sent(bot, OTHER.id)[-1]
    m = await dm(OWNER, ".키 TELEGRAM_BOT_TOKEN 123:abc")                # 아무 환경변수나 못 바꿈
    assert m.deleted and not path.exists() and "안 맞아서" in sent(bot, OWNER.id)[-1]
    m = await dm(OWNER, "sk-" + "x" * 30)                                # 모르는 모양도 기록 전에 지움
    assert m.deleted and not path.exists()
    assert apikeys.detect("오늘 날씨 어때") is None and apikeys.detect("xai 라는 회사 알아?") is None
    # 2026-09-30 오너 복붙 실패: 앞뒤 말·코드블록·.env 줄 모양이어도 키를 알아봄
    for t in (f"키 {XAI}", f"`{XAI}`", f"XAI_API_KEY={XAI}", f"그록꺼 {XAI} 넣어줘"):
        assert apikeys.detect(t) == ("XAI_API_KEY", XAI), t
    assert apikeys.detect("x=1") is None and apikeys.detect("a = b") is None, "평범한 말은 통과"
    assert apikeys.detect("TELEGRAM_BOT_TOKEN=1:abc") == ("TELEGRAM_BOT_TOKEN", None), "허용 안 된 이름은 저장 X (지우기만)"


@test
async def key_pasted_in_a_group_is_deleted_before_logging():
    r = await Room().open(admins={1})
    m = await r.say(fake_user(5, "멤버"), "이거 써봐 " + XAI)
    assert m.deleted and not await r.db._all("SELECT * FROM messages WHERE text LIKE '%xai-%'")
    assert any("지웠어요" in c[2] for c in r.bot.named("send_message"))


@test
async def config_loads_saved_keys_over_env():
    from sodam import config
    tmp = Path(tempfile.mkdtemp())
    (tmp / "keys.env").write_text("XAI_API_KEY=from_keys\n")
    old = {k: os.environ.get(k) for k in ("DB_PATH", "XAI_API_KEY", "TELEGRAM_BOT_TOKEN", "SODAM_ENV")}
    os.environ.update(DB_PATH=str(tmp / "sodam.db"), XAI_API_KEY="from_env", TELEGRAM_BOT_TOKEN="1:x",
                      SODAM_ENV=str(tmp / "none.env"))
    try:
        config.load_config()
        assert os.environ["XAI_API_KEY"] == "from_keys"
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


if __name__ == "__main__":
    run_all()
