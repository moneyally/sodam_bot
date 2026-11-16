"""🏷 화이트라벨 클론봇 (sodam/clone.py): python tests/run_all.py clone

구매자가 BotFather 로 만든 봇 토큰을 받아 별도 .env + 별도 DB 로 돌린다.
봇 이름·설명·프사·영상프사는 봇 API 로 적용(각 항목 따로, 하나 실패해도 나머지), @아이디는 BotFather 것.
"""
import asyncio
import os
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace

from fakes import make_db, runner

from telegram.error import TelegramError

from sodam import clone
from sodam.clone import CloneError

test, run_all = runner()


class FakeTGError(TelegramError):
    """진짜 TelegramError 하위라 clone.py 의 except TelegramError 가 그대로 잡는다."""


class FakeBotMe:
    """clone.fetch_bot_info / apply_branding 에 주입할 가짜 봇. 한 토큰당 한 인스턴스를 재사용."""

    def __init__(self, me=None, fail=None):
        self._me = me or SimpleNamespace(id=777, username="tenka_bot", first_name="텐카", is_bot=True)
        self.fail = set(fail or ())
        self.calls: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_me(self):
        if "get_me" in self.fail:
            raise FakeTGError("Unauthorized")
        return self._me

    async def set_my_name(self, name=None, **k):
        self.calls.append(("name", name))
        if "name" in self.fail:
            raise FakeTGError("flood")

    async def set_my_description(self, description=None, **k):
        self.calls.append(("desc", description))
        if "desc" in self.fail:
            raise FakeTGError("bad")

    async def set_my_short_description(self, short_description=None, **k):
        self.calls.append(("short", short_description))

    async def set_my_profile_photo(self, photo, **k):
        self.calls.append(("photo", type(photo).__name__))
        if "photo" in self.fail:
            raise FakeTGError("photo bad")


def _factory(bot):
    """make_bot=... 에 넣을, 토큰 무시하고 항상 같은 가짜 봇을 주는 팩토리."""
    return lambda token: bot


# ── 토큰 모양 ────────────────────────────────────────────────
@test
async def parse_token_accepts_real_rejects_junk():
    good = "123456789:AAH" + "x" * 32
    assert clone.parse_token(good) == good
    assert clone.parse_token("  `" + good + "`  ") == good     # 코드블록·공백 제거
    for bad in ("", "그냥 말", "123:short", "nocolon", "abc:" + "x" * 35):
        try:
            clone.parse_token(bad)
            assert False, f"거절했어야: {bad!r}"
        except CloneError:
            pass


# ── 봇 정보 조회 ──────────────────────────────────────────────
@test
async def fetch_bot_info_reads_getme():
    bot = FakeBotMe()
    info = await clone.fetch_bot_info("123:tok", make_bot=_factory(bot))
    assert info.bot_id == 777 and info.username == "tenka_bot" and info.name == "텐카"


@test
async def fetch_bot_info_bad_token_raises():
    bot = FakeBotMe(fail={"get_me"})
    try:
        await clone.fetch_bot_info("123:tok", make_bot=_factory(bot))
        assert False
    except CloneError:
        pass


@test
async def fetch_bot_info_rejects_user_account():
    bot = FakeBotMe(me=SimpleNamespace(id=5, username="someone", first_name="사람", is_bot=False))
    try:
        await clone.fetch_bot_info("123:tok", make_bot=_factory(bot))
        assert False
    except CloneError:
        pass


# ── 브랜딩 적용 (각 항목 따로, 하나 실패해도 나머지) ──────────────
@test
async def apply_branding_sets_name_desc_photo():
    bot = FakeBotMe()
    errs = await clone.apply_branding("123:tok", name="텐카", description="설명", short_desc="짧은",
                                      photo=b"\xff\xd8jpeg", make_bot=_factory(bot))
    assert errs == []
    kinds = [c[0] for c in bot.calls]
    assert "name" in kinds and "desc" in kinds and "short" in kinds and "photo" in kinds
    # 정적 사진이면 Static
    assert ("photo", "InputProfilePhotoStatic") in bot.calls


@test
async def apply_branding_video_uses_animated():
    bot = FakeBotMe()
    errs = await clone.apply_branding("123:tok", video=b"\x00mp4", main_frame=1.0, make_bot=_factory(bot))
    assert errs == []
    assert ("photo", "InputProfilePhotoAnimated") in bot.calls


@test
async def apply_branding_video_wins_over_photo():
    # 영상이 있으면 정적 사진은 안 보냄 (영상프사 우선)
    bot = FakeBotMe()
    await clone.apply_branding("123:tok", photo=b"jpg", video=b"mp4", make_bot=_factory(bot))
    assert ("photo", "InputProfilePhotoAnimated") in bot.calls
    assert ("photo", "InputProfilePhotoStatic") not in bot.calls


