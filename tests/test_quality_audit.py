"""Targeted tests for automated recommendation quality audit module and CLI."""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.eligibility.models import (
    CriterionDecision,
    CriterionStatus,
    EligibilityDecision,
    LivenessDecision,
    LivenessStatusContract,
    OverallEligibilityStatus,
)
from src.evaluation.quality_audit import (
    HISTORICAL_10_CAVEAT,
    QualityAuditDatabaseError,
    compute_confusion_matrix,
    evaluate_job_record,
    fetch_database_jobs,
    get_connection_for_audit,
    parse_frozen_ilanlar,
    parse_sample_dir,
    run_quality_audit,
)

ROOT = Path(__file__).resolve().parents[1]


def _make_dummy_gate_decision(
    *,
    exp: CriterionStatus = CriterionStatus.MET,
    lang: CriterionStatus = CriterionStatus.MET,
    pref: CriterionStatus = CriterionStatus.MET,
    live: LivenessStatusContract = LivenessStatusContract.OPEN,
    can_rec: bool = True,
) -> EligibilityDecision:
    now_str = "2026-10-08T12:00:00+00:00"
    return EligibilityDecision(
        job_id="101",
        overall_status=OverallEligibilityStatus.ELIGIBLE if can_rec else OverallEligibilityStatus.REVIEW,
        can_recommend=can_rec,
        can_generate_documents=can_rec,
        can_notify=can_rec,
        experience=CriterionDecision(status=exp, reason="Exp check", checked_at=now_str),
        language=CriterionDecision(status=lang, reason="Lang check", checked_at=now_str),
        posting_language_preference=CriterionDecision(status=pref, reason="Pref check", checked_at=now_str),
        liveness=LivenessDecision(status=live, reason="Live check", checked_at=now_str, http_code=200),
        hard_block_reasons=[] if can_rec else ["Block"],
        review_reasons=[],
        evaluated_at=now_str,
    )


