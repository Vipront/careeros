import unittest
from unittest.mock import MagicMock, patch
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import libsql_client
from src.db import transition, TursoConnection, TursoCursor, ALLOWED

class FakeResultSet:
    def __init__(self, columns, rows, rows_affected=0):
        self.columns = columns
        self.rows = rows
        self.rows_affected = rows_affected

class TestTransitionAtomicity(unittest.TestCase):
    def test_turso_batch_usage(self):
        """Test 1: Turso transition must execute UPDATE + INSERT in a single client.batch call."""
        mock_conn = MagicMock(spec=TursoConnection)
        mock_conn.client = MagicMock()

        # Simulate SELECT status -> "ready_for_review"
        mock_conn.execute.return_value = TursoCursor(FakeResultSet(["status"], [("ready_for_review",)]))

        with patch("src.db.get_connection", return_value=mock_conn):
            transition(123, "applied", note="test note")

        # Must call batch exactly once
        mock_conn.client.batch.assert_called_once()
        batch_args = mock_conn.client.batch.call_args[0][0]
        self.assertEqual(len(batch_args), 2, "Batch must contain exactly 2 statements")

        # First statement: UPDATE jobs
        stmt1 = batch_args[0]
        self.assertIn("UPDATE jobs SET status=", stmt1.sql)
        self.assertEqual(stmt1.args[0], "applied")
        self.assertEqual(stmt1.args[2], 123)

        # Second statement: INSERT INTO events
        stmt2 = batch_args[1]
        self.assertIn("INSERT INTO events", stmt2.sql)
        self.assertEqual(stmt2.args[0], 123)
        self.assertEqual(stmt2.args[1], "status_change")
        self.assertIn("ready_for_review -> applied: test note", stmt2.args[3])

    def test_sqlite_behavior(self):
        """Test 2: Standard SQLite connection must update jobs, insert event and commit."""
        db_file = ROOT / "data" / "test_transition_unit.db"
        if db_file.exists():
            db_file.unlink()

        mem_db = sqlite3.connect(str(db_file))
        mem_db.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, status TEXT, updated_at TEXT)")
        mem_db.execute("CREATE TABLE events (job_id INTEGER, event_type TEXT, event_time TEXT, note TEXT)")
        mem_db.execute("INSERT INTO jobs (id, status) VALUES (1, 'ready_for_review')")
        mem_db.commit()

        with patch("src.db.get_connection", return_value=mem_db):
            transition(1, "applied", note="sqlite test")

        check_db = sqlite3.connect(str(db_file))
        row = check_db.execute("SELECT status FROM jobs WHERE id=1").fetchone()
        self.assertEqual(row[0], "applied")

        event = check_db.execute("SELECT job_id, event_type, note FROM events WHERE job_id=1").fetchone()
        self.assertEqual(event[0], 1)
        self.assertEqual(event[1], "status_change")
        self.assertIn("ready_for_review -> applied: sqlite test", event[2])
        check_db.close()
        if db_file.exists():
            db_file.unlink()

    def test_invalid_transition(self):
        """Test 3: Invalid transition must raise ValueError without executing batch or updates."""
        mock_conn = MagicMock(spec=TursoConnection)
        mock_conn.client = MagicMock()
        mock_conn.execute.return_value = TursoCursor(FakeResultSet(["status"], [("rejected",)]))

        with patch("src.db.get_connection", return_value=mock_conn):
            with self.assertRaises(ValueError):
                transition(10, "applied")

        mock_conn.client.batch.assert_not_called()

    def test_turso_batch_failure(self):
        """Test 4: If client.batch() raises an exception, it must propagate and close connection."""
        mock_conn = MagicMock(spec=TursoConnection)
        mock_conn.client = MagicMock()
        mock_conn.execute.return_value = TursoCursor(FakeResultSet(["status"], [("ready_for_review",)]))
        mock_conn.client.batch.side_effect = RuntimeError("Network timeout")

        with patch("src.db.get_connection", return_value=mock_conn):
            with self.assertRaises(RuntimeError):
                transition(99, "applied")

        mock_conn.close.assert_called_once()

    def test_event_data_integrity(self):
        """Test 5: Batch event statement attributes match standard transition format."""
        mock_conn = MagicMock(spec=TursoConnection)
        mock_conn.client = MagicMock()
        mock_conn.execute.return_value = TursoCursor(FakeResultSet(["status"], [("evaluated",)]))

        with patch("src.db.get_connection", return_value=mock_conn):
            transition(555, "ready_for_review", note="quality check")

        batch_args = mock_conn.client.batch.call_args[0][0]
        event_stmt = batch_args[1]
        self.assertEqual(event_stmt.args[0], 555)
        self.assertEqual(event_stmt.args[1], "status_change")
        self.assertEqual(event_stmt.args[3], "evaluated -> ready_for_review: quality check")

    def test_losing_turso_update_does_not_write_a_phantom_event(self):
        class ConcurrentClient:
            def __init__(self):
                self.db = sqlite3.connect(":memory:")
                self.db.executescript(
                    "CREATE TABLE jobs(id INTEGER PRIMARY KEY, status TEXT, updated_at TEXT);"
                    "CREATE TABLE events(job_id INTEGER, event_type TEXT, event_time TEXT, note TEXT);"
                    "INSERT INTO jobs VALUES(1, 'ready_for_review', NULL);"
                )

            def execute(self, sql, args=()):
                cursor = self.db.execute(sql, args)
                rows = cursor.fetchall() if cursor.description else []
                columns = [item[0] for item in cursor.description] if cursor.description else []
                return FakeResultSet(columns, rows, cursor.rowcount)

            def batch(self, statements):
                # Another transition wins after this caller's status read.
                self.db.execute("UPDATE jobs SET status='applied' WHERE id=1")
                results = []
                for statement in statements:
                    cursor = self.db.execute(statement.sql, statement.args)
                    results.append(FakeResultSet([], [], cursor.rowcount))
                return results

        client = ConcurrentClient()
        connection = object.__new__(TursoConnection)
        connection.client = client

        with patch("src.db.get_connection", return_value=connection):
            with self.assertRaisesRegex(ValueError, "changed status concurrently"):
                transition(1, "applied", note="losing caller", connection=connection)

        self.assertEqual(client.db.execute("SELECT status FROM jobs WHERE id=1").fetchone()[0], "applied")
        self.assertEqual(client.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
        client.db.close()

if __name__ == "__main__":
    unittest.main()
