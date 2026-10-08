import contextlib
from datetime import datetime, timedelta, timezone
import io
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "data" / "schema.sql"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@contextlib.contextmanager
def temporary_jobs_database():
    with tempfile.TemporaryDirectory(prefix="careeros-reliability-") as temp_dir:
        db_path = Path(temp_dir) / "jobs.db"
        con = sqlite3.connect(db_path)
        con.executescript(SCHEMA.read_text(encoding="utf-8"))
        con.close()
        yield db_path


class _SignalWriter:
    def __init__(self, marker):
        self.buffer = io.StringIO()
        self.marker = marker

    def write(self, value):
        self.buffer.write(value)
        if "READY" in value:
            self.marker.write_text("streamed", encoding="utf-8")
        return len(value)

    def flush(self):
        return None


class TestPipelineReliability(unittest.TestCase):
    def test_run_step_streams_output_while_child_is_running(self):
        from run_daily import run_step

        with tempfile.TemporaryDirectory(prefix="careeros-step-") as temp_dir:
            marker = Path(temp_dir) / "streamed.txt"
            writer = _SignalWriter(marker)
            script = "\n".join((
                "import pathlib, sys, time",
                "marker = pathlib.Path(sys.argv[1])",
                "print('READY', flush=True)",
                "deadline = time.monotonic() + 2",
                "while not marker.exists() and time.monotonic() < deadline:",
                "    time.sleep(.01)",
                "print('DONE', flush=True)",
            ))
            failures = []
            with patch("sys.stdout", writer):
                run_step("stream-check", [sys.executable, "-c", script, str(marker)], failures, timeout=3)
            self.assertEqual(failures, [])
            self.assertTrue(marker.exists(), "Parent should receive READY before the child proceeds")
            self.assertIn("DONE", writer.buffer.getvalue())

    def test_run_step_kills_and_reaps_child_at_injected_timeout(self):
        from run_daily import run_step

        failures = []
        output = io.StringIO()
        started = time.monotonic()
        with patch("sys.stdout", output):
            run_step("timeout-check", [sys.executable, "-c", "import time; time.sleep(5)"], failures, timeout=0.1)
        self.assertEqual(failures, ["timeout-check"])
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn("TIMEOUT EXPIRED", output.getvalue())

    def test_rate_limit_sets_process_global_timestamp_and_persistent_cooldown(self):
        from src import llm_judge

        with temporary_jobs_database() as db_path:
            con = sqlite3.connect(db_path)
            con.execute(
                """INSERT INTO jobs (
                    fingerprint,title,company,date_found,created_at,updated_at,
                    status,description_available,profile_type,semantic_score,description
                ) VALUES ('retry-test','Synthetic role','Synthetic Co',datetime('now'),
                    datetime('now'),datetime('now'),'evaluated',1,'Bioinformatics',0.8,'Synthetic description')"""
            )
            con.commit()
            con.close()

            def connect():
                return sqlite3.connect(db_path)

            with (
                patch.object(llm_judge, "get_connection", side_effect=connect),
                patch.object(llm_judge, "call_claude", side_effect=RuntimeError("HTTP 429 rate limited")) as model_call,
                patch.object(llm_judge, "trace_llm_judge"),
                patch.object(llm_judge, "RATE_LIMIT_DELAY_SECONDS", 0),
                patch.object(llm_judge, "GLOBAL_COOLDOWN_SECONDS", 0),
                patch.object(llm_judge, "RATE_LIMIT_RETRY_SECONDS", 60),
                patch.object(llm_judge, "LAST_429_TIMESTAMP", 0.0),
                patch.object(llm_judge.time, "sleep"),
            ):
                llm_judge.run(limit=1)
                self.assertEqual(model_call.call_count, 1)
                verify = sqlite3.connect(db_path)
                saved = verify.execute(
                    "SELECT llm_judge_status,llm_judge_next_retry_at FROM jobs WHERE fingerprint='retry-test'"
                ).fetchone()
                verify.close()
                self.assertEqual(saved[0], "rate_limited")
                retry_at = datetime.fromisoformat(saved[1].replace("Z", "+00:00"))
                self.assertGreaterEqual(retry_at, datetime.now(timezone.utc) + timedelta(seconds=45))
                self.assertGreater(llm_judge.LAST_429_TIMESTAMP, 0)

                # A subsequent run before the saved deadline must not call the model again.
                llm_judge.run(limit=1)
                self.assertEqual(model_call.call_count, 1)

    def test_ready_failed_jobs_respect_backoff_and_terminal_state(self):
        from src import llm_judge

        with temporary_jobs_database() as db_path:
            con = sqlite3.connect(db_path)
            future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
            for fingerprint, terminal in (("ready-failed-retry", 0), ("ready-failed-terminal", 1)):
                con.execute(
                    """INSERT INTO jobs (
                        fingerprint,title,company,date_found,created_at,updated_at,status,
                        description_available,profile_type,semantic_score,llm_judge_status,
                        llm_judge_next_retry_at,llm_judge_terminal
                    ) VALUES (?, ?, 'Synthetic Co', datetime('now'), datetime('now'), datetime('now'),
                        'ready_for_review', 1, 'Bioinformatics', 0.8, 'failed', ?, ?)""",
                    (fingerprint, fingerprint, future, terminal),
                )
            con.commit()
            con.close()

            def connect():
                return sqlite3.connect(db_path)

            with (
                patch.object(llm_judge, "get_connection", side_effect=connect),
                patch.object(llm_judge, "call_claude") as model_call,
                patch.object(llm_judge, "trace_llm_judge"),
            ):
                llm_judge.run(limit=10)
                model_call.assert_not_called()

    def test_pending_count_respects_future_iso_retry_timestamp(self):
        from run_daily import get_pending_count
        from src import db

        with temporary_jobs_database() as db_path:
            con = sqlite3.connect(db_path)
            con.execute(
                """INSERT INTO jobs (
                    fingerprint,title,company,date_found,created_at,updated_at,status,
                    description_available,semantic_score,llm_judge_status,llm_judge_next_retry_at,profile_type
                ) VALUES ('pending-test','Synthetic role','Synthetic Co',datetime('now'),
                    datetime('now'),datetime('now'),'evaluated',1,0.8,'rate_limited',?,'Bioinformatics')""",
                ((datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),),
            )
            con.commit()
            con.close()

            with patch.object(db, "get_connection", side_effect=lambda: sqlite3.connect(db_path)):
                self.assertEqual(get_pending_count(), 0)
                con = sqlite3.connect(db_path)
                con.execute(
                    "UPDATE jobs SET llm_judge_next_retry_at=? WHERE fingerprint='pending-test'",
                    ((datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(),),
                )
                con.commit()
                con.close()
                self.assertEqual(get_pending_count(), 1)

    def test_retention_counts_only_successful_removals(self):
        from run_daily import cleanup_old_outputs
        from unittest.mock import patch as mock_patch
        import shutil

        with tempfile.TemporaryDirectory(prefix="careeros-retention-failure-") as temp_dir:
            old_folder = Path(temp_dir) / (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
            old_folder.mkdir()
            with mock_patch.object(shutil, "rmtree", side_effect=OSError("simulated removal failure")):
                removed = cleanup_old_outputs(days=30, output_dir=temp_dir)
            self.assertEqual(removed, 0)
            self.assertTrue(old_folder.exists())

    def test_retention_skips_date_named_symlink(self):
        from run_daily import cleanup_old_outputs

        with tempfile.TemporaryDirectory(prefix="careeros-retention-link-") as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "output"
            output_dir.mkdir()
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")
            link = output_dir / (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"Directory symlinks are unavailable: {exc}")
            removed = cleanup_old_outputs(days=30, output_dir=output_dir)
            self.assertEqual(removed, 0)
            self.assertTrue(sentinel.exists())
            self.assertTrue(link.is_symlink())


if __name__ == "__main__":
    unittest.main()
