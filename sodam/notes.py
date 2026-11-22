"""📝 멤버 노트 (.저장 이름 → #이름 치면 재전송). 2026-11-22 경쟁조사 ② (Rose /save·#hashtag).

autoreply(관리자 공지, 낱말 그대로 트리거) 와 다른 점:
- **멤버 누구나** 저장·호출 (autoreply 는 관리자 전용)
- **# 접두어** 트리거 (#공지) — 그래서 명령(.)·게임(!)·autoreply 낱말과 안 겹침
- 방 공용 key (누가 저장하든 #공지 는 하나) — 새 이름은 누구나, 이미 있는 이름은 **만든 사람·관리자만** 덮기/삭제

재전송은 autoreply.snapshot/send_snap 을 그대로 씀 → 원본 서식·움직이는 이모지·사진·스티커·표(rich) 보존.
명령 등록은 handlers 가 import 뒤 register_commands() (commands → menu → panels 순환이라 여기서)."""
import json
import re
import time

from .db import register_schema
from . import autoreply

register_schema("""
CREATE TABLE IF NOT EXISTS member_notes (
    chat_id INTEGER NOT NULL,
    key     TEXT    NOT NULL,
    user_id INTEGER NOT NULL,
    snap    TEXT    NOT NULL,          -- autoreply.snapshot JSON (원본 그대로 재전송용)
    created INTEGER NOT NULL,
    PRIMARY KEY (chat_id, key)
)""")

MAX_PER_ROOM = 200
MAX_PER_USER = 30
CALL_GAP = 8                           # 한 사람 노트 호출 간격(초) — 도배 방지
_KEY = re.compile(r"[0-9A-Za-z가-힣_]{1,30}")
# 글 안의 #이름 — 앞은 줄머리/공백, 뒤는 공백·끝·문장부호 (해시 중간 글자는 이름에서 뺌)
_HASH = re.compile(r"(?:^|(?<=\s))#([0-9A-Za-z가-힣_]{1,30})(?=$|\s|[!?.,~])")
_last_call: dict = {}                  # (chat, uid) → ts : 호출 쿨다운 (메모리, 재시작 영향 없음)


def valid_key(key: str | None) -> bool:
    return bool(key) and bool(_KEY.fullmatch(key))


def find_hashes(text: str) -> list[str]:
    return _HASH.findall(text or "")


async def exists(db, chat_id: int, key: str) -> bool:
    return bool(await db._one("SELECT 1 FROM member_notes WHERE chat_id=? AND key=?", (chat_id, key)))


async def owner_of(db, chat_id: int, key: str) -> int | None:
    row = await db._one("SELECT user_id FROM member_notes WHERE chat_id=? AND key=?", (chat_id, key))
    return row["user_id"] if row else None


async def get_snap(db, chat_id: int, key: str) -> dict | None:
    row = await db._one("SELECT snap FROM member_notes WHERE chat_id=? AND key=?", (chat_id, key))
    return json.loads(row["snap"]) if row else None


async def _count_room(db, chat_id: int) -> int:
    row = await db._one("SELECT COUNT(*) n FROM member_notes WHERE chat_id=?", (chat_id,))
    return row["n"] if row else 0


async def _count_user(db, chat_id: int, uid: int) -> int:
    row = await db._one("SELECT COUNT(*) n FROM member_notes WHERE chat_id=? AND user_id=?", (chat_id, uid))
    return row["n"] if row else 0


async def save(db, chat_id: int, key: str, *, by: int, snap: dict) -> str:
    """새 노트 저장. 성공이면 'ok', 아니면 사유 문자열."""
    if not valid_key(key):
        return "이름은 한글·영문·숫자·_ 로 30자까지야 (#없이)."
    if await exists(db, chat_id, key):
        return f"'#{key}' 는 이미 있어. 바꾸려면 먼저 .노트삭제 {key}"
    if await _count_room(db, chat_id) >= MAX_PER_ROOM:
        return f"이 방 노트가 꽉 찼어 (최대 {MAX_PER_ROOM}개)."
    if await _count_user(db, chat_id, by) >= MAX_PER_USER:
        return f"네가 만든 노트가 꽉 찼어 (한 사람 {MAX_PER_USER}개)."

    def fn(cn):
        cn.execute("INSERT INTO member_notes(chat_id, key, user_id, snap, created) VALUES(?,?,?,?,?)",
                   (chat_id, key, by, json.dumps(snap, ensure_ascii=False), int(time.time())))
    await db.atomic(fn)
    return "ok"


async def remove(db, chat_id: int, key: str, *, by: int, is_admin: bool) -> str:
    owner = await owner_of(db, chat_id, key)
    if owner is None:
        return f"'#{key}' 라는 노트가 없어."
    if owner != by and not is_admin:
        return "그 노트를 만든 사람이나 관리자만 지울 수 있어."
    await db._write("DELETE FROM member_notes WHERE chat_id=? AND key=?", (chat_id, key))
    return f"🗑 '#{key}' 지웠어."


async def rows(db, chat_id: int) -> list:
    return await db._all("SELECT key, user_id FROM member_notes WHERE chat_id=? ORDER BY key", (chat_id,))


def _can_call(chat_id: int, uid: int) -> bool:
    now = time.time()
    if now - _last_call.get((chat_id, uid), 0) < CALL_GAP:
        return False
    _last_call[(chat_id, uid)] = now
    if len(_last_call) > 5000:          # 메모리 상한 (오래된 것 정리)
        for k in [k for k, v in _last_call.items() if now - v > 300]:
            _last_call.pop(k, None)
    return True


async def handle(svc, bot, msg, text: str) -> bool:
    """그룹 메시지에서 #이름 → 그 노트 재전송 (handlers 가 autoreply 다음·게임/AI 앞에서 부름).
    노트를 재전송했으면 True(이 글 처리 끝), 등록 안 된 해시거나 # 없으면 False(게임·AI 계속)."""
    if "#" not in (text or ""):
        return False
    keys = find_hashes(text)
    if not keys:
        return False
    chat_id, uid = msg.chat_id, msg.from_user.id
    key = keys[0]                       # 한 글에 여러 # 라도 첫 번째만 (도배 방지)
    snap = await get_snap(svc.db, chat_id, key)
    if snap is None:
        return False                    # 그냥 해시태그 — 노트 아님
    if not _can_call(chat_id, uid):
        return True                     # 쿨다운: 재전송 안 하지만 노트 호출이라 중단
    try:
        await autoreply.send_snap(bot, svc.db, chat_id, snap, reply_to=msg.message_id)
    except Exception:
        pass
    return True


def register_commands() -> None:
    from .panels import notes as panel
    panel.register_commands()
