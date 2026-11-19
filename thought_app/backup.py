from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
import sqlite3
from pathlib import Path
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile
import zlib


MAX_UPLOAD_BYTES = 64 * 1024 * 1024
_MAX_JSON_BYTES = 128 * 1024 * 1024
_FORMAT = "thought-journal-backup"
_VERSION = 1
_ENTRY = "journal.json"
_MAX_INT = 2**63 - 1

# Parent tables precede their children. The archive carries data only.
_TABLES = (
    "providers", "models", "personas", "bots", "config_settings",
    "sessions", "messages", "summaries", "rewind_drafts",
    "bot_evolution_settings", "bot_evolution_notes", "observer_jobs", "observer_observations",
    "api_usage_alerts", "api_usage_calls",
)
_SEQUENCED = frozenset({
    "providers", "models", "personas", "bots", "sessions", "messages", "bot_evolution_notes",
    "observer_observations", "api_usage_alerts", "api_usage_calls",
})
_PUBLIC_SETTINGS = (
    "seeded", "poe_seeded", "group_personas_v3", "friend_voices_v4", "expert_personas_v1", "summary_model_id", "long_replies",
)


class BackupError(ValueError):
    """The backup cannot be created or restored as requested."""


def _identifier(value):
    return '"' + value.replace('"', '""') + '"'


def _columns(db, table):
    return {
        row["name"]: row
        for row in db.execute(f"PRAGMA table_info({_identifier(table)})")
        if not (table == "providers" and row["name"] == "api_key")
    }


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def export_backup(db: sqlite3.Connection, output: Path) -> None:
    """Write a consistent snapshot from the caller's open SQLite transaction."""
    if not db.in_transaction:
        raise RuntimeError("Export requires an open SQLite transaction.")
    sequences = {
        name: sequence
        for name, sequence in db.execute("SELECT name, seq FROM sqlite_sequence")
        if name in _SEQUENCED
    }
    metadata = {
        "format": _FORMAT, "version": _VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sequences": sequences,
    }
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle, ZipFile(handle, "w", ZIP_DEFLATED) as archive:
        with archive.open(_ENTRY, "w") as stream:
            size = 0

            def write(value):
                nonlocal size
                data = value.encode("utf-8")
                size += len(data)
                if size > _MAX_JSON_BYTES:
                    raise BackupError("备份数据超出支持的大小。")
                stream.write(data)

            write(_dump(metadata)[:-1] + ',"tables":{')
            for index, table in enumerate(_TABLES):
                columns = list(_columns(db, table))
                fields = ", ".join(map(_identifier, columns))
                rows = db.execute(
                    f"SELECT {fields} FROM {_identifier(table)}"
                    + (
                        " WHERE key IN (" + ", ".join("?" for _ in _PUBLIC_SETTINGS) + ")"
                        if table == "config_settings" else ""
                    ),
                    _PUBLIC_SETTINGS if table == "config_settings" else (),
                )
                if index:
                    write(",")
                write(_dump(table) + ':{"columns":' + _dump(columns) + ',"rows":[')
                for row_index, row in enumerate(rows):
                    if row_index:
                        write(",")
                    write(_dump(list(row)))
                write("]}")
            write("}}")
    if output.stat().st_size > MAX_UPLOAD_BYTES:
        raise BackupError("压缩后的备份文件超出支持的大小。")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate object key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("Invalid JSON number")


def read_backup(path: Path) -> dict:
    """Open only our bounded JSON ZIP format, without extracting any paths."""
    try:
        if path.stat().st_size > MAX_UPLOAD_BYTES:
            raise BackupError("备份文件超出支持的大小。")
        with ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) != 1 or entries[0].filename != _ENTRY:
                raise BackupError("请选择想法簿导出的 ZIP 备份。")
            entry = entries[0]
            if (
                entry.flag_bits & 1
                or entry.compress_type not in (ZIP_STORED, ZIP_DEFLATED)
                or entry.file_size > _MAX_JSON_BYTES
            ):
                raise BackupError("备份压缩格式不受支持，或者文件过大。")
            with archive.open(entry) as stream:
                data = stream.read(_MAX_JSON_BYTES + 1)
        if len(data) > _MAX_JSON_BYTES:
            raise BackupError("备份数据超出支持的大小。")
        payload = json.loads(
            data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant,
        )
    except BackupError:
        raise
    except (OSError, BadZipFile, EOFError, ValueError, RuntimeError, RecursionError, zlib.error) as exc:
        raise BackupError("无法读取备份，文件可能已损坏。") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("format") != _FORMAT
        or type(payload.get("version")) is not int
        or payload["version"] != _VERSION
    ):
        raise BackupError("备份格式或版本不受支持。")
    if set(payload) != {"format", "version", "exported_at", "tables", "sequences"}:
        raise BackupError("备份结构不完整。")
    try:
        if datetime.fromisoformat(payload["exported_at"]).utcoffset() is None:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise BackupError("备份时间无效。") from exc
    tables = payload["tables"]
    if not isinstance(tables, dict) or set(tables) != set(_TABLES):
        raise BackupError("备份表不完整，或者包含不支持的表。")
    for table in tables.values():
        if (
            not isinstance(table, dict) or set(table) != {"columns", "rows"}
            or not isinstance(table["columns"], list) or not isinstance(table["rows"], list)
            or not all(isinstance(column, str) for column in table["columns"])
            or len(set(table["columns"])) != len(table["columns"])
        ):
            raise BackupError("备份表格式不正确。")
    sequences = payload["sequences"]
    if not isinstance(sequences, dict) or set(sequences) - _SEQUENCED or any(
        type(seq) is not int or not 0 <= seq < _MAX_INT for seq in sequences.values()
    ):
        raise BackupError("备份的编号记录无效。")
    return payload


