"""Offline tests for exact evaluation-cache identity and additive storage."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.llm.client import canonical_json_bytes, make_evaluation_cache_key
from src import llm_judge
from src.llm_judge import ensure_llm_result_storage


class TestEvaluationCacheIdentity(unittest.TestCase):
    def key(self, **overrides: object) -> str:
        values = {
            "job_fields": {"id": 17, "title": "Synthetic role", "location": "Berlin"},
            "runtime_profile_bytes": b'{"synthetic_profile":true}',
            "prompt": "synthetic prompt",
            "schema_version": "judge-result-v1",
            "model": "synthetic-model-v1",
            "provider": "synthetic-provider",
        }
        values.update(overrides)
        return make_evaluation_cache_key(**values)

    def test_canonical_json_ignores_mapping_insertion_order(self) -> None:
        first = canonical_json_bytes({"b": 2, "a": {"y": 1, "x": 0}})
        second = canonical_json_bytes({"a": {"x": 0, "y": 1}, "b": 2})
        self.assertEqual(first, second)

    def test_every_evaluation_input_changes_the_identity(self) -> None:
        baseline = self.key()
        variants = [
            self.key(job_fields={"id": 17, "title": "Synthetic role", "location": "Paris"}),
            self.key(runtime_profile_bytes=b'{"synthetic_profile":false}'),
            self.key(prompt="changed synthetic prompt"),
            self.key(schema_version="judge-result-v2"),
            self.key(model="synthetic-model-v2"),
            self.key(provider="other-synthetic-provider"),
        ]
        self.assertTrue(all(value != baseline for value in variants))

    def test_storage_setup_adds_cache_without_replacing_existing_job_columns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "synthetic.db"
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT)")
                connection.commit()

                ensure_llm_result_storage(connection)
                ensure_llm_result_storage(connection)

                job_columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
                cache_columns = {row[1] for row in connection.execute("PRAGMA table_info(llm_evaluation_cache)")}
                self.assertTrue({"id", "title", "llm_judge_result_json", "llm_model"} <= job_columns)
                self.assertTrue({"cache_key", "result_json", "model", "usage_json", "created_at"} <= cache_columns)
            finally:
                connection.close()

    def test_identical_role_inputs_with_different_ids_reuse_one_saved_judge_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "synthetic-jobs.db"
            connection = sqlite3.connect(database)
            connection.execute(
                """
                CREATE TABLE jobs (
                    id INTEGER PRIMARY KEY, fingerprint TEXT, title TEXT, company TEXT,
                    location TEXT, url TEXT, source TEXT, date_found TEXT, created_at TEXT,
                    updated_at TEXT, description TEXT, requirements_text TEXT,
                    education_requirements TEXT, experience_requirements TEXT,
                    eligibility_text TEXT, profile_type TEXT, semantic_score REAL,
                    keyword_score REAL, match_score REAL, status TEXT,
                    description_available INTEGER, llm_score REAL, final_score REAL,
                    review_priority TEXT, llm_judge_status TEXT DEFAULT 'not_attempted',
                    llm_judge_attempts INTEGER DEFAULT 0, llm_judge_next_retry_at TEXT,
                    llm_judge_terminal INTEGER DEFAULT 0
                )
                """
            )
            job_values = (
                "Synthetic scientist", "Synthetic institute", "Berlin", "https://synthetic.invalid/role",
                "synthetic-source", "2026-10-01", "2026-10-01", "2026-10-01",
                "Synthetic NGS research description", "Synthetic Python and NGS requirements",
                "Synthetic degree requirement", "Synthetic experience requirement", "Synthetic eligibility",
                "Bioinformatics", 0.8, 80.0, 82.0, "evaluated", 1,
            )
            for identifier in (101, 202):
                connection.execute(
                    """INSERT INTO jobs (
                        id, fingerprint, title, company, location, url, source,
                        date_found, created_at, updated_at, description, requirements_text,
                        education_requirements, experience_requirements, eligibility_text,
                        profile_type, semantic_score, keyword_score, match_score, status,
                        description_available
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (identifier, f"synthetic-fingerprint-{identifier}", *job_values),
                )
            connection.commit()
            connection.close()

            result = {
                "is_match": True,
                "profile_type": "Bioinformatics",
                "match_score": 82,
                "missing_critical_skills": [],
                "relocation_supported": False,
                "confidence": 0.8,
                "reasoning": "Synthetic test result.",
            }

            with (
                patch.object(llm_judge, "get_connection", side_effect=lambda: sqlite3.connect(database)),
                patch.object(llm_judge, "load_master_profile", return_value={"synthetic": True}),
                patch.object(llm_judge, "call_claude", return_value=(result.copy(), "synthetic-model", {"input_tokens": 10, "output_tokens": 5})) as model_call,
                patch.object(llm_judge, "trace_llm_judge"),
                patch.object(llm_judge.time, "sleep"),
            ):
                llm_judge.run(limit=2)

            model_call.assert_called_once()
            verify = sqlite3.connect(database)
            try:
                saved = verify.execute(
                    "SELECT id, llm_judge_status, llm_judge_result_json FROM jobs ORDER BY id"
                ).fetchall()
                cache_count = verify.execute("SELECT COUNT(*) FROM llm_evaluation_cache").fetchone()[0]
            finally:
                verify.close()
            self.assertEqual([row[1] for row in saved], ["success", "success"])
            self.assertEqual([json.loads(row[2])["job_id"] for row in saved], [101, 202])
            self.assertEqual(cache_count, 1)


if __name__ == "__main__":
    unittest.main()
