# -*- coding: utf-8 -*-
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from src.ingestion.manual_ingestion import process_manual_job

ROOT = Path(__file__).resolve().parents[1]

class TestManualPipelineFlow(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "manual_pipeline.db"
        con = sqlite3.connect(str(self.db_path))
        con.executescript((ROOT / "data" / "schema.sql").read_text(encoding="utf-8"))
        con.execute("ALTER TABLE jobs ADD COLUMN llm_model TEXT")
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _get_con(self):
        return sqlite3.connect(str(self.db_path))

    def _install_connection_factory(self, stack):
        import src.db
        import src.final_ranking
        import src.llm_judge
        import src.matcher.semantic

        for module, attribute in (
            (src.db, "get_connection"),
            (src.final_ranking, "get_connection"),
            (src.llm_judge, "get_connection"),
            (src.matcher.semantic, "get_connection"),
        ):
            stack.enter_context(patch.object(module, attribute, side_effect=self._get_con))

    def test_manual_job_reaches_semantic_judge_and_final_ranking(self):
        import src.final_ranking
        import src.ingestion.manual_ingestion as manual_ingestion
        import src.llm_judge
        import src.matcher.semantic

        class ToyModel:
            def encode(self, texts, **kwargs):
                import numpy as np
                return np.ones((len(texts), 3), dtype=np.float32)

        llm_result = {
            "is_match": True,
            "profile_type": "Bioinformatics",
            "match_score": 88,
            "missing_critical_skills": [],
            "relocation_supported": False,
            "confidence": 0.9,
            "reasoning": "Strong match for the target computational biology profile.",
        }

        with ExitStack() as stack:
            self._install_connection_factory(stack)
            stack.enter_context(patch.object(manual_ingestion, "verified_skill_matches", return_value=["Python"]))
            stack.enter_context(patch.object(manual_ingestion, "choose_profile", return_value=("Bioinformatics", {})))
            stack.enter_context(patch.object(manual_ingestion, "relevance_score", return_value=30.0))
            stack.enter_context(patch.object(src.matcher.semantic, "load_model", return_value=ToyModel()))
            stack.enter_context(patch.object(src.llm_judge, "call_claude", return_value=(
                llm_result, "offline-test-model", {"input_tokens": 0, "output_tokens": 0}
            )))
            stack.enter_context(patch.object(src.llm_judge.time, "sleep", return_value=None))
            stack.enter_context(patch.object(src.llm_judge, "trace_llm_judge", return_value=None))

            con = self._get_con()
            added = process_manual_job(
                url="https://jobs.example.org/manual-bioinformatics",
                title="Bioinformatics Research Associate",
                company="Research Institute",
                location="Berlin",
                description=(
                    "Bioinformatics researcher using Python and RNA-seq for computational genomics. "
                    "Analyze NGS datasets and gene expression."
                ),
                connection=con,
            )
            con.close()

            self.assertTrue(added.success)
            self.assertEqual(added.status, "evaluated")
            self.assertLess(added.final_score, 60.0)
            self.assertIn("pending semantic and LLM evaluation", added.reason)

            rejected_con = self._get_con()
            rejected = process_manual_job(
                title="Senior Director of Bioinformatics",
                company="Research Institute",
                location="Berlin",
                description="Lead and manage a large scientific team.",
                connection=rejected_con,
            )
            rejected_con.close()
            self.assertTrue(rejected.success)
            self.assertEqual(rejected.status, "rejected")

            src.matcher.semantic.run()
            semantic_con = self._get_con()
            sem_row = semantic_con.execute(
                "SELECT status, profile_type, semantic_score FROM jobs WHERE id=?", (added.job_id,)
            ).fetchone()
            rejected_semantic = semantic_con.execute(
                "SELECT semantic_score FROM jobs WHERE id=?", (rejected.job_id,)
            ).fetchone()[0]
            semantic_con.close()
            self.assertEqual(sem_row[0], "evaluated")
            self.assertEqual(sem_row[1], "Bioinformatics")
            self.assertIsNotNone(sem_row[2])
            self.assertIsNone(rejected_semantic)

            src.llm_judge.run(limit=10)
            judge_con = self._get_con()
            judge_row = judge_con.execute(
                "SELECT status, llm_judge_status, llm_score FROM jobs WHERE id=?", (added.job_id,)
            ).fetchone()
            judge_con.close()
            self.assertEqual(judge_row[0], "evaluated")
            self.assertEqual(judge_row[1], "success")
            self.assertEqual(judge_row[2], 88.0)

            src.final_ranking.run()
            final_con = self._get_con()
            final_row = final_con.execute(
                """SELECT status, semantic_score, llm_judge_status, llm_score, final_score
                   FROM jobs WHERE id=?""",
                (added.job_id,),
            ).fetchone()
            application_count = final_con.execute(
                "SELECT COUNT(*) FROM applications WHERE job_id=?", (added.job_id,)
            ).fetchone()[0]
            final_con.close()
            self.assertEqual(final_row[0], "evaluated")
            self.assertGreater(final_row[1], 0.0)
            self.assertEqual(final_row[2], "success")
            self.assertEqual(final_row[3], 88.0)
            self.assertEqual(final_row[4], 88.0)
            self.assertEqual(application_count, 0, "Document acceptance is still pending")


if __name__ == "__main__":
    unittest.main()
