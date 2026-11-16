"""🏷 화이트라벨 클론봇 — 구매자가 BotFather 로 만든 봇(토큰)을 받아, 같은 소담 코드로
별도 .env + 별도 DB 로 돌린다 (docs/WHITELABEL_DESIGN.md). 딜러봇(SODAM_ENV=.env.dealer)이 선례.

- 보이는 이름·설명·프사·영상프사 = 봇 API(setMyName/setMyProfilePhoto, Bot API 9.4, 2026-02)로 적용.
- @아이디 = 봇 API 로 못 바꿈 → 구매자가 BotFather 에서 정한 것을 그대로 씀(마법사는 읽어서 보여주기만).
- 별도 DB_PATH 디렉터리면 오너·구독·예산·방·채널·세션·미디어·keys.env 가 전부 자동 격리.
- 켜기 = systemd 인스턴스 유닛 sodam@<slug>.service (ConditionPathExists=data/clones/<slug>.env).
  봇 프로세스는 NoNewPrivileges 라 systemctl 못 함 → root 데몬(sodam-clone-apply)이 파일을 보고 켠다.
  그 전엔 반자동(ssh systemctl enable --now sodam@<slug>).
"""
from __future__ import annotations

import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .db import register_schema

# 봇 토큰 모양: <숫자 봇ID>:<35자 안팎 비밀>  (예: 123456789:AAH...)
BOT_TOKEN_RE = re.compile(r"^(\d{6,12}):([A-Za-z0-9_-]{30,50})$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9]{0,23}$")
BASE_PORT = 8790          # 본체 미니앱 8790 → 클론은 8791 부터
MAX_NAME = 32             # setMyName 한도
MAX_DESC = 512            # setMyDescription 한도
MAX_SHORT = 120           # setMyShortDescription 한도

# 본체 .env 에서 클론 .env 로 그대로 물려줄 "공유" 설정 (OpenAI 키·모델·요금 등).
# 클론마다 달라야 하는 값(토큰·DB·오너·이름·포트·결제)은 여기 없고 아래 write_env 가 직접 쓴다.
SHARE_KEYS = (
    "OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_GUARD_MODEL", "AGENT_LIGHT_MODEL",
    "OPENAI_REASONING_EFFORT", "OPENAI_IMAGE_MODEL", "OPENAI_IMAGE_EDIT_MODEL",
    "OPENAI_IMAGE_QUALITY", "OPENAI_CACHE_RETENTION", "AGENT_THINK", "AGENT_THINK_EFFORT",
    "TRONGRID_API_KEY", "SPORTSDB_KEY", "TIMEZONE",
    "DAILY_USD_BUDGET", "DAILY_TOKEN_BUDGET",
    "SUB_PRICE_USDT", "SUB_DAYS", "FREE_AI_PER_DAY", "INVOICE_MINUTES",
)
# 공유하면 안 되는(클론마다 다르거나 안 쓰는) 키 — 혹시 SHARE_KEYS 를 넓혀도 새지 않게 이중 방어.
NEVER_SHARE = frozenset({
    "TELEGRAM_BOT_TOKEN", "DB_PATH", "OWNER_IDS", "BOT_NAME", "BOT_CALL_NAMES",
    "BOT_CALL_NAMES_EN", "MINIAPP_PORT", "MINIAPP_URL", "PAY_ADDRESS", "BOT_ROLE",
    "LOG_CHAT_ID", "SODAM_ENV", "MTPROTO_API_ID", "MTPROTO_API_HASH",
    "BACKUP_DIR", "BACKUP_SEND_TO_LOG", "TRIAL_DAYS", "BOT_SUPPORT_CONTACT",
})


class CloneError(Exception):
    """마법사에 그대로 보여줄 한국어 사유."""


@dataclass(frozen=True)
class BotInfo:
    bot_id: int
    username: str
    name: str        # first_name (표시 이름)


