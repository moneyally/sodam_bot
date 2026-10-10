"""🧩 이모지 공방 — 오너가 소담 1:1 에 보낸 '타일 이모지'(프리미엄 커스텀 이모지)를 조각 파일 그대로 서버에 모아 둔다.

왜 (2026-10-10 오너): '제휴업체' 배너처럼 긴 그림을 100×100 조각 여러 개로 나눠 이어 치는 이모지를 똑같은 모양으로 만들고 싶음.
소담은 중개만 — 견본을 받아 저장(분석 기록) → 클로드가 서버에서 가져가 분석·제작 → `tools/emoji_pack.py` 로 오너 이름의 이모지 팩 등록.

- 오너만, 메시지에 custom_emoji 가 있고 이모지를 뺀 글이 짧을 때(NOTE_MAX)만 받음 (오너가 평소 대화에 이모지 하나 쓴 건 AI 로 그대로).
- 순서·줄바꿈 그대로 (텔레그램 위치는 UTF-16 단위 → 파이썬 글자 위치로 바꿔서 줄 계산).
- getCustomEmojiStickers 로 조각(sticker) → 파일: 정지 webp / 영상 webm / 움직이는 tgs. 같은 조각은 한 번만 받음.
- data/emoji_lab/<시각>/ 에 NN_<id>.<ext> + meta.json(줄·순서·세트 이름·크기·종류) + preview.png(이어 붙인 미리보기) → 오너 1:1 에 미리보기.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

NOTE_MAX = 40            # 이모지를 뺀 글이 이보다 길면 평소 대화로 봄
WANT = ("이모지", "분석", "타일", "조각", "만들")   # 이모지 하나 + 이 말이면 받음 (하나 + 다른 말 = 평소 대화)
MAX_TILES = 60           # 한 번에 받는 조각 수 (getCustomEmojiStickers 한도 200)
MAX_BYTES = 2_000_000    # 조각 하나 크기 상한 (프리미엄 이모지 규격은 64KB)
TILE = 100               # 미리보기 칸 크기
KEEP = 50                # 보관 묶음 수 (넘으면 오래된 것부터 지움)


def lab_dir(db_path: str) -> Path:
    if db_path in ("", ":memory:"):
        return Path(tempfile.gettempdir()) / f"sodam_emoji_lab_{os.getpid()}"
    return Path(db_path).resolve().parent / "emoji_lab"


def _u16_to_index(text: str, offset: int) -> int:
    """텔레그램 entity offset(UTF-16 단위) → 파이썬 글자 위치."""
    return len(text.encode("utf-16-le")[: offset * 2].decode("utf-16-le", errors="ignore"))


def tiles_of(msg) -> tuple[list[list[str]], str]:
    """메시지의 커스텀 이모지 → (줄마다 id 목록, 이모지 뺀 나머지 글)."""
    text = msg.text if msg.text is not None else (msg.caption or "")
    ents = list(msg.entities or ()) if msg.text is not None else list(getattr(msg, "caption_entities", None) or ())
    ents = sorted((e for e in ents if getattr(e, "type", "") == "custom_emoji" and getattr(e, "custom_emoji_id", None)),
                  key=lambda e: e.offset)
    rows: list[list[str]] = []
    rest, last = [], 0
    for e in ents:
        a, b = _u16_to_index(text, e.offset), _u16_to_index(text, e.offset + e.length)
        gap = text[last:a]
        rest.append(gap)
        if not rows or "\n" in gap:
            rows.append([])
        rows[-1].append(str(e.custom_emoji_id))
        last = b
    rest.append(text[last:])
    note = " ".join("".join(rest).split())
    return rows, note


async def maybe_capture(svc, bot, msg, user) -> bool:
    """오너가 보낸 타일 이모지면 받아 저장하고 True."""
    rows, note = tiles_of(msg)
    count = sum(len(r) for r in rows)
    asked = any(w in note for w in WANT)
    if not count or len(note) > NOTE_MAX or (count == 1 and note and not asked):
        return False
    if user.id not in await svc.perms.owners():
        return False
    try:
        await capture(svc, bot, msg, rows, note)
    except Exception as e:
        log.exception("이모지 공방 저장 실패")
        await msg.reply_text(f"🧩 이모지 조각을 못 받았어요: {type(e).__name__}")
    return True


async def capture(svc, bot, msg, rows: list[list[str]], note: str) -> Path:
    flat = [i for r in rows for i in r][:MAX_TILES]
    uniq = list(dict.fromkeys(flat))
    stickers = await bot.get_custom_emoji_stickers(uniq)
    by_id = {str(s.custom_emoji_id): s for s in stickers}
    base = lab_dir(svc.cfg.db_path)
    out = base / time.strftime("%Y%m%d-%H%M%S")
    n = 1
    while out.exists():
        n += 1
        out = base / (time.strftime("%Y%m%d-%H%M%S") + f"-{n}")
    out.mkdir(parents=True)
    files, sets = {}, {}
    for i, cid in enumerate(uniq, 1):
        s = by_id.get(cid)
        if not s:
            continue
        ext = "tgs" if s.is_animated else "webm" if s.is_video else "webp"
        f = await bot.get_file(s.file_id)
        data = bytes(await f.download_as_bytearray())
        if len(data) > MAX_BYTES:
            continue
        name = f"{i:02d}_{cid}.{ext}"
        (out / name).write_bytes(data)
        files[cid] = {"file": name, "emoji": s.emoji, "set": s.set_name, "kind": ext,
                      "w": s.width, "h": s.height, "repaint": bool(getattr(s, "needs_repainting", False)),
                      "bytes": len(data)}
        if s.set_name and s.set_name not in sets:
            sets[s.set_name] = await _set_info(bot, s.set_name)
    meta = {"from": msg.from_user.id, "at": int(time.time()), "note": note, "rows": rows,
            "tiles": files, "sets": sets, "missing": [c for c in uniq if c not in files]}
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    preview = await asyncio.to_thread(_preview, out, rows, files)
    _trim(base)
    kinds = sorted({v["kind"] for v in files.values()})
    kind_ko = {"webp": "정지 그림", "webm": "움직이는 영상", "tgs": "움직이는 벡터"}
    sets_txt = ", ".join(f"{k} ({v.get('title') or '?'} · {v.get('count', '?')}개)" for k, v in sets.items()) or "모름"
    text = (f"🧩 타일 이모지 받았어요 — 저장 {out.name}\n"
            f"• 줄 {len(rows)}개 · 조각 {len(flat)}칸 (서로 다른 조각 {len(files)}개)\n"
            f"• 종류: {', '.join(kind_ko.get(k, k) for k in kinds) or '없음'}\n"
            f"• 원래 세트: {sets_txt}"
            + (f"\n• 메모: {note}" if note else "")
            + (f"\n⚠️ 못 받은 조각 {len(meta['missing'])}개" if meta["missing"] else ""))
    if preview:
        await msg.reply_photo(io.BytesIO(preview), caption=text[:1000])
    else:
        await msg.reply_text(text)
    log.info("이모지 공방 저장 %s 조각 %d", out, len(files))
    return out


async def _set_info(bot, set_name: str) -> dict:
    try:
        st = await bot.get_sticker_set(set_name)
    except Exception as e:
        return {"error": type(e).__name__}
    return {"title": st.title, "type": getattr(st, "sticker_type", ""), "count": len(st.stickers),
            "ids": [str(x.custom_emoji_id) for x in st.stickers][:200]}


def _frame(path: Path):
    """조각 한 장 → RGBA 그림 (영상은 첫 장면, tgs 는 못 그림 → None)."""
    from PIL import Image
    if path.suffix == ".webp":
        return Image.open(path).convert("RGBA")
    if path.suffix == ".webm":
        try:
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None
        r = subprocess.run([exe, "-v", "error", "-c:v", "libvpx-vp9", "-i", str(path), "-frames:v", "1",
                            "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True, timeout=20)
        if r.returncode or not r.stdout:
            return None
        return Image.open(io.BytesIO(r.stdout)).convert("RGBA")
    return None


def _preview(out: Path, rows: list[list[str]], files: dict) -> bytes | None:
    try:
        from PIL import Image
    except Exception:
        return None
    width = max(len(r) for r in rows)
    canvas = Image.new("RGBA", (width * TILE + 20, len(rows) * TILE + 20), (40, 44, 52, 255))
    cache: dict = {}
    for y, row in enumerate(rows):
        for x, cid in enumerate(row):
            info = files.get(cid)
            if not info:
                continue
            if cid not in cache:
                try:
                    cache[cid] = _frame(out / info["file"])
                except Exception:
                    cache[cid] = None
            im = cache[cid]
            if im is None:
                continue
            im = im.resize((TILE, TILE))
            canvas.alpha_composite(im, (10 + x * TILE, 10 + y * TILE))
    canvas.save(out / "preview.png")
    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def _trim(base: Path) -> None:
    dirs = sorted((d for d in base.iterdir() if d.is_dir() and d.name[:2].isdigit()), key=lambda d: d.name)
    for d in dirs[:-KEEP]:
        for f in d.iterdir():
            f.unlink(missing_ok=True)
        d.rmdir()
