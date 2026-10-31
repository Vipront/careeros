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


def test_backup_reads_large_table_in_linear_work():
    from scripts.backup_database import _collect_backup
    con = sqlite3.connect(':memory:')
    con.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY, title TEXT)')
    expected = [(i, str(i)) for i in range(-5, 4995)]
    con.executemany('INSERT INTO jobs VALUES (?,?)', expected)
    steps = 0

    def progress():
        nonlocal steps
        steps += 100
        return 0

    con.set_progress_handler(progress, 100)
    try:
        payload = _collect_backup(con)
        assert payload['tables'][0]['rows'] == [list(row) for row in expected]
        assert steps < 100_000, f'Backup repeatedly scanned old rows: {steps} VM steps'
    finally:
        con.close()


@pytest.mark.parametrize('schema', [
    'CREATE TABLE jobs(id TEXT PRIMARY KEY, title TEXT) WITHOUT ROWID',
    'CREATE TABLE jobs(rowid INTEGER, _rowid_ INTEGER, oid INTEGER, title TEXT)',
])
def test_backup_preserves_tables_without_accessible_rowid(schema):
    from scripts.backup_database import _collect_backup
    con = sqlite3.connect(':memory:')
    con.execute(schema)
    count = len(con.execute('SELECT * FROM jobs').description)
    rows = [tuple(str(i) for _ in range(count)) for i in range(105)]
    con.executemany('INSERT INTO jobs VALUES (' + ','.join('?' for _ in range(count)) + ')', rows)
    try:
        payload = _collect_backup(con)
        assert len(payload['tables'][0]['rows']) == 105
        assert set(map(tuple, payload['tables'][0]['rows'])) == set(con.execute('SELECT * FROM jobs'))
    finally:
        con.close()








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
