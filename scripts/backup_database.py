"""Create a private, no-overwrite logical database backup."""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests


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
            values, offset, columns = [], 0, []
            while True:
                cursor = con.execute(f'SELECT * FROM "{name}" LIMIT 100 OFFSET ?', (offset,))
                columns = [column[0] for column in cursor.description]
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


class _TransactionCursor:
    def __init__(self, result: Any):
        self.description = [(name, None, None, None, None, None, None) for name in result.columns]
        self._rows = result.rows

    def fetchall(self):
        return self._rows


class _TransactionReader:
    def __init__(self, transaction: Any):
        self.transaction = transaction

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()):
        return _TransactionCursor(self.transaction.execute(sql, list(parameters)))


def _collect_consistent_backup(con: Any) -> dict[str, Any]:
    """Use a driver transaction for remote LibSQL snapshots when available."""
    client = getattr(con, "client", None)
    transaction_factory = getattr(client, "transaction", None)
    if not callable(transaction_factory):
        return _collect_backup(con)
    transaction = transaction_factory()
    try:
        return _collect_backup(_TransactionReader(transaction))
    finally:
        try:
            transaction.rollback()
        finally:
            transaction.close()


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
    payload = _collect_consistent_backup(con)
    _publish_exclusive(destination, payload)
    return {table["name"]: len(table["rows"]) for table in payload["tables"]}


class _HttpSnapshotConnection:
    """Read one consistent snapshot through Turso's HTTP transaction baton."""

    def __init__(self, url: str, token: str):
        parsed = urlsplit(url)
        scheme = {"libsql": "https", "wss": "https", "ws": "http"}.get(parsed.scheme, parsed.scheme)
        self.url = urlunsplit(parsed._replace(scheme=scheme, path="/v2/pipeline", query="", fragment=""))
        self.baton = None
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers["Authorization"] = f"Bearer {token}"
        try:
            self.execute("BEGIN DEFERRED")
        except Exception:
            self.session.close()
            raise

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()):
        arguments = [{"type": "integer", "value": str(value)} for value in parameters]
        response = self.session.post(self.url, json={
            "baton": self.baton,
            "requests": [{"type": "execute", "stmt": {"sql": sql, "args": arguments, "want_rows": True}}],
        }, timeout=60, allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError("Snapshot HTTP request failed")
        body = response.json()
        self.baton = body.get("baton")
        result = body["results"][0]
        if result.get("type") != "ok" or not self.baton:
            if "no such table: sqlite_sequence" in result.get("error", {}).get("message", ""):
                raise sqlite3.OperationalError("no such table: sqlite_sequence")
            raise RuntimeError("Snapshot transaction failed or expired")
        base_url = body.get("base_url")
        if base_url:
            original, target = urlsplit(self.url), urlsplit(base_url)
            if (original.scheme, original.netloc) != (target.scheme, target.netloc):
                raise RuntimeError("Snapshot endpoint changed origin")
            self.url = base_url.rstrip("/") + "/v2/pipeline"
        raw = result["response"]["result"]

        def decode(cell):
            kind = cell["type"]
            if kind == "null":
                return None
            if kind == "integer":
                return int(cell["value"])
            if kind == "float":
                return float(cell["value"])
            if kind == "text":
                return cell["value"]
            raise ValueError("Unsupported snapshot value type")

        class Cursor:
            description = [(column["name"],) for column in raw["cols"]]

            def fetchall(self):
                return [[decode(cell) for cell in row] for row in raw["rows"]]

        return Cursor()

    def close(self):
        try:
            if self.baton:
                self.session.post(self.url, json={"baton": self.baton, "requests": [{"type": "close"}]},
                                  timeout=15, allow_redirects=False)
        finally:
            self.session.close()


def _open_connection(root: Path):
    sys.path.insert(0, str(root))
    from src import db  # noqa: PLC0415

    if db.TURSO_URL and db.TURSO_TOKEN:
        # The pinned client's HTTP transport has no interactive transactions;
        # current Turso endpoints may also reject its legacy WebSocket protocol.
        return _HttpSnapshotConnection(db.TURSO_URL, db.TURSO_TOKEN)
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
