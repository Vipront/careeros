import sqlite3
from types import SimpleNamespace

import pytest

from src import db


class _FlakySchemaClient:
    def __init__(self, connection):
        self.connection = connection
        self.fail_inspection_once = True

    def execute(self, sql, args=()):
        if sql == "PRAGMA table_info(jobs)" and self.fail_inspection_once:
            self.fail_inspection_once = False
            raise RuntimeError("temporary schema inspection failure")
        cursor = self.connection.execute(sql, args)
        rows = cursor.fetchall() if cursor.description else []
        columns = [item[0] for item in cursor.description] if cursor.description else []
        return SimpleNamespace(columns=columns, rows=rows, rows_affected=cursor.rowcount)

    def close(self):
        self.connection.close()


def test_turso_schema_check_retries_after_failed_inspection(monkeypatch):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, status TEXT, final_score REAL)")
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, job_id INTEGER)")
    turso = object.__new__(db.TursoConnection)
    turso.client = _FlakySchemaClient(con)
    monkeypatch.setattr(db, "_TURSO_SCHEMA_CHECKED", False)

    with pytest.raises(RuntimeError, match="Could not inspect jobs schema"):
        db.ensure_v2_schema(turso)
    assert db._TURSO_SCHEMA_CHECKED is False

    assert db.ensure_v2_schema(turso) is True
    assert db._TURSO_SCHEMA_CHECKED is True
    columns = {row[1] for row in con.execute("PRAGMA table_info(jobs)").fetchall()}
    assert "application_materials_path" in columns
    con.close()


def test_schema_migration_failure_is_observable_and_not_cached(monkeypatch):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, status TEXT, final_score REAL)")
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, job_id INTEGER)")
    turso = object.__new__(db.TursoConnection)
    client = _FlakySchemaClient(con)
    client.fail_inspection_once = False
    original_execute = client.execute

    def fail_column_add(sql, args=()):
        if sql.startswith("ALTER TABLE jobs ADD COLUMN raw_html"):
            raise RuntimeError("DDL unavailable")
        return original_execute(sql, args)

    client.execute = fail_column_add
    turso.client = client
    monkeypatch.setattr(db, "_TURSO_SCHEMA_CHECKED", False)

    with pytest.raises(RuntimeError, match="Could not add required jobs column: raw_html"):
        db.ensure_v2_schema(turso)
    assert db._TURSO_SCHEMA_CHECKED is False
    con.close()
