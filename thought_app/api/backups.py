from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory, mkstemp

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from ..backup import MAX_UPLOAD_BYTES, BackupError, export_backup, read_backup, restore_backup
from ..config import seed_experts
from ..database import connect, database_path
from ..persona_defaults import AGENT_PERSONA_REWRITE_KEY, migrate_builtin_reply_styles


router = APIRouter(prefix="/api/backup", tags=["backup"])
ARCHIVE_SUFFIX = ".thoughts-backup.zip"
NO_CACHE = {"Cache-Control": "no-store"}


def archive_name():
    return "thought-journal-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ARCHIVE_SUFFIX


def temporary_archive(**kwargs):
    fd, filename = mkstemp(suffix=ARCHIVE_SUFFIX, **kwargs)
    os.close(fd)
    return Path(filename)


@router.get("/export")
def download_backup():
    archive = temporary_archive(prefix="thought-journal-")
    try:
        with connect() as db:
            db.execute("BEGIN")
            export_backup(db, archive)
    except (BackupError, OSError, sqlite3.Error) as error:
        archive.unlink(missing_ok=True)
        detail = str(error) if isinstance(error, BackupError) else "无法导出备份，请检查本地磁盘。"
        raise HTTPException(500, detail) from error
    return FileResponse(
        archive, filename=archive_name(), media_type="application/zip",
        headers=NO_CACHE, background=BackgroundTask(archive.unlink, missing_ok=True),
    )


def apply_backup(payload):
    folder = database_path().parent / "backups"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    archive = temporary_archive(prefix="before-import-", dir=folder)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            export_backup(db, archive)
        except Exception:
            archive.unlink(missing_ok=True)
            raise
        restored = restore_backup(db, payload)
        seed_experts(db)
        migrate_builtin_reply_styles(db)
        db.execute("INSERT OR IGNORE INTO config_settings (key, value) VALUES (?, '1')",
                   (AGENT_PERSONA_REWRITE_KEY,))
    return {"restored": restored, "safety_backup": archive.name}


@router.post("/import")
async def import_backup(request: Request):
    with TemporaryDirectory(prefix="thought-journal-import-") as directory:
        incoming = Path(directory) / "upload.zip"
        size = 0
        with incoming.open("wb") as stream:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "备份文件超过上传上限。")
                stream.write(chunk)
        try:
            payload = await run_in_threadpool(read_backup, incoming)
        except BackupError as error:
            raise HTTPException(400, str(error)) from error

    runtime = request.app.state.runtime
    if runtime.restoring or runtime.api_requests != 1 or runtime.sides.tasks:
        raise HTTPException(409, "还有会话或运维操作在进行，请等它完成后再导入。")

    runtime.restoring = True
    try:
        await runtime.pause_observer()
        result = await run_in_threadpool(apply_backup, payload)
        runtime.sides.errors.clear()
        return result
    except BackupError as error:
        raise HTTPException(400, str(error)) from error
    except (OSError, sqlite3.Error) as error:
        raise HTTPException(500, "未能恢复备份，请检查本地磁盘。") from error
    finally:
        try:
            runtime.resume_observer()
        finally:
            runtime.restoring = False
