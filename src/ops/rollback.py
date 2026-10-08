"""Explicit offline restore for validated local SQLite database backups."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from typing import Iterator
import re

ROOT = Path(__file__).resolve().parents[2]
OFFLINE_CONFIRMATION = "RESTORE_LOCAL_SQLITE_OFFLINE"
RESTORE_TIMEOUT_SECONDS = 15.0


@contextmanager
def _exclusive_pipeline_lock(lock_path: Path) -> Iterator[bool]:
    """Share run_daily's cross-platform lock so restore cannot race the pipeline."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = lock_path.open("a+")
    except OSError:
        yield False
        return
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError:
                pass
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                pass
        yield locked
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _validate_database(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("Backup is missing or empty")
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        integrity = con.execute("PRAGMA quick_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise ValueError("Backup failed SQLite quick_check")
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"jobs", "events", "applications"}.issubset(tables):
            raise ValueError("Backup is not a CareerOS SQLite database")
    finally:
        con.close()


def _materialize_logical_backup(source: Path, destination: Path) -> None:
    """Convert the versioned logical export into a validated temporary SQLite DB."""
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Could not read logical backup JSON") from exc
    if not isinstance(payload, dict) or payload.get("format") != "careeros-logical-backup-v1":
        raise ValueError("Unsupported logical backup format")
    tables = payload.get("tables")
    indexes = payload.get("indexes", [])
    sequences = payload.get("sequences", [])
    if not isinstance(tables, list) or not isinstance(indexes, list) or not isinstance(sequences, list):
        raise ValueError("Logical backup tables, indexes, or sequences are invalid")

    con = sqlite3.connect(destination)
    try:
        con.execute("PRAGMA foreign_keys=OFF")
        seen: set[str] = set()
        for table in tables:
            if not isinstance(table, dict):
                raise ValueError("Logical backup contains an invalid table")
            name, schema = table.get("name"), table.get("schema")
            columns, rows = table.get("columns"), table.get("rows")
            if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
                    or name in seen):
                raise ValueError("Logical backup contains an unsafe or duplicate table name")
            if (not isinstance(schema, str) or not schema.lstrip().upper().startswith("CREATE TABLE")
                    or not isinstance(columns, list) or not all(isinstance(col, str) for col in columns)
                    or not isinstance(rows, list)):
                raise ValueError(f"Logical backup table {name} has invalid schema or rows")
            # execute() accepts exactly one statement; multi-statement payloads are rejected.
            con.execute(schema)
            actual_columns = [row[1] for row in con.execute(f'PRAGMA table_info("{name}")')]
            if actual_columns != columns:
                raise ValueError(f"Logical backup columns do not match table {name}")
            seen.add(name)
            placeholders = ",".join("?" for _ in columns)
            if columns:
                insert = f'INSERT INTO "{name}" VALUES ({placeholders})'
                for row in rows:
                    if (not isinstance(row, list) or len(row) != len(columns)
                            or any(value is not None and not isinstance(value, (str, int, float)) for value in row)):
                        raise ValueError(f"Logical backup contains invalid row data for {name}")
                    con.execute(insert, row)
            elif rows:
                raise ValueError(f"Logical backup has rows without columns for {name}")
        if not {"jobs", "events", "applications"}.issubset(seen):
            raise ValueError("Logical backup is missing required CareerOS tables")
        for statement in indexes:
            if not isinstance(statement, str) or not re.match(r"^\s*CREATE\s+(UNIQUE\s+)?INDEX\b", statement, re.I):
                raise ValueError("Logical backup contains an invalid index definition")
            con.execute(statement)
        for sequence in sequences:
            if (not isinstance(sequence, list) or len(sequence) != 2
                    or not isinstance(sequence[0], str) or sequence[0] not in seen
                    or not isinstance(sequence[1], int) or sequence[1] < 0):
                raise ValueError("Logical backup contains invalid AUTOINCREMENT state")
            updated = con.execute("UPDATE sqlite_sequence SET seq=? WHERE name=?", (sequence[1], sequence[0]))
            if updated.rowcount == 0:
                con.execute("INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)", (sequence[0], sequence[1]))
        violations = con.execute("PRAGMA foreign_key_check").fetchone()
        if violations:
            raise ValueError("Logical backup contains foreign-key violations")
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    _validate_database(destination)


def _publish_new_database(source: Path, destination: Path) -> None:
    """Atomically create a private DB without replacing a concurrently-created file."""
    if os.name == "nt":
        os.rename(source, destination)
    else:
        os.link(source, destination)
        source.unlink()


