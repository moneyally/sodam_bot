"""🧪 run_code — 작업실(격리 코드 실행)에서 파이썬을 돌려 계산·표·차트·파일·방 통계 자유 분석 (OpenAI 코드 인터프리터와 같은 모양).

- 서버: sodam-workshop (sodam/workshop/server.py) — 인터넷 없음, 봇 폴더 안 보임, 작업마다 격리. 이 모듈은 요청·결과 전달만.
- 누가: 관리자·오너는 바로. 멤버는 방 설정 run_code_members 를 켠 방에서만. 1:1 은 오너만 (방 데이터 없이).
- 방 데이터: 그룹방이면 그 방만 담은 사본 room.db (sodam/workshop/snapshot.py) — 메시지 원문은 관리자·오너 요청일 때만.
- 한도: 방마다 하루 run_code_daily(기본 30, 관리자는 줄이기만 — settings.OWNER_CAP).
- 결과 파일: 그림(png·jpg·gif)은 사진으로, 나머지(csv·xlsx·pdf…)는 파일로 이 방에 올림. 출력 글은 AI 에게만.
- 방 사본을 넣은 실행은 멤버가 쓴 글·이름을 읽은 것 → ctx.room_read (카드 없는 쓰기 도구 막힘, tools.execute).
"""
from __future__ import annotations

import io
import logging
from datetime import datetime

from telegram import InputFile
from telegram.error import TelegramError

from .. import memory, tools
from ..permissions import Role
from ..settings import OWNER_CAP, register_setting
from ..tools import Tool, ToolCtx
from ..util import display_name, esc
from ..workshop import client, snapshot

log = logging.getLogger(__name__)

register_setting("run_code_members", False, "🧪 멤버도 코드 실행(계산·차트) 쓰기")
register_setting("run_code_daily", 30, "🧪 코드 실행 하루 횟수", range_=(0, 30))
OWNER_CAP["run_code_daily"] = 30          # 방 관리자는 줄이기만, 늘리는 건 오너

PHOTO_EXT = (".png", ".jpg", ".jpeg", ".gif")
OUT_CHARS = 3000                           # AI 에게 돌려줄 출력 (도구 결과 4000자 안)
COUNTER = "run_code"

DESC = ("격리된 파이썬 작업실에서 코드를 실행한다 (인터넷 없음, 방마다 20분 동안 같은 폴더). 계산·통계·표·차트·엑셀/CSV/PDF 파일 만들기, "
        "그리고 그룹방에선 이 방 데이터 사본 room.db(sqlite)로 자유 분석. "
        "print 한 글이 결과로 돌아오고, 현재 폴더에 저장한 파일(png·jpg·gif = 사진, csv·xlsx·pdf·txt·json·md·svg = 파일, 5개까지)은 방에 바로 올라간다. "
        "라이브러리: numpy pandas matplotlib(한글 글꼴 NanumGothic 설정됨) seaborn openpyxl xlsxwriter duckdb pillow reportlab pypdf qrcode "
        "tabulate wordcloud squarify networkx rapidfuzz holidays korean_lunar_calendar emoji. "
        "오류가 나면 고쳐서 한 번 더. 결과 숫자는 출력에 있는 그대로만 말한다. "
        + snapshot.GUIDE)


def _allowed(ctx: ToolCtx) -> str | None:
    if ctx.chat_id > 0:
        return None if ctx.role >= Role.OWNER else "1:1 에서는 코드 실행을 못 씀 (그룹방에서 관리자가 부탁하면 됨)."
    if ctx.role >= Role.ADMIN or ctx.settings.get("run_code_members"):
        return None
    return "이 방에선 코드 실행(계산·차트)은 관리자만 쓸 수 있음. 관리자가 🧩 설정에서 멤버에게도 켤 수 있다고 짧게 안내할 것."


async def _send(ctx: ToolCtx, name: str, data: bytes, caption: str):
    if name.lower().endswith(PHOTO_EXT) and len(data) < 10 * 1024 * 1024:
        try:
            return await ctx.bot.send_photo(ctx.chat_id, photo=data, caption=caption, parse_mode="HTML")
        except TelegramError:   # 사진으로 안 되는 크기·비율이면 파일로
            pass
    return await ctx.bot.send_document(ctx.chat_id, InputFile(io.BytesIO(data), filename=name), caption=caption, parse_mode="HTML")


async def t_run_code(ctx: ToolCtx, a: dict) -> str:
    code = str(a.get("code") or "").strip()
    if not code:
        return "실행할 코드가 비어 있음."
    if why := _allowed(ctx):
        return why
    svc = ctx.svc
    day = datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")
    limit = min(int(ctx.settings.get("run_code_daily", 30)), OWNER_CAP["run_code_daily"])
    if ctx.role < Role.OWNER and await svc.db.counter(day, ctx.chat_id, COUNTER) >= limit:
        return f"오늘 이 방 코드 실행 한도({limit}번)를 다 썼음. 내일 다시 된다고 안내할 것."
    files: dict[str, bytes] = {}
    if ctx.chat_id < 0:
        admin = ctx.role >= Role.ADMIN
        files["room.db"] = await snapshot.build(svc.db, ctx.chat_id, admin, svc.cfg.tz)
        ctx.room_read = True   # 사본 = 멤버가 쓴 이름·글 → 이 답변에선 확인 카드 없는 쓰기 도구 막힘
    try:
        res = await client.run(f"c{abs(ctx.chat_id)}", code, files)
    except client.WorkshopDown as e:
        log.warning("작업실 응답 없음: %s", e)
        return "작업실(코드 실행 서버)이 지금 꺼져 있음. 잠시 뒤 다시 부탁해 달라고 짧게 안내할 것 (계산은 말로 대신 할 수 있으면 해도 됨)."
    await svc.db.bump(day, ctx.chat_id, COUNTER)
    who = esc(display_name(ctx.caller.first_name, ctx.caller.last_name, ctx.caller.username))
    sent_names, last = [], None
    for name, data in (res.get("files") or {}).items():
        try:
            last = await _send(ctx, name, data, f"🧪 {who}님 요청 · {esc(name)}")
            sent_names.append(name)
        except TelegramError as e:
            log.info("작업실 결과 전송 실패 %s: %s", name, e)
    if last is not None:
        photo = getattr(last, "photo", None)
        await memory.record_turn(svc.db, ctx.chat_id, ctx.caller.id, "run_code", code[:500], "(작업실 결과 파일을 보냄)",
                                 last.message_id, media=photo[-1].file_id if photo else None)
    status = res.get("status", "?")
    out = (res.get("output") or "").strip()
    if len(out) > OUT_CHARS:
        out = out[:OUT_CHARS // 2] + "\n…(중간 생략)…\n" + out[-OUT_CHARS // 2:]
    head = {"ok": "실행 완료", "error": "실행 중 오류 — 고쳐서 한 번 더 해 볼 것", "timeout": "시간 초과(20초) — 더 가볍게",
            "cpu": "계산 시간 초과 — 더 가볍게", "memory": "메모리 초과 — 데이터를 줄여서"}.get(status, f"실행 실패({status})")
    tail = f"\n방에 올린 파일: {', '.join(sent_names)} (파일 내용을 다시 설명하지 말고 한마디만)" if sent_names else ""
    return f"{head} ({res.get('ms', 0)}ms)\n출력:\n{out or '(출력 없음)'}{tail}"


tools.register_tool(Tool(
    "run_code", DESC,
    {"code": {"type": "string", "description": "실행할 파이썬 코드 (결과는 print, 파일은 현재 폴더에 저장)"}},
    ["code"], t_run_code), read_only=True)