register_schema(
    """
CREATE TABLE IF NOT EXISTS clones (
    slug      TEXT PRIMARY KEY,
    bot_id    INTEGER NOT NULL,
    username  TEXT NOT NULL,
    name      TEXT NOT NULL,
    buyer_id  INTEGER NOT NULL DEFAULT 0,
    port      INTEGER NOT NULL,
    status    TEXT NOT NULL DEFAULT 'pending',
    created   REAL NOT NULL
);
""",
    # 🏷 clones 는 방(chat_id) 이 아니라 봇 단위(slug·bot_id) 라서 migrate_chat 대상이 아니다.
    # (migrate={"clones":"plain"} 를 주면 migrate_chat 이 없는 chat_id 컬럼을 UPDATE 하다 크래시한다)
)


# ── 토큰·브랜딩 (네트워크) ───────────────────────────────────────────

def parse_token(raw: str) -> str:
    """토큰 모양이면 그대로, 아니면 CloneError. (앞뒤 공백·코드블록 제거)"""
    tok = (raw or "").strip().strip("`").strip()
    if not BOT_TOKEN_RE.match(tok):
        raise CloneError("봇 토큰 모양이 아니야 — @BotFather 가 준 <code>123456789:AA...</code> 를 그대로 붙여줘.")
    return tok


async def fetch_bot_info(token: str, *, make_bot=None) -> BotInfo:
    """getMe 로 토큰이 진짜 봇인지 확인하고 봇 정보를 돌려준다. 실패면 CloneError."""
    from telegram import Bot
    from telegram.error import TelegramError

    bot = (make_bot or Bot)(token)
    try:
        async with bot:
            me = await bot.get_me()
    except TelegramError as e:
        raise CloneError(f"그 토큰으로 텔레그램에 물어보니 거절당했어 ({e}). 토큰이 맞는지 봐줘.") from None
    if not getattr(me, "is_bot", True):
        raise CloneError("봇 계정이 아니야 — 사람 계정 토큰은 못 써.")
    return BotInfo(bot_id=me.id, username=me.username or "", name=me.first_name or "")


async def apply_branding(token: str, *, name: str | None = None, description: str | None = None,
                         short_desc: str | None = None, photo: bytes | None = None,
                         video: bytes | None = None, main_frame: float = 0.0,
                         make_bot=None) -> list[str]:
    """봇 API 로 이름·설명·프사·영상프사 적용. 각 항목은 따로 try — 하나 실패해도 나머지는 적용.
    실패한 항목의 한국어 사유 목록을 돌려준다(빈 리스트 = 전부 성공)."""
    from telegram import Bot, InputProfilePhotoAnimated, InputProfilePhotoStatic
    from telegram.error import TelegramError

    errs: list[str] = []
    bot = (make_bot or Bot)(token)
    async with bot:
        if name:
            try:
                await bot.set_my_name(name=name[:MAX_NAME])
            except TelegramError as e:
                errs.append(f"이름: {e}")
        if description is not None:
            try:
                await bot.set_my_description(description=description[:MAX_DESC])
            except TelegramError as e:
                errs.append(f"설명: {e}")
        if short_desc is not None:
            try:
                await bot.set_my_short_description(short_description=short_desc[:MAX_SHORT])
            except TelegramError as e:
                errs.append(f"짧은 설명: {e}")
        if video is not None:
            try:
                await bot.set_my_profile_photo(
                    InputProfilePhotoAnimated(animation=video, main_frame_timestamp=main_frame))
            except TelegramError as e:
                errs.append(f"영상프사: {e}")
        elif photo is not None:
            try:
                await bot.set_my_profile_photo(InputProfilePhotoStatic(photo=photo))
            except TelegramError as e:
                errs.append(f"프사: {e}")
    return errs


# ── slug·포트·파일 경로 ─────────────────────────────────────────────

def slugify(username: str) -> str:
    """@아이디 → 파일·유닛 이름(소문자 영숫자, 24자). 못 만들면 빈 문자열."""
    s = re.sub(r"[^a-z0-9]", "", (username or "").lower())[:24]
    return s if s and SLUG_RE.match(s) else ""


def clones_dir(db_path: str) -> Path:
    return Path(db_path).resolve().parent / "clones"


def env_path(db_path: str, slug: str) -> Path:
    return clones_dir(db_path) / f"{slug}.env"


def clone_db_path(slug: str) -> str:
    """systemd 가 WorkingDirectory=/opt/sodam 에서 읽을 상대 경로."""
    return f"data/clones/{slug}/sodam.db"


