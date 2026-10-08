import json
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src import db
from tests.db_helpers import isolated_database, seed_job

ROOT = Path(__file__).resolve().parents[1]
class TestPipelineIntegrity(unittest.TestCase):

    def test_schema_validity(self):
        con = sqlite3.connect(":memory:")
        schema_text = (ROOT / "data" / "schema.sql").read_text(encoding="utf-8")
        con.executescript(schema_text)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        required = {"jobs", "applications", "events", "enrichment_cache"}
        self.assertTrue(required.issubset(tables), f"Missing tables: {required - tables}")
        con.close()

    def test_verify_setup(self):
        from src.verify_setup import run
        with isolated_database() as db_path:
            # Explicit paths are a local test override even if production Turso
            # credentials are present. A client construction here would be a bug.
            fake_client = SimpleNamespace(
                create_client_sync=lambda *_args, **_kwargs: self.fail("verify_setup attempted Turso access")
            )
            with (
                patch.object(db, "TURSO_URL", "libsql://must-not-be-used.invalid"),
                patch.object(db, "TURSO_TOKEN", "test-token"),
                patch.object(db, "libsql_client", fake_client),
            ):
                self.assertEqual(run(db_path=db_path), 0)

    def test_regression_fixture_cases(self):
        data = json.loads((ROOT / "data" / "regression_fixture_v1.json").read_text(encoding="utf-8"))
        cases = data.get("cases", [])
        self.assertGreater(len(cases), 0)

    def test_linkedin_crawler_targets(self):
        from src.collectors.linkedin_crawler import SEARCH_TARGETS
        self.assertEqual(len(SEARCH_TARGETS), 6)

    def test_telegram_bot_module(self):
        from src.telegram_bot import get_pending_review_jobs
        with isolated_database() as db_path:
            expected_id = seed_job(
                db_path,
                fingerprint="synthetic-bot-review",
                title="Synthetic Bot Review",
                company="Pipeline Test",
                status="ready_for_review",
                description="Bioinformatics analyst position in English. Python required. No prior experience required.",
            )
            con = db.get_connection(path=db_path)
            con.execute("""
                UPDATE jobs
                SET liveness_status = 'ACTIVE',
                    liveness_http_code = 200,
                    liveness_detail = 'liveness-v2:positive-posting - Page accessible and application open',
                    liveness_checked_at = datetime('now')
                WHERE id = ?
            """, (expected_id,))
            con.commit()
            con.close()
            jobs = get_pending_review_jobs(limit=5)
            self.assertEqual([job["id"] for job in jobs], [expected_id])
            self.assertEqual(jobs[0]["company"], "Pipeline Test")

    def test_pipeline_steps_include_quality_audit(self):
        from run_daily import STEPS_FINAL, STEPS_PROCESS

        self.assertEqual(STEPS_PROCESS[-1][0], "documents")
        final_step_names = [s[0] for s in STEPS_FINAL]
        self.assertIn("quality_audit", final_step_names)
        self.assertIn("telegram", final_step_names)
        audit_idx = final_step_names.index("quality_audit")
        telegram_idx = final_step_names.index("telegram")
        self.assertLess(audit_idx, telegram_idx)


if __name__ == "__main__":
    unittest.main()