class TestQualityAudit(unittest.TestCase):
    def test_candidate_suitability_requires_all_criteria_met(self) -> None:
        candidate_facts = {"profile_version": "test-v1"}
        job = {
            "id": "101",
            "keyword_score": 80.0,
            "semantic_score": 0.8,
            "llm_judge_result_json": json.dumps({"is_match": True, "match_score": 85.0}),
        }

        # Case 1: All MET -> candidate_suitable=True
        dec_all_met = _make_dummy_gate_decision(exp=CriterionStatus.MET, lang=CriterionStatus.MET, pref=CriterionStatus.MET)
        with patch("src.evaluation.quality_audit.evaluate_job_eligibility", return_value=dec_all_met):
            details, _, _ = evaluate_job_record(job, candidate_facts)
            self.assertTrue(details["candidate_suitable"])

        # Case 2: One criterion is UNKNOWN (e.g. 4599 case) -> must abstain/block
        dec_one_unknown = _make_dummy_gate_decision(
            exp=CriterionStatus.MET,
            lang=CriterionStatus.MET,
            pref=CriterionStatus.UNKNOWN,
            can_rec=False,
        )
        with patch("src.evaluation.quality_audit.evaluate_job_eligibility", return_value=dec_one_unknown):
            details, _, _ = evaluate_job_record(job, candidate_facts)
            self.assertFalse(details["candidate_suitable"], "UNKNOWN criterion must not pass suitability")

        # Case 3: One criterion is UNMET -> candidate_suitable=False
        dec_unmet = _make_dummy_gate_decision(
            exp=CriterionStatus.UNMET,
            lang=CriterionStatus.MET,
            pref=CriterionStatus.MET,
            can_rec=False,
        )
        with patch("src.evaluation.quality_audit.evaluate_job_eligibility", return_value=dec_unmet):
            details, _, _ = evaluate_job_record(job, candidate_facts)
            self.assertFalse(details["candidate_suitable"])

    def test_malformed_judge_result_handling(self) -> None:
        candidate_facts = {"profile_version": "test-v1"}
        dec = _make_dummy_gate_decision()

        # Truthy string "true" is NOT strict bool -> invalid
        job_truthy = {"id": "102", "llm_judge_result_json": json.dumps({"is_match": "true", "match_score": 80})}
        with patch("src.evaluation.quality_audit.evaluate_job_eligibility", return_value=dec):
            details, _, is_valid = evaluate_job_record(job_truthy, candidate_facts)
            self.assertFalse(is_valid)
            self.assertFalse(details["is_match"])

        # Corrupted JSON string -> invalid, doesn't crash
        job_corrupt = {"id": "103", "llm_judge_result_json": "{not valid json"}
        with patch("src.evaluation.quality_audit.evaluate_job_eligibility", return_value=dec):
            details, _, is_valid = evaluate_job_record(job_corrupt, candidate_facts)
            self.assertFalse(is_valid)
            self.assertFalse(details["is_match"])

    def test_missing_database_table_raises_operational_error(self) -> None:
        con = sqlite3.connect(":memory:")
        # Database has no 'jobs' table
        with self.assertRaises(QualityAuditDatabaseError):
            fetch_database_jobs(con)
        con.close()

    def test_fetch_database_jobs_bounds_limit_and_includes_queues(self) -> None:
        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY, status TEXT, title TEXT, keyword_score REAL, semantic_score REAL, llm_judge_result_json TEXT)"
        )
        judge_dummy = '{"is_match": false, "match_score": 50}'
        # Judged rows in matching queues
        con.execute("INSERT INTO jobs VALUES (1, 'evaluated', 'J1', 50, 0.5, ?)", (judge_dummy,))
        con.execute("INSERT INTO jobs VALUES (2, 'low_priority', 'J2', 50, 0.5, ?)", (judge_dummy,))
        con.execute("INSERT INTO jobs VALUES (3, 'normal', 'J3', 50, 0.5, ?)", (judge_dummy,))
        con.execute("INSERT INTO jobs VALUES (4, 'ready_for_review', 'J4', 50, 0.5, NULL)")
        con.execute("INSERT INTO jobs VALUES (5, 'rejected', 'J5', 50, 0.5, ?)", (judge_dummy,))
        # Unjudged evaluated row: excluded by default query
        con.execute("INSERT INTO jobs VALUES (6, 'evaluated', 'J6_unjudged', 50, 0.5, NULL)")

        # Default query fetches 5 (judged rows + ready_for_review), excludes unjudged #6
        jobs = fetch_database_jobs(con, limit=10)
        self.assertEqual(len(jobs), 5)
        self.assertNotIn(6, [j["id"] for j in jobs])

        # Explicit ids query CAN select unjudged row #6
        explicit_jobs = fetch_database_jobs(con, ids=[6])
        self.assertEqual(len(explicit_jobs), 1)
        self.assertEqual(explicit_jobs[0]["id"], 6)

        # Bounds limit within 1..500
        jobs_limit_1 = fetch_database_jobs(con, limit=1)
        self.assertEqual(len(jobs_limit_1), 1)

        con.close()



    def test_sqlite_readonly_mode_ro_enforces_no_writes(self) -> None:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            db_path = Path(tf.name)

        try:
            init_con = sqlite3.connect(db_path)
            init_con.execute("CREATE TABLE jobs (id INT PRIMARY KEY, status TEXT, title TEXT)")
            init_con.execute("INSERT INTO jobs VALUES (1, 'evaluated', 'Test Job')")
            init_con.commit()
            init_con.close()

            # Open via audit helper
            ro_con, provider = get_connection_for_audit(db_path=db_path)
            self.assertEqual(provider, "SQLite")

            # Writes must raise OperationalError (attempt to write a readonly database)
            with self.assertRaises(sqlite3.OperationalError):
                ro_con.execute("INSERT INTO jobs VALUES (2, 'evaluated', 'Mutate')")

            ro_con.close()
        finally:
            if db_path.exists():
                db_path.unlink()

    def test_probe_live_does_not_persist_db(self) -> None:
        candidate_facts = {"profile_version": "test-v1"}
        job = {"id": "104", "title": "Probe Test", "url": "https://example.com/job/104"}
        dummy_dec = _make_dummy_gate_decision()

        with patch("src.evaluation.quality_audit.evaluate_job_eligibility") as mock_eval:
            mock_eval.return_value = dummy_dec
            evaluate_job_record(job, candidate_facts, probe_live=True)
            # Verify con=None was passed so gate never writes to DB
            _, kwargs = mock_eval.call_args
            self.assertIsNone(kwargs.get("con"))
            self.assertTrue(kwargs.get("probe_live_if_missing"))

    def test_auto_turso_select_only_no_credentials_leak(self) -> None:
        fake_cursor = MagicMock()
        fake_cursor.description = [("id", None), ("status", None), ("title", None)]
        fake_cursor.fetchall.return_value = []

        fake_client = MagicMock()
        fake_client.execute.return_value = SimpleNamespace(
            columns=["id", "status", "title"],
            rows=[],
            rows_affected=0,
        )

        with (
            patch("src.db.TURSO_URL", "libsql://test.turso.io"),
            patch("src.db.TURSO_TOKEN", "super-secret-token-123"),
            patch("src.db.libsql_client.create_client_sync", return_value=fake_client),
        ):
            con, provider = get_connection_for_audit(provider="auto")
            self.assertEqual(provider, "Turso")

            # Verify no token in string representation of provider
            self.assertNotIn("super-secret-token-123", provider)

            # Table inspection uses SELECT only
            con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'")
            calls = fake_client.execute.call_args_list
            for call in calls:
                sql = call[0][0]
                self.assertTrue(sql.strip().upper().startswith("SELECT"), f"Expected SELECT only, got: {sql}")

            con.close()

    def test_parse_frozen_ilanlar_unicode_headings(self) -> None:
        sample_text = (
            "## 101 — Em Dash Job\n\nAcme Corp | Berlin\n\nhttps://example.com/101\n\nJob description text\n\n"
            "## 102 – En Dash Job\n\nBeta Inc | Paris\n\nhttps://example.com/102\n\nSecond description\n\n"
            "## 103 - Hyphen Job\n\nGamma LLC | London\n\nhttps://example.com/103\n\nThird description\n"
        )
        parsed = parse_frozen_ilanlar(sample_text)
        self.assertEqual(len(parsed), 3)
        self.assertEqual(parsed[101]["title"], "Em Dash Job")
        self.assertEqual(parsed[101]["company"], "Acme Corp")
        self.assertEqual(parsed[102]["title"], "En Dash Job")
        self.assertEqual(parsed[103]["title"], "Hyphen Job")

    def test_missing_ids_reported_and_actionable_metric_omitted_from_confusion_matrices(self) -> None:
        import tempfile

        # Confusion matrix helper handles empty pairs gracefully
        cm_empty = compute_confusion_matrix([])
        self.assertIsNone(cm_empty["precision"])
        self.assertIsNone(cm_empty["recall"])

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            db_path = Path(tf.name)

        try:
            init_con = sqlite3.connect(db_path)
            init_con.execute(
                "CREATE TABLE jobs (id INT PRIMARY KEY, status TEXT, title TEXT, keyword_score REAL, semantic_score REAL, llm_judge_result_json TEXT)"
            )
            init_con.commit()
            init_con.close()

            # A self-contained incomplete sample must report missing joins;
            # never depend on ignored private files existing on this machine.
            with tempfile.TemporaryDirectory() as sample_tmp:
                sample_path = Path(sample_tmp)
                (sample_path / "ilanlar.md").write_text(
                    "## 1 — Analyst\n\nAcme | Remote\n\nhttps://example.com/1\n\n"
                    "The role uses Python and requires no prior experience.\n",
                    encoding="utf-8",
                )
                (sample_path / "predictions.jsonl").write_text(
                    json.dumps({"job_id": 1, "llm_judge_result_json": {
                        "is_match": True, "match_score": 85,
                    }}) + "\n", encoding="utf-8",
                )
                (sample_path / "labels.csv").write_text(
                    "job_id,is_match,reason\n1,true,qualified\n2,false,unsuitable\n",
                    encoding="utf-8",
                )
                report = run_quality_audit(
                    db_path=db_path, provider="sqlite",
                    sample_dir=sample_path, limit=1,
                )
            replay = report.get("historical_replay", {})
            self.assertTrue(replay["available"])
            self.assertEqual(replay["labeled_jobs_count"], 1)
            self.assertEqual(replay["missing_ids"]["missing_predictions"], [2])
            self.assertEqual(replay["missing_ids"]["missing_descriptions"], [2])
            self.assertIn("actionable_recommendations", replay)
            matrices = replay["confusion_matrices"]
            self.assertNotIn("actionable_recommendations_vs_human", matrices)
            self.assertEqual(matrices["historical_old_predictions_vs_human"]["tp"], 1)
        finally:
            if db_path.exists():
                db_path.unlink()


    def test_no_tautology_leakage_caveat_present(self) -> None:
        self.assertIn("small convenience sample", HISTORICAL_10_CAVEAT)
        self.assertIn("not constitute an independent blind holdout", HISTORICAL_10_CAVEAT)


if __name__ == "__main__":
    unittest.main()
