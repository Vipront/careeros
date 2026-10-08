"""Integration tests for the shared recommendation quality gate across all workflows."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.db import ensure_v2_schema
from src.eligibility.gate import (
    ensure_eligibility_schema,
    evaluate_job_eligibility,
    persist_eligibility_decision,
)
from src.eligibility.models import OverallEligibilityStatus


class TestSharedRecommendationGate(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(self.tmp.name)
        self.tmp.close()

        self.con = sqlite3.connect(self.db_path)
        self.con.execute("""
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY,
                title TEXT,
                company TEXT,
                location TEXT,
                url TEXT,
                description TEXT,
                requirements_text TEXT,
                experience_requirements TEXT,
                education_requirements TEXT,
                eligibility_text TEXT,
                status TEXT,
                final_score REAL,
                llm_score REAL,
                llm_judge_status TEXT,
                llm_judge_result_json TEXT,
                liveness_status TEXT,
                liveness_checked_at TEXT,
                liveness_http_code INTEGER,
                liveness_detail TEXT,
                knockout_reason TEXT,
                review_priority TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        ensure_v2_schema(self.con)
        ensure_eligibility_schema(self.con)

        self.mock_candidate = {
            "is_graduated": False,
            "completed_degrees": [],
            "enrolled_programs": ["B.Sc. Molecular Biology and Genetics"],
            "research_experience_years": 0.25,
            "industry_experience_years": 0.0,
            "total_work_experience_years": 0.25,
            "languages": {
                "Turkish": "Native",
                "English": "Professional working proficiency, B2",
            },
        }

    def tearDown(self) -> None:
        self.con.close()
        self.db_path.unlink(missing_ok=True)

    def test_job_4291_high_llm_score_cannot_bypass_hard_experience_barrier(self) -> None:
        from datetime import datetime, timezone
        now_ts = datetime.now(timezone.utc).isoformat()
        job = {
            "id": 4291,
            "title": "Associate Research Technician",
            "description": "We expect our associate research technician: To have an MLO degree level 4 with 0-2 years working experience (preferably in industry)",
            "final_score": 86.0,
            "llm_score": 86.0,
            "liveness_status": "ACTIVE",
            "liveness_checked_at": now_ts,
        }
        decision = evaluate_job_eligibility(job, candidate=self.mock_candidate, ttl_hours=24.0)
        self.assertEqual(decision.overall_status, OverallEligibilityStatus.INELIGIBLE)
        self.assertFalse(decision.can_recommend)
        self.assertFalse(decision.can_generate_documents)
        self.assertFalse(decision.can_notify)
        self.assertTrue(any("MLO degree" in r for r in decision.hard_block_reasons))

    def test_eligibility_text_participates_in_hard_education_gate(self) -> None:
        job = {
            "id": 777,
            "title": "Molecular Biology Research Associate",
            "description": "Research role using molecular biology methods and data analysis.",
            "eligibility_text": "A PhD is mandatory for this position.",
        }

        decision = evaluate_job_eligibility(job, candidate=self.mock_candidate)

        self.assertEqual(decision.overall_status, OverallEligibilityStatus.INELIGIBLE)
        self.assertFalse(decision.can_generate_documents)
        self.assertTrue(any("PhD mandatory" in reason for reason in decision.hard_block_reasons))

    def test_job_4515_french_language_is_ineligible(self) -> None:
        from datetime import datetime, timezone
        now_ts = datetime.now(timezone.utc).isoformat()
        job = {
            "id": 4515,
            "title": "CDD Technicien supérieur de biologie moléculaire H/F",
            "description": "Vous êtes une personne passionnée par la biologie moléculaire... Vous êtes titulaire d'un Bac +2.",
            "final_score": 78.0,
            "liveness_status": "ACTIVE",
            "liveness_checked_at": now_ts,
        }
        decision = evaluate_job_eligibility(job, candidate=self.mock_candidate, ttl_hours=24.0)
        self.assertEqual(decision.overall_status, OverallEligibilityStatus.INELIGIBLE)
        self.assertFalse(decision.can_recommend)

    def test_job_4599_closed_liveness_is_ineligible(self) -> None:
        from datetime import datetime, timezone
        now_ts = datetime.now(timezone.utc).isoformat()
        job = {
            "id": 4599,
            "title": "Ingénieur bioinformatique",
            "description": "Assurer la curation scientifique...",
            "liveness_status": "CLOSED",
            "liveness_http_code": 410,
            "liveness_checked_at": now_ts,
        }
        decision = evaluate_job_eligibility(job, candidate=self.mock_candidate, ttl_hours=24.0)
        self.assertEqual(decision.overall_status, OverallEligibilityStatus.INELIGIBLE)
        self.assertTrue(any("closed" in r.lower() for r in decision.hard_block_reasons))

    def test_job_4624_student_computational_biology_is_eligible(self) -> None:
        from datetime import datetime, timezone
        from src.ops.liveness import LIVENESS_CLASSIFIER_VERSION
        now_ts = datetime.now(timezone.utc).isoformat()
        job = {
            "id": 4624,
            "title": "Computational Biology internship",
            "description": (
                "AIXIAL GROUP | Sèvres, France. Student in computational biology, bioinformatics, "
                "or a related field. Experience in programming using Python, R. Excellent communication and presentation skills."
            ),
            "final_score": 85.0,
            "liveness_status": "ACTIVE",
            "liveness_http_code": 200,
            "liveness_detail": f"{LIVENESS_CLASSIFIER_VERSION} - Page accessible and application open",
            "liveness_checked_at": now_ts,
        }
        decision = evaluate_job_eligibility(job, candidate=self.mock_candidate, ttl_hours=24.0)
        self.assertEqual(decision.overall_status, OverallEligibilityStatus.ELIGIBLE)
        self.assertTrue(decision.can_recommend)
        self.assertTrue(decision.can_generate_documents)
        self.assertTrue(decision.can_notify)

    def test_stale_liveness_does_not_block_otherwise_eligible_job(self) -> None:
        from datetime import datetime, timezone, timedelta
        from src.ops.liveness import LIVENESS_CLASSIFIER_VERSION
        stale_ts = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        job = {
            "id": 4624,
            "title": "Computational Biology internship",
            "description": "Student in computational biology, bioinformatics, or a related field. Python and R experience.",
            "final_score": 85.0,
            "liveness_status": "ACTIVE",
            "liveness_http_code": 200,
            "liveness_detail": f"{LIVENESS_CLASSIFIER_VERSION} - Page accessible and application open",
            "liveness_checked_at": stale_ts,
        }
        decision = evaluate_job_eligibility(job, candidate=self.mock_candidate, ttl_hours=24.0)
        self.assertEqual(decision.overall_status, OverallEligibilityStatus.ELIGIBLE)
        self.assertTrue(decision.can_recommend)
        self.assertFalse(any("Liveness" in r for r in decision.review_reasons))

    def test_old_active_without_positive_evidence_does_not_require_review(self) -> None:
        from datetime import datetime, timezone
        now_ts = datetime.now(timezone.utc).isoformat()
        job = {
            "id": 4624,
            "title": "Computational Biology internship",
            "description": "Student in computational biology, bioinformatics, or a related field. Python and R experience.",
            "final_score": 85.0,
            "liveness_status": "ACTIVE",
            "liveness_checked_at": now_ts,
            # Missing liveness_detail / liveness_http_code from new classifier
        }
        decision = evaluate_job_eligibility(job, candidate=self.mock_candidate, ttl_hours=24.0)
        self.assertEqual(decision.overall_status, OverallEligibilityStatus.ELIGIBLE)
        self.assertTrue(decision.can_recommend)

    def test_old_active_without_new_classifier_marker_is_informational(self) -> None:
        """Missing source proof does not block an otherwise eligible job."""
        from datetime import datetime, timezone
        now_ts = datetime.now(timezone.utc).isoformat()
        job = {
            "id": 4624,
            "title": "Computational Biology internship",
            "description": "Student in computational biology, bioinformatics, or a related field. Python and R experience.",
            "final_score": 85.0,
            "liveness_status": "ACTIVE",
            "liveness_http_code": 200,
            "liveness_detail": "Page accessible and application open",  # Legacy format WITHOUT liveness-v2 marker
            "liveness_checked_at": now_ts,
        }
        decision = evaluate_job_eligibility(job, candidate=self.mock_candidate, ttl_hours=24.0, probe_live_if_missing=False)
        self.assertEqual(decision.overall_status, OverallEligibilityStatus.ELIGIBLE)
        self.assertTrue(decision.can_recommend)
        self.assertTrue(decision.can_notify)
        self.assertFalse(any("Liveness" in r for r in decision.review_reasons))

    def test_persist_decision_and_schema_integration(self) -> None:
        from datetime import datetime, timezone
        from src.ops.liveness import LIVENESS_CLASSIFIER_VERSION
        now_ts = datetime.now(timezone.utc).isoformat()
        job = {
            "id": 100,
            "title": "Internship in Bioinformatics",
            "description": "Student opportunity in Python and R for cancer genomics.",
            "liveness_status": "ACTIVE",
            "liveness_http_code": 200,
            "liveness_detail": f"{LIVENESS_CLASSIFIER_VERSION} - Page accessible and application open",
            "liveness_checked_at": now_ts,
        }
        self.con.execute("INSERT INTO jobs (id, title, status) VALUES (100, 'Internship in Bioinformatics', 'evaluated')")
        self.con.commit()

        decision = evaluate_job_eligibility(job, candidate=self.mock_candidate)
        persist_eligibility_decision(self.con, 100, decision)

        row = self.con.execute("SELECT eligibility_status, eligibility_decision_json, eligibility_rule_version FROM jobs WHERE id=100").fetchone()
        self.assertEqual(row[0], "eligible")
        self.assertIn("can_recommend", row[1])
        self.assertEqual(row[2], "1.2.0")


if __name__ == "__main__":
    unittest.main()