def _valid_value(value, column):
    if value is None:
        return not (column["notnull"] or column["pk"])
    datatype = column["type"].upper()
    if datatype == "TEXT":
        return type(value) is str
    if datatype == "INTEGER":
        return type(value) is int and (-_MAX_INT - 1) <= value <= _MAX_INT
    return datatype == "REAL" and type(value) in (float, int) and (
        math.isfinite(value) if type(value) is float else (-_MAX_INT - 1) <= value <= _MAX_INT
    )


def _valid_json_array(value, element_is_valid):
    try:
        items = json.loads(value)
    except (TypeError, ValueError, RecursionError):
        return False
    return isinstance(items, list) and all(element_is_valid(item) for item in items)


def _valid_reference(item):
    return isinstance(item, dict) and type(item.get("session_id")) is int and (
        item.get("message_id") is None or type(item["message_id"]) is int
    )


def _valid_summary_point(item):
    return (
        isinstance(item, dict) and isinstance(item.get("text"), str)
        and isinstance(item.get("source_ids"), list)
        and all(type(source) is int for source in item["source_ids"])
    )


def _validate_rows(db, tables):
    for name, table in tables.items():
        current = _columns(db, name)
        columns = table["columns"]
        expected = set(current)
        if name == "personas" and "web_search" not in columns:
            expected.remove("web_search")  # Earlier backups use the column default.
        if set(columns) != expected:
            raise BackupError(f"备份表 {name} 与当前版本的字段不匹配。")
        definitions = [current[column] for column in columns]
        if name == "messages":
            checks = {"references_json": _valid_reference, "audience_json": lambda item: isinstance(item, str)}
        elif name == "summaries":
            checks = {
                "keywords": lambda item: isinstance(item, str), "questions": lambda item: isinstance(item, str),
                "views": _valid_summary_point, "observations": _valid_summary_point,
            }
        elif name in {"bot_evolution_notes", "observer_observations"}:
            checks = {"keywords_json": lambda item: isinstance(item, str)}
        else:
            checks = {}
        checks_by_index = [(columns.index(field), check) for field, check in checks.items()]
        for row in table["rows"]:
            if not isinstance(row, list) or len(row) != len(columns) or not all(
                _valid_value(value, column) for value, column in zip(row, definitions)
            ):
                raise BackupError(f"备份表 {name} 含有无效的记录。")
            if any(row[index] is not None and not _valid_json_array(row[index], check) for index, check in checks_by_index):
                raise BackupError(f"备份表 {name} 含有无效的结构化内容。")
            if name == "config_settings" and row[columns.index("key")] not in _PUBLIC_SETTINGS:
                raise BackupError("备份包含不支持的设置。")


def _saved_credentials(db, providers):
    identity = ("kind", "base_url", "name")
    indices = [providers["columns"].index(field) for field in identity]
    incoming = Counter(tuple(row[index] for index in indices) for row in providers["rows"])
    local = db.execute("SELECT kind, base_url, name, api_key FROM providers").fetchall()
    counts = Counter(tuple(row[:3]) for row in local)
    return {
        tuple(row[:3]): row["api_key"]
        for row in local
        if row["api_key"] and counts[tuple(row[:3])] == 1 and incoming[tuple(row[:3])] == 1
    }


def restore_backup(db: sqlite3.Connection, payload: dict) -> dict:
    """Replace local rows inside the caller's SQLite transaction."""
    if not db.in_transaction or not db.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise RuntimeError("Restore requires an open SQLite transaction with foreign keys enabled.")
    tables = payload["tables"]
    _validate_rows(db, tables)
    credentials = _saved_credentials(db, tables["providers"])
    db.execute("SAVEPOINT restore_journal")
    try:
        db.execute("PRAGMA defer_foreign_keys = ON")
        for name in reversed(_TABLES):
            db.execute(f"DELETE FROM {_identifier(name)}")
        for name in _TABLES:
            table = tables[name]
            columns = ", ".join(map(_identifier, table["columns"]))
            placeholders = ", ".join("?" for _ in table["columns"])
            db.executemany(f"INSERT INTO {_identifier(name)} ({columns}) VALUES ({placeholders})", table["rows"])
        db.executemany(
            "UPDATE providers SET api_key = ? WHERE kind = ? AND base_url = ? AND name = ?",
            [(key, *identity) for identity, key in credentials.items()],
        )
        for name, sequence in payload["sequences"].items():
            updated = db.execute("UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?", (sequence, name))
            if not updated.rowcount:
                db.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (name, sequence))
        db.execute("UPDATE observer_jobs SET state = 'pending', ready_at = 0 WHERE state = 'running'")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise BackupError("备份中有缺失或无效的关联记录。")
    except Exception as exc:
        db.execute("ROLLBACK TO restore_journal")
        db.execute("RELEASE restore_journal")
        if isinstance(exc, (sqlite3.Error, UnicodeError, OverflowError)):
            raise BackupError("备份的记录有冲突，无法导入。") from exc
        raise
    db.execute("RELEASE restore_journal")
    return {
        "sessions": len(tables["sessions"]["rows"]), "messages": len(tables["messages"]["rows"]),
        "restored_api_keys": len(credentials), "exported_at": payload["exported_at"],
    }
