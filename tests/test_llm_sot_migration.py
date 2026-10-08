# -*- coding: utf-8 -*-
"""
Tests for DB Single Source of Truth Migration in final_ranking.py.
Follows TDD (RED phase) before removing JSON read path in final_ranking.py.
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import src.final_ranking as fr


class TestLlmJudgeResultsDBSourceOfTruth(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        self.db_path = self.tmp_path / "test_jobs.db"
        self.json_file = self.tmp_path / "llm_judge_results.json"

        self.con = sqlite3.connect(self.db_path)
        self.con.execute("""
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY,
                title TEXT,
                status TEXT,
                profile_type TEXT,
                keyword_score REAL,
                match_score REAL,
                semantic_score REAL,
                llm_score REAL,
                final_score REAL,
                review_priority TEXT,
                last_evaluated TEXT,
                updated_at TEXT,
                llm_judge_result_json TEXT
            )
        """)
        self.con.execute("""
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER,
                event_type TEXT,
                event_time TEXT,
                note TEXT
            )
        """)
        self.con.commit()

    def tearDown(self):
        self.con.close()
        self.tmp_dir.cleanup()

    def _mock_get_connection(self):
        return sqlite3.connect(self.db_path)

    def test_load_llm_results_reads_from_db_only(self):
        """A: When DB has LLM result, load_llm_results loads it correctly."""
        db_payload = {
            "job_id": 101,
            "match_score": 85,
            "is_match": True,
            "profile_type": "Bioinformatics"
        }
        self.con.execute(
            "INSERT INTO jobs (id, title, status, llm_judge_result_json) VALUES (?, ?, ?, ?)",
            (101, "Bioinformatician", "evaluated", json.dumps(db_payload))
        )
        self.con.commit()

        with patch("src.final_ranking.get_connection", side_effect=self._mock_get_connection):
            results = fr.load_llm_results()

        self.assertIn(101, results)
        self.assertEqual(results[101]["match_score"], 85)
        self.assertEqual(results[101]["is_match"], True)

    def test_db_takes_precedence_and_ignores_json_file_content(self):
        """B: Even if JSON file exists with different/stale scores, DB must be the single source of truth."""
        # DB has score 90
        db_payload = {"job_id": 102, "match_score": 90, "is_match": True}
        self.con.execute(
            "INSERT INTO jobs (id, title, status, llm_judge_result_json) VALUES (?, ?, ?, ?)",
            (102, "Senior Tech", "evaluated", json.dumps(db_payload))
        )
        self.con.commit()

        # Stale JSON has score 20
        json_payload = [{"job_id": 102, "match_score": 20, "is_match": False}]
        self.json_file.write_text(json.dumps(json_payload), encoding="utf-8")

        with patch("src.final_ranking.get_connection", side_effect=self._mock_get_connection):
            results = fr.load_llm_results()

        self.assertEqual(results[102]["match_score"], 90)
        self.assertTrue(results[102]["is_match"])

    def test_ranking_succeeds_when_json_is_missing(self):
        """C: Pipeline ranking must function flawlessly when llm_judge_results.json does not exist."""
        db_payload = {"job_id": 103, "match_score": 75, "is_match": True}
        self.con.execute(
            "INSERT INTO jobs (id, title, status, llm_judge_result_json) VALUES (?, ?, ?, ?)",
            (103, "Scientist", "evaluated", json.dumps(db_payload))
        )
        self.con.commit()

        with patch("src.final_ranking.get_connection", side_effect=self._mock_get_connection):
            results = fr.load_llm_results()

        self.assertIn(103, results)
        self.assertEqual(results[103]["match_score"], 75)

    def test_ranking_succeeds_when_json_is_malformed_corrupt(self):
        """D: Malformed / corrupt JSON must never crash the ranking pipeline."""
        db_payload = {"job_id": 104, "match_score": 80, "is_match": True}
        self.con.execute(
            "INSERT INTO jobs (id, title, status, llm_judge_result_json) VALUES (?, ?, ?, ?)",
            (104, "Researcher", "evaluated", json.dumps(db_payload))
        )
        self.con.commit()

        # Corrupt JSON file
        self.json_file.write_text("{{{ corrupt non-json content }}}", encoding="utf-8")

        with patch("src.final_ranking.get_connection", side_effect=self._mock_get_connection):
            results = fr.load_llm_results()

        self.assertIn(104, results)
        self.assertEqual(results[104]["match_score"], 80)

    def test_empty_db_and_mixed_records_handled_gracefully(self):
        """E: Jobs without LLM results are handled cleanly without error."""
        self.con.execute(
            "INSERT INTO jobs (id, title, status, keyword_score, semantic_score, llm_score, llm_judge_result_json) "
            "VALUES (105, 'Wet Lab Lead', 'evaluated', 50.0, 0.5, NULL, NULL)"
        )
        self.con.commit()

        with patch("src.final_ranking.get_connection", side_effect=self._mock_get_connection):
            results = fr.load_llm_results()

        self.assertNotIn(105, results)

    def test_run_e2e_scenario_a_db_result_present(self):
        """A: When DB has LLM Judge result, fr.run() applies it without reading any JSON."""
        payload = {"job_id": 301, "match_score": 88.0, "is_match": True}
        self.con.execute("""
            INSERT INTO jobs (id, title, status, keyword_score, semantic_score, llm_judge_result_json)
            VALUES (301, 'Bioinformatics Engineer', 'evaluated', 70.0, 0.75, ?)
        """, (json.dumps(payload),))
        self.con.commit()

        with patch("src.final_ranking.get_connection", side_effect=self._mock_get_connection):
            fr.run()

        row = self.con.execute("SELECT status, final_score, llm_score, review_priority FROM jobs WHERE id=301").fetchone()
        self.assertEqual(row[0], "evaluated")  # Document acceptance is still pending.
        self.assertEqual(row[1], 88.0)
        self.assertEqual(row[2], 88.0)
        self.assertEqual(row[3], "high")

    def test_run_e2e_scenario_b_both_db_and_json_present_db_canonical(self):
        """B: When both DB and JSON exist, DB is canonical and JSON never overrides DB."""
        db_payload = {"job_id": 302, "match_score": 92.0, "is_match": True}
        self.con.execute("""
            INSERT INTO jobs (id, title, status, keyword_score, semantic_score, llm_judge_result_json)
            VALUES (302, 'Genomics Lead', 'evaluated', 60.0, 0.65, ?)
        """, (json.dumps(db_payload),))
        self.con.commit()

        # Conflicting stale JSON
        stale_json = [{"job_id": 302, "match_score": 25.0, "is_match": False}]
        self.json_file.write_text(json.dumps(stale_json), encoding="utf-8")

        with patch("src.final_ranking.get_connection", side_effect=self._mock_get_connection):
            fr.run()

        row = self.con.execute("SELECT status, final_score, llm_score FROM jobs WHERE id=302").fetchone()
        self.assertEqual(row[0], "evaluated")  # Document acceptance is still pending.
        self.assertEqual(row[1], 92.0)
        self.assertEqual(row[2], 92.0)

    def test_run_e2e_scenario_c_json_missing(self):
        """C: Workflow executes normally when JSON file is absent on disk."""
        if self.json_file.exists():
            self.json_file.unlink()

        db_payload = {"job_id": 303, "match_score": 80.0, "is_match": True}
        self.con.execute("""
            INSERT INTO jobs (id, title, status, keyword_score, semantic_score, llm_judge_result_json)
            VALUES (303, 'Senior Analyst', 'evaluated', 65.0, 0.70, ?)
        """, (json.dumps(db_payload),))
        self.con.commit()

        with patch("src.final_ranking.get_connection", side_effect=self._mock_get_connection):
            fr.run()

        row = self.con.execute("SELECT status, final_score FROM jobs WHERE id=303").fetchone()
        self.assertEqual(row[0], "evaluated")  # Document acceptance is still pending.
        self.assertEqual(row[1], 80.0)

    def test_run_e2e_scenario_d_json_corrupt(self):
        """D: Corrupt JSON on disk does not disrupt runtime DB reading or pipeline execution."""
        self.json_file.write_text("{CORRUPTED_JSON_DATA!!!", encoding="utf-8")

        db_payload = {"job_id": 304, "match_score": 77.0, "is_match": True}
        self.con.execute("""
            INSERT INTO jobs (id, title, status, keyword_score, semantic_score, llm_judge_result_json)
            VALUES (304, 'Research Specialist', 'evaluated', 70.0, 0.70, ?)
        """, (json.dumps(db_payload),))
        self.con.commit()

        with patch("src.final_ranking.get_connection", side_effect=self._mock_get_connection):
            fr.run()

        row = self.con.execute("SELECT status, final_score FROM jobs WHERE id=304").fetchone()
        self.assertEqual(row[0], "evaluated")  # Document acceptance is still pending.
        self.assertEqual(row[1], 77.0)

    def test_run_e2e_scenario_e_no_llm_result_fallback_preserved(self):
        """E: When job has no LLM Judge result in DB, fallback scoring is applied correctly."""
        self.con.execute("""
            INSERT INTO jobs (id, title, status, keyword_score, semantic_score, llm_score, llm_judge_result_json)
            VALUES (305, 'Wet Lab Intern', 'evaluated', 70.0, 0.80, NULL, NULL)
        """)
        self.con.commit()

        with patch("src.final_ranking.get_connection", side_effect=self._mock_get_connection):
            fr.run()

        # Fallback calculation: 0.40 * 70.0 + 0.60 * (0.80 * 100) = 28.0 + 48.0 = 76.0
        row = self.con.execute("SELECT status, final_score, review_priority FROM jobs WHERE id=305").fetchone()
        self.assertEqual(row[0], "evaluated")  # LLM and document acceptance are still pending.
        self.assertEqual(row[1], 76.0)
        self.assertEqual(row[2], "high")


if __name__ == "__main__":
    unittest.main()
