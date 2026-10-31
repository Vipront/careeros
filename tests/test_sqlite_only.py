import sqlite3

import pytest

from src import db
from scripts.backup_database import _open_connection
from src.evaluation.quality_audit import get_connection_for_audit


def test_old_remote_environment_cannot_select_a_remote_backend(monkeypatch):
    monkeypatch.setenv('TURSO_DB_URL', 'https://obsolete.invalid')
    monkeypatch.setenv('TURSO_AUTH_TOKEN', 'obsolete-test-value')
    assert db.get_connection_provider() == 'SQLite'
    con = db.get_connection()
    try:
        assert isinstance(con, sqlite3.Connection)
        assert con.execute('PRAGMA foreign_keys').fetchone() == (1,)
        assert con.execute('PRAGMA journal_mode').fetchone() == ('wal',)
    finally:
        con.close()


def test_backup_opens_only_readonly_local_database(tmp_path):
    (tmp_path / 'data').mkdir()
    with sqlite3.connect(tmp_path / 'data/jobs.db') as con:
        con.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY)')
    con = _open_connection(tmp_path)
    try:
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            con.execute('INSERT INTO jobs VALUES(1)')
    finally:
        con.close()


def test_removed_provider_is_rejected():
    with pytest.raises(ValueError, match='Unknown database provider'):
        get_connection_for_audit(provider='turso')