def _read_env(path: Path) -> dict[str, str]:
    """간단 .env 파싱 (KEY=VALUE 줄만). dotenv 안 써도 되는 읽기용."""
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def write_env(db_path: str, slug: str, token: str, owner_ids, port: int, *,
              bot_name: str, buyer_id: int = 0, pay_address: str = "",
              trial_days: int = 3, base_env: str = ".env",
              miniapp_url: str = "") -> Path:
    """data/clones/<slug>.env (600) + data/clones/<slug>/ DB 폴더 생성. 원자적 교체.
    공유키는 본체 .env 에서 복사, 클론 전용값은 여기서 직접."""
    if not SLUG_RE.match(slug):
        raise CloneError("클론 이름(slug)은 소문자·숫자 1~24자여야 해.")
    d = clones_dir(db_path)
    (d / slug).mkdir(parents=True, exist_ok=True)   # DB 폴더(여기에 keys.env·media·세션도 격리)

    owners = ",".join(str(x) for x in sorted({int(o) for o in owner_ids}))
    shared = _read_env(Path(base_env))
    lines = [
        "# 🏷 화이트라벨 클론 — sodam/clone.py 가 만든 파일. 손으로 고치지 말 것.",
        f"# 만든 시각: {time.strftime('%Y-%m-%d %H:%M:%S')}  구매자: {buyer_id}",
        f"TELEGRAM_BOT_TOKEN={token}",
        f"DB_PATH={clone_db_path(slug)}",
        f"OWNER_IDS={owners}",
        f"BOT_NAME={bot_name}",
        "BOT_ROLE=all",
        f"MINIAPP_PORT={port}",
        f"TRIAL_DAYS={int(trial_days)}",
    ]
    if miniapp_url:
        lines.append(f"MINIAPP_URL={miniapp_url}")
    if pay_address:
        lines.append(f"PAY_ADDRESS={pay_address}")
    for k in SHARE_KEYS:
        if k in NEVER_SHARE:                 # 이중 방어: 전용값은 절대 복사 안 함
            continue
        v = shared.get(k)
        if v:
            lines.append(f"{k}={v}")

    path = env_path(db_path, slug)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{slug}.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def remove_env(db_path: str, slug: str) -> None:
    """클론 .env 를 지운다 → 자동 켜기 데몬이 그 봇을 끈다. DB 폴더는 남김(기록 보존)."""
    try:
        env_path(db_path, slug).unlink()
    except OSError:
        pass


# ── DB 등록 (메인 DB clones 표) ─────────────────────────────────────

async def list_clones(db) -> list[dict]:
    rows = await db._all("SELECT * FROM clones ORDER BY created")
    return [dict(r) for r in rows]


async def get_clone(db, slug: str) -> dict | None:
    row = await db._one("SELECT * FROM clones WHERE slug=?", (slug,))
    return dict(row) if row else None


async def by_bot_id(db, bot_id: int) -> dict | None:
    row = await db._one("SELECT * FROM clones WHERE bot_id=?", (int(bot_id),))
    return dict(row) if row else None


async def next_slug(db, username: str) -> str:
    """@아이디 기반 slug, 겹치면 clone1·clone2…"""
    existing = {r["slug"] for r in await list_clones(db)}
    base = slugify(username)
    if base and base not in existing:
        return base
    n = 1
    while f"clone{n}" in existing:
        n += 1
    return f"clone{n}"


async def next_port(db) -> int:
    used = {int(r["port"]) for r in await list_clones(db)}
    p = BASE_PORT + 1
    while p in used:
        p += 1
    return p


async def register(db, *, slug: str, info: BotInfo, name: str, port: int, buyer_id: int = 0) -> None:
    await db._write(
        "INSERT OR REPLACE INTO clones(slug, bot_id, username, name, buyer_id, port, status, created) "
        "VALUES(?,?,?,?,?,?, 'pending', ?)",
        (slug, info.bot_id, info.username, name, int(buyer_id), int(port), time.time()),
    )


async def set_status(db, slug: str, status: str) -> None:
    await db._write("UPDATE clones SET status=? WHERE slug=?", (status, slug))


async def unregister(db, slug: str) -> None:
    await db._write("DELETE FROM clones WHERE slug=?", (slug,))
