import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from scripts.backup_database import create_backup
from scripts.backup_database import _open_connection
from src.ops import rollback


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("source,expected", [
    ("https://example.invalid", "https://example.invalid"),
    ("http://example.invalid:8080", "http://example.invalid:8080"),
    ("libsql://example.invalid", "libsql://example.invalid"),
])
def test_backup_uses_transaction_capable_transport(tmp_path, monkeypatch, source, expected):
    from src import db
    monkeypatch.setattr(db, "TURSO_URL", source)
    monkeypatch.setattr(db, "TURSO_TOKEN", "test-token")
    monkeypatch.setattr("scripts.backup_database._HttpSnapshotConnection", lambda url, token: (url, token))
    assert _open_connection(tmp_path) == (expected, "test-token")


def test_http_snapshot_keeps_baton_and_decodes_values(monkeypatch):
    from scripts.backup_database import _HttpSnapshotConnection

    calls = []

    class Session:
        headers = {}

        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            number = len(calls)

            class Response:
                status_code = 200

                def json(self):
                    return {"baton": f"snapshot-{number}", "results": [{"type": "ok", "response": {
                        "result": {"cols": [{"name": "id"}, {"name": "text"}], "rows": [] if number == 1 else [
                            [{"type": "integer", "value": "12"}, {"type": "text", "value": "value"}]
                        ]}
                    }}]}

            return Response()

        def close(self):
            pass

    monkeypatch.setattr("scripts.backup_database.requests.Session", Session)
    con = _HttpSnapshotConnection("https://example.invalid", "test-token")
    assert con.execute("SELECT id,text FROM jobs LIMIT 100 OFFSET ?", (100,)).fetchall() == [[12, "value"]]
    con.close()
    assert calls[0][1]["json"]["baton"] is None
    assert calls[1][1]["json"]["baton"] == "snapshot-1"
    assert calls[2][1]["json"]["baton"] == "snapshot-2"
    assert calls[2][1]["json"]["requests"] == [{"type": "close"}]
    assert all(call[1]["allow_redirects"] is False for call in calls)


def test_http_snapshot_refuses_lost_transaction(monkeypatch):
    from scripts.backup_database import _HttpSnapshotConnection

    class Session:
        headers = {}

        def post(self, *_args, **_kwargs):
            class Response:
                status_code = 200

                def json(self):
                    return {"baton": None, "results": [{"type": "ok"}]}

            return Response()

        def close(self):
            pass

    monkeypatch.setattr("scripts.backup_database.requests.Session", Session)
    with pytest.raises(RuntimeError, match="expired"):
        _HttpSnapshotConnection("https://example.invalid", "test-token")


@pytest.fixture
def workspace_tmp():
    scratch_root = REPO_ROOT / ".test_tmp"
    scratch_root.mkdir(exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="ops-audit-", dir=scratch_root))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _database(path: Path, title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT);"
            "CREATE TABLE events (id INTEGER PRIMARY KEY, job_id INTEGER);"
            "CREATE TABLE applications (id INTEGER PRIMARY KEY, job_id INTEGER);"
        )
        con.execute("INSERT INTO jobs(title) VALUES (?)", (title,))
        con.commit()
    finally:
        con.close()


def _title(path: Path) -> str:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT title FROM jobs").fetchone()[0]
    finally:
        con.close()


def test_logical_backup_is_private_atomic_and_does_not_overwrite(workspace_tmp):
    root = workspace_tmp / "app"
    source = root / "data" / "jobs.db"
    destination = root / "data" / "backups" / "snapshot.json"
    _database(source, "snapshot")
    con = sqlite3.connect(source)
    try:
        counts = create_backup(root, destination, con)
    finally:
        con.close()

    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["format"] == "careeros-logical-backup-v1"
    assert counts["jobs"] == 1
    if os.name != "nt":
        assert destination.stat().st_mode & 0o777 == 0o600
    duplicate_con = sqlite3.connect(source)
    try:
        with pytest.raises(FileExistsError):
            create_backup(root, destination, duplicate_con)
    finally:
        duplicate_con.close()
    assert list(destination.parent.glob(".backup-*")) == []


