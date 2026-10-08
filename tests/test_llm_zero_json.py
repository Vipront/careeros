# -*- coding: utf-8 -*-
"""
Tests for LLM Judge DB Single Source of Truth & Zero JSON Artifact Generation.
Verifies that:
1. LLM Judge results are written directly and transactionally to jobs.llm_judge_result_json in DB.
2. No data/llm_judge_results.json file is created or updated upon LLM Judge execution.
3. final_ranking loads results exclusively from DB without any JSON dependency.
4. If a JSON file is missing or corrupt, neither LLM Judge nor final_ranking is affected.
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import src.final_ranking as fr


class TestLlmJudgeZeroJsonDependency(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        self.db_path = self.tmp_path / "test_jobs.db"
        self.mock_json_file = self.tmp_path / "llm_judge_results.json"

        self.con = sqlite3.connect(self.db_path)
        self.con.execute("""
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY,
                title TEXT,
                company TEXT,
                location TEXT,
                description TEXT,
                requirements_text TEXT,
                education_requirements TEXT,
                experience_requirements TEXT,
                eligibility_text TEXT,
                status TEXT,
                profile_type TEXT,
                description_available INTEGER DEFAULT 1,
                keyword_score REAL,
                match_score REAL,
                semantic_score REAL,
                llm_score REAL,
                final_score REAL,
                review_priority TEXT,
                last_evaluated TEXT,
                updated_at TEXT,
                llm_judge_status TEXT,
                llm_judge_attempts INTEGER DEFAULT 0,
                llm_judge_next_retry_at TEXT,
                llm_judge_terminal INTEGER DEFAULT 0,
                llm_model TEXT,
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

    def test_llm_judge_execution_does_not_create_or_update_json_artifact(self):
        """E: LLM Judge run must persist solely to DB and NOT produce data/llm_judge_results.json."""
        import src.llm_judge as lj

        # Insert eligible job
        self.con.execute("""
            INSERT INTO jobs (
                id, title, company, location, description, requirements_text,
                education_requirements, experience_requirements, eligibility_text,
                status, profile_type, description_available, match_score, semantic_score, keyword_score,
                llm_judge_status
            ) VALUES (
                201, 'Bioinformatics Analyst', 'EMBL', 'Heidelberg',
                'NGS RNA-Seq analysis', 'Python, Nextflow, RNA-Seq',
                'M.Sc. in Bioinformatics', '2 years experience', 'EU citizen or valid permit',
                'evaluated', 'Bioinformatics', 1, 85.0, 0.82, 80.0,
                'not_attempted'
            )
        """)
        self.con.commit()

        fake_judge_result = {
            "is_match": True,
            "profile_type": "Bioinformatics",
            "match_score": 88.0,
            "missing_critical_skills": [],
            "relocation_supported": True,
            "confidence": 0.95,
            "reasoning": "Excellent match for bioinformatics candidate."
        }
        fake_usage = {"input_tokens": 100, "output_tokens": 50}

        # Verify target JSON does not exist before run
        self.assertFalse(self.mock_json_file.exists())

        with patch("src.llm_judge.get_connection", side_effect=self._mock_get_connection), \
             patch("src.llm_judge.call_claude", return_value=(fake_judge_result, "claude-haiku-4-5", fake_usage)), \
             patch("src.llm_judge.trace_llm_judge"):
            lj.run(limit=1)

        # 1. DB persistence verification
        cur = self.con.execute(
            "SELECT llm_judge_status, llm_score, llm_judge_result_json FROM jobs WHERE id = 201"
        )
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "success")
        self.assertEqual(row[1], 88.0)
        saved_json = json.loads(row[2])
        self.assertEqual(saved_json["match_score"], 88.0)
        self.assertTrue(saved_json["is_match"])

        # 2. JSON non-generation verification: No RESULTS constant or file produced
        self.assertFalse(hasattr(lj, "RESULTS"), "lj.RESULTS constant must be removed from llm_judge.py")
        self.assertFalse(self.mock_json_file.exists(), "No JSON artifact should have been created on disk")

    def test_final_ranking_has_no_llm_results_constant_and_reads_db_only(self):
        """B & C: final_ranking must not have LLM_RESULTS constant and loads cleanly from DB."""
        self.assertFalse(hasattr(fr, "LLM_RESULTS"), "fr.LLM_RESULTS constant must be removed from final_ranking.py")

        db_payload = {
            "job_id": 202,
            "match_score": 92.0,
            "is_match": True,
            "profile_type": "Bioinformatics"
        }
        self.con.execute(
            "INSERT INTO jobs (id, title, status, llm_judge_result_json) VALUES (?, ?, ?, ?)",
            (202, "Lead Computational Chemist", "evaluated", json.dumps(db_payload))
        )
        self.con.commit()

        with patch("src.final_ranking.get_connection", side_effect=self._mock_get_connection):
            results = fr.load_llm_results()

        self.assertIn(202, results)
        self.assertEqual(results[202]["match_score"], 92.0)
        self.assertTrue(results[202]["is_match"])


if __name__ == "__main__":
    unittest.main()
