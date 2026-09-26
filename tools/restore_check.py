"""백업 복원 검증: python tools/restore_check.py [백업파일.db.gz | 백업폴더]

최신 백업(sodam-YYYYMMDD-HHMMSS.db.gz)을 임시 폴더에 풀어 sodam.db.DB 로 열고
PRAGMA integrity_check + 핵심 테이블 행 수를 출력한다. 하나라도 실패하면 exit 1.
실제 DB(data/sodam.db)는 건드리지 않는다. 인자가 없으면 .env 의 BACKUP_DIR (기본 data/backups).
"""
from __future__ import annotations

import asyncio
import gzip
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sodam.backup import Backup  # noqa: E402
from sodam.db import DB  # noqa: E402

CORE_TABLES = ("chats", "users", "members", "owners", "subscriptions", "invoices", "payments", "messages", "chat_state")


def find_latest(target: Path) -> Path | None:
    if target.is_file():
        return target
    files = sorted(target.glob(Backup.PATTERN), reverse=True)  # 파일명에 시각 → 이름순 = 시간순
    return files[0] if files else None


async def check(target: Path, out=print) -> int:
    """0 = 복원 가능, 1 = 실패."""
    backup = find_latest(target)
    if not backup:
        out(f"❌ 백업 파일이 없어요: {target}")
        return 1
    out(f"백업: {backup} ({backup.stat().st_size:,} bytes)")
    with tempfile.TemporaryDirectory(prefix="sodam-restore-") as tmp:
        db_file = Path(tmp) / "sodam.db"
        try:
            with gzip.open(backup, "rb") as src, open(db_file, "wb") as dst:
                shutil.copyfileobj(src, dst)
        except (OSError, EOFError) as e:
            out(f"❌ 압축 해제 실패: {e}")
            return 1
        # DB.open 은 없는 테이블을 새로 만들기 때문에, 열기 전에 원본 그대로 핵심 테이블이 있는지 본다
        try:
            con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
            try:
                have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                con.close()
        except sqlite3.DatabaseError as e:
            out(f"❌ SQLite 파일이 아니거나 깨졌어요: {e}")
            return 1
        missing = [t for t in CORE_TABLES if t not in have]
        if missing:
            out(f"❌ 핵심 테이블 없음: {', '.join(missing)}")
            return 1
        db = DB(str(db_file))
        try:
            await db.open()
            rows = await db._all("PRAGMA integrity_check")
            result = rows[0][0] if rows else "(결과 없음)"
            if result != "ok":
                out("❌ integrity_check 실패: " + "; ".join(str(r[0]) for r in rows[:5]))
                return 1
            out("integrity_check: ok")
            for t in CORE_TABLES:
                n = (await db._one(f"SELECT COUNT(*) AS n FROM {t}"))["n"]
                out(f"  {t:<14} {n:>8,} 행")
        except Exception as e:  # 망가진 파일·SQLite 가 아님 등
            out(f"❌ DB 열기/검사 실패: {type(e).__name__}: {e}")
            return 1
        finally:
            try:
                await db.close()
            except Exception:
                db.stop_sync()
    out("✅ 복원 가능")
    return 0


def main(argv: list[str]) -> int:
    if argv:
        target = Path(argv[0])
    else:
        try:
            from dotenv import load_dotenv
            load_dotenv(ROOT / ".env")
        except ImportError:
            pass
        target = Path(os.getenv("BACKUP_DIR", "").strip() or "data/backups")
        if not target.is_absolute():
            target = ROOT / target
    return asyncio.run(check(target))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