def test_remote_logical_backup_reads_through_one_transaction(workspace_tmp):
    class Result:
        def __init__(self, columns, rows):
            self.columns, self.rows = columns, rows

    class Transaction:
        def __init__(self):
            self.rolled_back = False
            self.closed = False

        def execute(self, sql, _parameters):
            if "FROM sqlite_master WHERE type='table'" in sql:
                return Result(["name", "sql"], [
                    ("jobs", "CREATE TABLE jobs(id INTEGER PRIMARY KEY, title TEXT)"),
                    ("events", "CREATE TABLE events(id INTEGER PRIMARY KEY)"),
                    ("applications", "CREATE TABLE applications(id INTEGER PRIMARY KEY)"),
                ])
            if sql.startswith('SELECT * FROM "jobs"'):
                return Result(["id", "title"], [(1, "remote")])
            if "sqlite_master WHERE type='index'" in sql:
                return Result(["sql"], [])
            if "FROM sqlite_sequence" in sql:
                return Result(["name", "seq"], [])
            return Result(["id"], [])

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    transaction = Transaction()

    class Client:
        def transaction(self):
            return transaction

    class RemoteConnection:
        client = Client()

    destination = workspace_tmp / "app" / "data" / "backups" / "remote.json"
    counts = create_backup(workspace_tmp / "app", destination, RemoteConnection())
    assert counts["jobs"] == 1
    assert transaction.rolled_back and transaction.closed
    tables = json.loads(destination.read_text(encoding="utf-8"))["tables"]
    assert next(table for table in tables if table["name"] == "jobs")["rows"] == [[1, "remote"]]


def test_restore_requires_offline_ack_and_validates_json_before_touching_db(workspace_tmp):
    root = workspace_tmp / "app"
    live_db = root / "data" / "jobs.db"
    backup_dir = root / "data" / "backups"
    _database(live_db, "live")
    backup_dir.mkdir(parents=True)
    invalid = backup_dir / "invalid.json"
    invalid.write_text('{"format":"careeros-logical-backup-v1","tables":[]}', encoding="utf-8")

    missing_ack = rollback.execute_rollback(invalid.name, root=root)
    invalid_backup = rollback.execute_rollback(
        invalid.name, root=root, confirmation=rollback.OFFLINE_CONFIRMATION
    )

    assert missing_ack["status"] == "FAILED"
    assert invalid_backup["status"] == "FAILED"
    assert _title(live_db) == "live"
    assert invalid.exists()


def test_restore_requires_clean_wal_state_and_preserves_backup(workspace_tmp):
    root = workspace_tmp / "app"
    live_db = root / "data" / "jobs.db"
    backup = root / "data" / "backups" / "known-good.db"
    _database(live_db, "live")
    _database(backup, "restored")
    wal = live_db.with_name(live_db.name + "-wal")
    wal.write_bytes(b"active writer marker")

    result = rollback.execute_rollback(
        backup.name, root=root, confirmation=rollback.OFFLINE_CONFIRMATION
    )
    assert result["status"] == "FAILED"
    assert wal.exists()
    assert _title(live_db) == "live"
    assert backup.exists()


def test_locked_restore_times_out_without_changing_source_or_database(workspace_tmp, monkeypatch):
    root = workspace_tmp / "app"
    live_db = root / "data" / "jobs.db"
    backup = root / "data" / "backups" / "known-good.db"
    _database(live_db, "live")
    _database(backup, "restored")
    writer = sqlite3.connect(live_db, timeout=0.1)
    try:
        writer.execute("BEGIN EXCLUSIVE")
        monkeypatch.setattr(rollback, "RESTORE_TIMEOUT_SECONDS", 0.15)
        result = rollback.execute_rollback(
            backup.name, root=root, confirmation=rollback.OFFLINE_CONFIRMATION
        )
        assert result["status"] == "FAILED"
        assert "remained locked" in result["reason"]
        assert writer.execute("SELECT title FROM jobs").fetchone()[0] == "live"
    finally:
        writer.rollback()
        writer.close()
    assert backup.exists()


def test_valid_sqlite_and_logical_backups_restore_without_consuming_source(workspace_tmp):
    root = workspace_tmp / "app"
    live_db = root / "data" / "jobs.db"
    backup_dir = root / "data" / "backups"
    sqlite_backup = backup_dir / "known-good.db"
    logical_backup = backup_dir / "logical.json"
    _database(live_db, "live")
    _database(sqlite_backup, "restored-db")

    result = rollback.execute_rollback(
        sqlite_backup.name, root=root, confirmation=rollback.OFFLINE_CONFIRMATION
    )
    assert result["status"] == "ROLLBACK_SUCCESSFUL"
    assert _title(live_db) == "restored-db"
    assert sqlite_backup.exists()

    con = sqlite3.connect(sqlite_backup)
    try:
        create_backup(root, logical_backup, con)
    finally:
        con.close()
    result = rollback.execute_rollback(
        logical_backup.name, root=root, confirmation=rollback.OFFLINE_CONFIRMATION
    )
    assert result["status"] == "ROLLBACK_SUCCESSFUL"
    assert _title(live_db) == "restored-db"
    assert logical_backup.exists()
    assert not list(live_db.parent.glob(".restore-*.db"))