@test
async def apply_branding_one_failure_keeps_rest():
    bot = FakeBotMe(fail={"name"})
    errs = await clone.apply_branding("123:tok", name="텐카", description="설명", make_bot=_factory(bot))
    assert len(errs) == 1 and "이름" in errs[0]
    assert ("desc", "설명") in bot.calls          # 이름 실패해도 설명은 적용됨


# ── slug / 포트 ──────────────────────────────────────────────
@test
async def slugify_cleans_username():
    assert clone.slugify("@Tenka_Bot") == "tenkabot"
    assert clone.slugify("텐카봇") == ""           # 한글만이면 slug 못 만듦 → clone番호로


@test
async def next_slug_and_port_avoid_collision():
    db = await make_db()
    info = clone.BotInfo(bot_id=1, username="tenka_bot", name="텐카")
    assert await clone.next_slug(db, "tenka_bot") == "tenkabot"
    await clone.register(db, slug="tenkabot", info=info, name="텐카", port=8791)
    # 같은 username 두 번째 → clone1
    assert await clone.next_slug(db, "tenka_bot") == "clone1"
    # 포트는 안 겹치게 다음 값
    assert await clone.next_port(db) == 8792


# ── .env 파일 생성 (공유키 복사 + 전용값 분리 + 600) ──────────────
@test
async def write_env_copies_shared_keeps_private():
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "sodam.db")
    base = Path(tmp) / "base.env"
    base.write_text(
        "TELEGRAM_BOT_TOKEN=111:MAIN\nOWNER_IDS=1\nDB_PATH=data/sodam.db\n"
        "OPENAI_API_KEY=sk-shared-xyz\nOPENAI_MODEL=gpt-x\nPAY_ADDRESS=TmainAddr\n",
        encoding="utf-8")
    path = clone.write_env(db_path, "tenkabot", "222:CLONE", {1, 999}, 8791,
                           bot_name="텐카", buyer_id=999, base_env=str(base))
    env = clone._read_env(path)
    # 전용값 = 구매자 것 (본체 토큰·오너·DB 가 새지 않음)
    assert env["TELEGRAM_BOT_TOKEN"] == "222:CLONE"
    assert env["DB_PATH"] == "data/clones/tenkabot/sodam.db"
    assert env["OWNER_IDS"] == "1,999"
    assert env["BOT_NAME"] == "텐카"
    assert env["MINIAPP_PORT"] == "8791"
    assert "PAY_ADDRESS" not in env               # 본체 결제주소는 복사 안 함 (NEVER_SHARE)
    # 공유키는 복사됨 (AI 가 돌아가야 하니까)
    assert env["OPENAI_API_KEY"] == "sk-shared-xyz"
    assert env["OPENAI_MODEL"] == "gpt-x"
    # 권한 600
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    # DB 폴더도 만들어짐
    assert (clone.clones_dir(db_path) / "tenkabot").is_dir()


@test
async def write_env_rejects_bad_slug():
    tmp = tempfile.mkdtemp()
    try:
        clone.write_env(os.path.join(tmp, "s.db"), "BAD SLUG!", "1:t", {1}, 8791, bot_name="x")
        assert False
    except CloneError:
        pass


# ── DB 등록/목록/상태/삭제 ─────────────────────────────────────
@test
async def register_list_status_unregister():
    db = await make_db()
    info = clone.BotInfo(bot_id=42, username="tenka_bot", name="텐카")
    await clone.register(db, slug="tenkabot", info=info, name="텐카", port=8791, buyer_id=999)
    rows = await clone.list_clones(db)
    assert len(rows) == 1 and rows[0]["status"] == "pending" and rows[0]["buyer_id"] == 999
    assert (await clone.by_bot_id(db, 42))["slug"] == "tenkabot"
    await clone.set_status(db, "tenkabot", "active")
    assert (await clone.get_clone(db, "tenkabot"))["status"] == "active"
    await clone.unregister(db, "tenkabot")
    assert await clone.list_clones(db) == []


@test
async def remove_env_deletes_file_keeps_db_dir():
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "sodam.db")
    clone.write_env(db_path, "tenkabot", "222:CLONE", {1}, 8791, bot_name="텐카")
    assert clone.env_path(db_path, "tenkabot").exists()
    clone.remove_env(db_path, "tenkabot")
    assert not clone.env_path(db_path, "tenkabot").exists()
    assert (clone.clones_dir(db_path) / "tenkabot").is_dir()   # DB 폴더(기록)는 남김


if __name__ == "__main__":
    asyncio.run(run_all())
