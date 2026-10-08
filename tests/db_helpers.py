"""Isolated SQLite database helpers for integration-style tests."""

from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

from src import db

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "data" / "schema.sql"


@contextmanager
def isolated_database():
    """Use a fresh, seeded SQLite DB while retaining the real db.py contract."""
    with tempfile.TemporaryDirectory(prefix="careeros-test-") as temp_dir:
        db_path = Path(temp_dir) / "jobs.db"
        bootstrap = sqlite3.connect(db_path)
        bootstrap.executescript(SCHEMA.read_text(encoding="utf-8"))
        bootstrap.close()

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {
                "TURSO_DB_URL": "",
                "TURSO_AUTH_TOKEN": "",
            }))
            stack.enter_context(patch.object(db, "TURSO_URL", None))
            stack.enter_context(patch.object(db, "TURSO_TOKEN", None))
            stack.enter_context(patch.object(db, "DB_PATH", db_path))
            stack.enter_context(patch.object(db, "_SQLITE_SCHEMA_CHECKED", False))
            stack.enter_context(patch.object(db, "_TURSO_SCHEMA_CHECKED", False))
            yield db_path


def seed_job(db_path, *, fingerprint, title, company, status, requirements="", description=""):
    """Insert one deterministic synthetic job and return its ID."""
    con = db.get_connection(path=db_path)
    try:
        cursor = con.execute(
            """INSERT INTO jobs (
                fingerprint, title, company, location, date_found, created_at,
                updated_at, status, requirements_text, description, final_score
            ) VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'), ?, ?, ?, ?)""",
            (fingerprint, title, company, "Test City", status, requirements, description, 80),
        )
        con.commit()
        return cursor.lastrowid
    finally:
        con.close()


def seed_event(db_path, job_id, note):
    con = db.get_connection(path=db_path)
    try:
        con.execute(
            "INSERT INTO events(job_id,event_type,event_time,note) VALUES(?,?,datetime('now'),?)",
            (job_id, "test_event", note),
        )
        con.commit()
    finally:
        con.close()