def execute_rollback(
    backup_name: str | None = None,
    *,
    root: Path = ROOT,
    confirmation: str | None = None,
) -> dict[str, str]:
    """Restore a .db backup only after explicit offline acknowledgement.

    Logical JSON exports are reconstructed in a temporary SQLite database,
    validated, then restored through SQLite's backup API. The source is retained.
    """
    root = Path(root).resolve()
    backup_dir = root / "data" / "backups"
    live_db = root / "data" / "jobs.db"
    if confirmation != OFFLINE_CONFIRMATION:
        return {"status": "FAILED", "reason": f"Explicit offline confirmation required: {OFFLINE_CONFIRMATION}"}
    if not backup_dir.is_dir():
        return {"status": "FAILED", "reason": "No backup directory found"}

    if backup_name is None:
        backups = sorted((*backup_dir.glob("*.db"), *backup_dir.glob("*.json")),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        if not backups:
            return {"status": "FAILED", "reason": "No SQLite or logical JSON backups available"}
        source = backups[0]
    else:
        name = Path(backup_name)
        if name.name != backup_name or name.suffix.lower() not in {".db", ".json"}:
            return {"status": "FAILED", "reason": "Restore accepts only a .db or logical .json backup filename"}
        source = backup_dir / name

    try:
        source = source.resolve()
        if not source.is_file():
            raise ValueError("Backup file does not exist")
        if not source.is_relative_to(backup_dir.resolve()) or source == backup_dir.resolve():
            raise ValueError("Backup must remain inside data/backups")
        if source == live_db.resolve():
            raise ValueError("Cannot restore the live database from itself")
        if source.suffix.lower() == ".db":
            _validate_database(source)
        else:
            payload = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("format") != "careeros-logical-backup-v1":
                raise ValueError("Unsupported logical backup format")
    except (OSError, ValueError, sqlite3.Error) as exc:
        return {"status": "FAILED", "reason": str(exc)}

    lock_path = root / "data" / "pipeline.lock"
    with _exclusive_pipeline_lock(lock_path) as locked:
        if not locked:
            return {"status": "FAILED", "reason": "Pipeline is active; stop all services and retry"}

        wal_file = live_db.with_name(live_db.name + "-wal")
        shm_file = live_db.with_name(live_db.name + "-shm")
        if wal_file.exists() or shm_file.exists():
            return {"status": "FAILED", "reason": "SQLite WAL/SHM files exist; no files were removed. Stop services and checkpoint the database first"}

        live_db.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            fd, temporary_name = tempfile.mkstemp(prefix=".restore-", suffix=".db", dir=live_db.parent)
            os.close(fd)
            temporary = Path(temporary_name)
            if source.suffix.lower() == ".json":
                _materialize_logical_backup(source, temporary)
            else:
                shutil.copyfile(source, temporary)
            os.chmod(temporary, 0o600)
            with temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            _validate_database(temporary)

            if not live_db.exists():
                _publish_new_database(temporary, live_db)
                temporary = None
                timestamp = datetime.now(timezone.utc).isoformat()
                with (root / "data" / "rollback_audit.log").open("a", encoding="utf-8") as handle:
                    handle.write(f"[{timestamp}] SQLITE_RESTORE: {source.name} -> {live_db.name}\n")
                return {"status": "ROLLBACK_SUCCESSFUL", "restored_from": str(source),
                        "restored_to": str(live_db), "timestamp": timestamp}

            # Use SQLite's online backup API so the database itself holds the
            # destination write lock throughout the restore; never unlink WAL files.
            source_con = sqlite3.connect(temporary.as_uri() + "?mode=ro", uri=True, timeout=5)
            target_con = sqlite3.connect(live_db, timeout=0.2)
            try:
                deadline = time.monotonic() + RESTORE_TIMEOUT_SECONDS

                def stop_if_database_stays_busy(status: int, _remaining: int, _total: int) -> None:
                    if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) and time.monotonic() >= deadline:
                        raise TimeoutError("SQLite remained locked; restore stopped without deleting WAL files")

                source_con.backup(target_con, pages=128, progress=stop_if_database_stays_busy, sleep=0.05)
            finally:
                target_con.close()
                source_con.close()
            temporary.unlink()
            temporary = None
            timestamp = datetime.now(timezone.utc).isoformat()
            with (root / "data" / "rollback_audit.log").open("a", encoding="utf-8") as handle:
                handle.write(f"[{timestamp}] SQLITE_RESTORE: {source.name} -> {live_db.name}\n")
            return {"status": "ROLLBACK_SUCCESSFUL", "restored_from": str(source),
                    "restored_to": str(live_db), "timestamp": timestamp}
        except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
            return {"status": "FAILED", "reason": str(exc)}
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore a validated local SQLite backup while the app is offline")
    parser.add_argument("backup_name", nargs="?", help=".db or careeros-logical-backup-v1 .json filename")
    parser.add_argument("--confirm-offline", required=True, choices=[OFFLINE_CONFIRMATION])
    args = parser.parse_args(argv)
    result = execute_rollback(args.backup_name, confirmation=args.confirm_offline)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "ROLLBACK_SUCCESSFUL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
