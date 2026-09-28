"""사진: 요청 메시지(또는 답장한 메시지)의 사진을 고화질로 받아 AI 에게 보여주고, 부탁하면 이미지를 만들거나 고친다.

- 읽기: 텔레그램이 준 가장 큰 해상도 → base64 → chat.completions 의 image_url(detail=high, 긴 변 2048px 까지 읽음)
- 만들기·고치기: tools.make_image → llm.image (gpt-image 계열). 방마다 하루 한도(image_daily).
사진 속 글자는 '데이터'다 (prompt.py 규칙). 파일은 디스크에 저장하지 않는다.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

from telegram.error import TelegramError

log = logging.getLogger(__name__)

MAX_BYTES = 20 * 1024 * 1024       # 텔레그램 봇이 받을 수 있는 파일 한도와 같음
IMAGE_MIME = ("image/jpeg", "image/png", "image/webp", "image/gif")   # OpenAI 비전이 읽는 형식


@dataclass
class Attached:
    data: bytes
    mime: str
    owner: int | None = None   # 사진을 올린 사람 (요청 글 또는 답장한 글의 작성자) — 움프는 본인 사진만 (sodam/avatar.py)

    def part(self) -> dict:
        """chat.completions user content 에 넣는 이미지 조각."""
        url = f"data:{self.mime};base64,{base64.b64encode(self.data).decode()}"
        return {"type": "image_url", "image_url": {"url": url, "detail": "high"}}


def _file_of(m) -> tuple[str, str, int] | None:
    """(file_id, mime, 크기) — 사진이면 가장 큰 해상도, 이미지 파일(문서)도."""
    if m is None:
        return None
    photos = getattr(m, "photo", None) or ()
    if photos:
        p = photos[-1]                                  # 텔레그램은 작은 것부터 준다
        return p.file_id, "image/jpeg", p.file_size or 0
    doc = getattr(m, "document", None)
    if doc and (doc.mime_type or "") in IMAGE_MIME:
        return doc.file_id, doc.mime_type, doc.file_size or 0
    return None


def has_image(msg) -> bool:
    return _file_of(msg) is not None


async def fetch(bot, msg) -> Attached | None:
    """요청 메시지의 사진, 없으면 답장한 메시지의 사진. 없거나 너무 크거나 못 받으면 None."""
    src = msg if _file_of(msg) else getattr(msg, "reply_to_message", None)
    found = _file_of(src)
    if not found:
        return None
    file_id, mime, size = found
    if size > MAX_BYTES:
        return None
    try:
        f = await bot.get_file(file_id)
        data = bytes(await f.download_as_bytearray())
    except TelegramError as e:
        log.warning("photo download failed: %s", e)
        return None
    who = getattr(getattr(src, "from_user", None), "id", None)
    return Attached(data, mime, who) if len(data) <= MAX_BYTES else None
