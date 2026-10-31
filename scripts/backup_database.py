"""Create a private, no-overwrite logical database backup."""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any



def _collect_backup(con: Any) -> dict[str, Any]:
    """Read all tables in one SQLite snapshot where the driver supports it."""
    owns_snapshot = isinstance(con, sqlite3.Connection) and not con.in_transaction
    if owns_snapshot:
        con.execute("BEGIN")
    try:
        schemas = con.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        backup: dict[str, Any] = {"format": "careeros-logical-backup-v1", "tables": []}
        for name, schema in schemas:
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("Unsupported table identifier")
            columns = [column[0] for column in con.execute(f'SELECT * FROM "{name}" LIMIT 0').description]
            rowid = next((key for key in ("rowid", "_rowid_", "oid")
                          if key not in {col.lower() for col in columns}), None)
            if re.search(r"\bWITHOUT\s+ROWID\b", schema, re.I):
                rowid = None
            values, offset, last_rowid = [], 0, None
            while True:
                if rowid:
                    # Seek past the previous page instead of scanning it again for OFFSET.
                    condition = "" if last_rowid is None else f"WHERE {rowid} > ?"
                    args = () if last_rowid is None else (last_rowid,)
                    cursor = con.execute(
                        f'SELECT {rowid}, * FROM "{name}" {condition} ORDER BY {rowid} LIMIT 100', args)
                    raw_page = cursor.fetchall()
                    if raw_page:
                        last_rowid = raw_page[-1][0]
                    page = [row[1:] for row in raw_page]
                else:
                    # Unusual WITHOUT ROWID or fully shadowed tables retain the compatible path.
                    cursor = con.execute(f'SELECT * FROM "{name}" LIMIT 100 OFFSET ?', (offset,))
                    page = cursor.fetchall()
                values.extend([list(row) for row in page])
                if len(page) < 100:
                    break
                offset += 100
            backup["tables"].append({"name": name, "schema": schema, "columns": columns, "rows": values})
        backup["indexes"] = [
            row[0] for row in con.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY name"
            ).fetchall()
        ]
        try:
            backup["sequences"] = [list(row) for row in con.execute(
                "SELECT name,seq FROM sqlite_sequence ORDER BY name"
            ).fetchall()]
        except Exception as exc:
            if "no such table" not in str(exc).lower():
                raise
            backup["sequences"] = []
        return backup
    finally:
        if owns_snapshot:
            con.rollback()


def _publish_exclusive(destination: Path, payload: dict[str, Any]) -> None:
    """Write beside the destination, then atomically publish without clobbering."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".backup-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        # Windows rename is atomic and refuses an existing destination; POSIX
        # hard-link creation has the same no-clobber property on one filesystem.
        if os.name == "nt":
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Some platforms do not support fsync on directory handles.
            pass
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def create_backup(root: Path, destination: Path, con: Any) -> dict[str, int]:
    root = root.resolve()
    destination = destination.resolve()
    backup_dir = (root / "data" / "backups").resolve()
    if not destination.is_relative_to(backup_dir) or destination == backup_dir:
        raise ValueError("Backup must remain under the application's data/backups directory")
    payload = _collect_backup(con)
    _publish_exclusive(destination, payload)
    return {table["name"]: len(table["rows"]) for table in payload["tables"]}


def _open_connection(root: Path):
    return sqlite3.connect((root / "data" / "jobs.db").as_uri() + "?mode=ro", uri=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    destination = args.destination
    con = _open_connection(root)
    try:
        counts = create_backup(root, destination, con)
        print(json.dumps({"backup_created": True, "table_counts": counts}))
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
