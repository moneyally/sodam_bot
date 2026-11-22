"""🚫 방별 명령 끄기 (.명령끄기 시세 → 그 방에서 .시세 무시). 2026-11-22 경쟁조사 ③ (Rose /disable).

- 방마다 꺼진 명령을 disabled_cmds 표에 대표이름으로 저장, 30초 방별 캐시 (대부분 방은 0개 → 메시지당 DB 0).
- 관리·안전 필수 명령(설정·오너·명령끄기…)은 끌 수 없음 (PROTECT_ALIASES → 런타임 canon).
- 1:1 엔 적용 안 함(방 기능). 꺼진 명령은 **조용히 무시**(삭제 안 함).
명령 등록은 handlers 가 import 뒤 register_commands()."""
import time

from .db import register_schema

register_schema("""
CREATE TABLE IF NOT EXISTS disabled_cmds (
    chat_id INTEGER NOT NULL,
    name    TEXT    NOT NULL,          -- 명령 대표이름 (Cmd.names[0])
    PRIMARY KEY (chat_id, name)
)""")

# 끌 수 없는 명령(관리·안전·되돌리기 수단). 별칭 아무거나 → 런타임에 대표이름으로 변환해서 비교
PROTECT_ALIASES = ("설정", "설정변경", "명령끄기", "명령켜기", "꺼진명령", "owner", "도움말", "명령어", "AI대화")
_CACHE: dict = {}                      # chat_id → (ts, frozenset(대표이름))
CACHE_TTL = 30


def canon_name(raw: str) -> str | None:
    """입력 명령(별칭 포함) → 대표이름. 그런 명령이 없으면 None."""
    from . import commands
    cmd = commands._INDEX.get((raw or "").lstrip("./!").lower())
    return cmd.names[0] if cmd else None


def _protected() -> frozenset:
    return frozenset(c for a in PROTECT_ALIASES if (c := canon_name(a)))


def bust(chat_id: int) -> None:
    _CACHE.pop(chat_id, None)


async def off_set(db, chat_id: int) -> frozenset:
    hit = _CACHE.get(chat_id)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    rows = await db._all("SELECT name FROM disabled_cmds WHERE chat_id=?", (chat_id,))
    s = frozenset(r["name"] for r in rows)
    _CACHE[chat_id] = (time.time(), s)
    return s


async def is_off(db, chat_id: int, cmd) -> bool:
    """이 방에서 이 명령(Cmd)이 꺼졌나. 대부분 방은 빈 집합이라 캐시 히트."""
    s = await off_set(db, chat_id)
    return bool(s) and cmd.names[0] in s


async def disable_cmd(db, chat_id: int, raw: str) -> str:
    name = canon_name(raw)
    if name is None:
        return f"'{raw}' 라는 명령이 없어."
    if name in _protected():
        return f"'.{name}' 은 관리·안전 명령이라 끌 수 없어."
    await db._write("INSERT OR IGNORE INTO disabled_cmds(chat_id, name) VALUES(?,?)", (chat_id, name))
    bust(chat_id)
    return f"🔇 이 방에서 '.{name}' 명령을 껐어. (켜기: .명령켜기 {name})"


async def enable_cmd(db, chat_id: int, raw: str) -> str:
    name = canon_name(raw) or raw
    await db._write("DELETE FROM disabled_cmds WHERE chat_id=? AND name=?", (chat_id, name))
    bust(chat_id)
    return f"🔊 '.{name}' 명령을 다시 켰어."


async def off_list(db, chat_id: int) -> list:
    rows = await db._all("SELECT name FROM disabled_cmds WHERE chat_id=? ORDER BY name", (chat_id,))
    return [r["name"] for r in rows]


def register_commands() -> None:
    from .panels import disable as panel
    panel.register_commands()
