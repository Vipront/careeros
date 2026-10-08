# -*- coding: utf-8 -*-
"""
Tests for Database Schema Index Optimizations (tests/test_db_indexes.py).
Follows TDD (RED phase) before adding index creation into src/db.py:ensure_v2_schema.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.db import ensure_v2_schema


class TestDatabaseIndexOptimization(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(self.tmp.name)
        self.tmp.close()

        self.con = sqlite3.connect(self.db_path)
        self.con.execute("""
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                company TEXT,
                status TEXT,
                final_score REAL,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        self.con.execute("""
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                event_time TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT ''
            )
        """)
        self.con.commit()

    def tearDown(self):
        self.con.close()
        self.db_path.unlink(missing_ok=True)

    def test_ensure_v2_schema_creates_index(self):
        """Index idx_jobs_status_final_score should exist after ensure_v2_schema."""
        ensure_v2_schema(self.con)

        indexes = [row[1] for row in self.con.execute("PRAGMA index_list(jobs)").fetchall()]
        self.assertIn("idx_jobs_status_final_score", indexes)

    def test_ensure_v2_schema_creates_events_index(self):
        """Index idx_events_job_id should exist on events table after ensure_v2_schema."""
        ensure_v2_schema(self.con)

        indexes = [row[1] for row in self.con.execute("PRAGMA index_list(events)").fetchall()]
        self.assertIn("idx_events_job_id", indexes)

    def test_events_query_plan_uses_index(self):
        """Query on events by job_id should use idx_events_job_id instead of full scan."""
        self.con.execute("INSERT INTO events (job_id, event_type, event_time, note) VALUES (101, 'status_change', '2026-09-05', 'test')")
        self.con.commit()
        ensure_v2_schema(self.con)
        plan = self.con.execute("EXPLAIN QUERY PLAN SELECT event_type, event_time, note FROM events WHERE job_id = ? ORDER BY id ASC", (101,)).fetchall()
        plan_texts = [row[3] for row in plan]
        uses_index = any("USING INDEX idx_events_job_id" in text for text in plan_texts)
        self.assertTrue(uses_index, f"Events query plan did not use index: {plan_texts}")

    def test_ensure_v2_schema_is_idempotent(self):
        """Calling ensure_v2_schema multiple times must succeed without error."""
        ensure_v2_schema(self.con)
        # Call second time
        ensure_v2_schema(self.con)

        indexes = [row[1] for row in self.con.execute("PRAGMA index_list(jobs)").fetchall()]
        self.assertIn("idx_jobs_status_final_score", indexes)
        events_indexes = [row[1] for row in self.con.execute("PRAGMA index_list(events)").fetchall()]
        self.assertIn("idx_events_job_id", events_indexes)

    def test_query_plan_uses_index(self):
        """Query filtering by status and ordering by final_score should use the composite index."""
        # Insert sample rows
        rows = [
            ("Bioinformatician", "EMBL", "ready_for_review", 85.0),
            ("Technician", "LMU", "rejected", 30.0),
            ("Postdoc", "Max Planck", "ready_for_review", 90.0),
            ("Intern", "Roche", "low_priority", 50.0),
        ]
        self.con.executemany("INSERT INTO jobs (title, company, status, final_score) VALUES (?, ?, ?, ?)", rows)
        self.con.commit()

        ensure_v2_schema(self.con)

        plan = self.con.execute("""
            EXPLAIN QUERY PLAN
            SELECT id, title, final_score
            FROM jobs
            WHERE status = 'ready_for_review'
            ORDER BY final_score DESC
        """).fetchall()

        plan_texts = [row[3] for row in plan]
        uses_index = any("USING INDEX idx_jobs_status_final_score" in text for text in plan_texts)
        self.assertTrue(uses_index, f"Query plan did not use index: {plan_texts}")

    def test_data_integrity_preserved(self):
        """Existing data remains untouched when index is added."""
        self.con.execute("INSERT INTO jobs (title, company, status, final_score) VALUES ('Scientist', 'BioNTech', 'ready_for_review', 95.0)")
        self.con.commit()

        ensure_v2_schema(self.con)

        row = self.con.execute("SELECT title, company, status, final_score FROM jobs WHERE title = 'Scientist'").fetchone()
        self.assertEqual(row, ("Scientist", "BioNTech", "ready_for_review", 95.0))


if __name__ == "__main__":
    unittest.main()
