"""DB 자동 백업: 온라인 백업 → 무결성 검사 → gzip → 오래된 것 정리.

복구: 봇을 멈추고 `python tools/restore_check.py 파일` 로 검증 → 압축을 풀어 DB_PATH 위치에 덮어쓰고
(같은 폴더의 sodam.db-wal / sodam.db-shm 은 삭제) 다시 실행.
"""
import asyncio
import gzip
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import Config
from .db import DB


class BackupError(Exception):
    pass


class Backup:
    PATTERN = "sodam-*.db.gz"

    def __init__(self, cfg: Config, db: DB):
        self.cfg = cfg
        self.db = db
        self.dir = Path(cfg.backup_dir)

    async def run(self) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(self.cfg.tz).strftime("%Y%m%d-%H%M%S")
        raw = self.dir / f"sodam-{stamp}.db"
        gz = raw.with_name(raw.name + ".gz")
        try:
            await self.db.backup_to(str(raw))
            gz = await asyncio.to_thread(self._verify_and_compress, raw)
        except BaseException:
            gz.unlink(missing_ok=True)   # 디스크 가득 참 등으로 중간에 실패하면 반쯤 쓴 파일이 공간을 먹지 않게
            raise
        finally:
            raw.unlink(missing_ok=True)
        await asyncio.to_thread(self._prune)
        return gz

    def _verify_and_compress(self, raw: Path) -> Path:
        con = sqlite3.connect(raw)
        try:
            result = con.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            con.close()
        if result != "ok":
            raise BackupError(f"무결성 검사 실패: {result}")
        gz = raw.with_name(raw.name + ".gz")
        with open(raw, "rb") as src, gzip.open(gz, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        return gz

    def list(self) -> list[Path]:
        if not self.dir.exists():
            return []
        return sorted(self.dir.glob(self.PATTERN), reverse=True)  # 파일명에 시각이 있어서 이름순 = 시간순

    def _prune(self) -> None:
        for old in self.list()[self.cfg.backup_keep:]:
            old.unlink(missing_ok=True)
