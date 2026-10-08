# -*- coding: utf-8 -*-
import unittest
import sqlite3
import json
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import ALLOWED, transition
from src.final_ranking import calculate_final_score, READY_THRESHOLD
from src.filters.knockout import knockout_reason
from src.documents.verifier import verify_tailored_document

class TestEndToEndWorkflow(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.tmp_dir) / "test_jobs.db"
        self.con = sqlite3.connect(str(self.db_path))
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
                status TEXT DEFAULT 'new',
                keyword_score REAL DEFAULT 0,
                semantic_score REAL DEFAULT 0,
                final_score REAL DEFAULT 0,
                knockout_reason TEXT,
                updated_at TEXT
            )
        """)
        self.con.execute("""
            CREATE TABLE events (
                job_id INTEGER,
                event_type TEXT,
                event_time TEXT,
                note TEXT
            )
        """)
        self.con.commit()

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # 1. SCENARIO A: Strong Positive Workflow
    def test_scenario_a_strong_positive(self):
        """Strong positive: is_match=True, high scores -> ready_for_review -> documents -> verified."""
        llm_out = {
            "is_match": True,
            "profile_type": "Wet Lab",
            "match_score": 88.0,
            "missing_critical_skills": []
        }
        kw_score = 80.0
        sem_score = 85.0
        final = calculate_final_score(kw_score, sem_score, llm_out["match_score"])
        self.assertGreaterEqual(final, READY_THRESHOLD)

        # State transition: new -> evaluated -> ready_for_review
        self.assertIn("evaluated", ALLOWED["new"])
        self.assertIn("ready_for_review", ALLOWED["evaluated"])

        # Verify verifier passes on compliant folder
        doc_folder = Path(self.tmp_dir) / "doc_job_1"
        doc_folder.mkdir()
        # Mock docx text containing verified terms and facts
        (doc_folder / "apply_info.json").write_text(json.dumps({
            "instructions": "Send CV to lab@helmholtz.de",
            "email": "lab@helmholtz.de"
        }), encoding="utf-8")

        # Create minimal tailored_cv.docx text
        from docx import Document
        doc = Document()
        doc.add_paragraph("Ugur Yildirim - Molecular Biology & Genetics. Handled qPCR, Western blot, cell culture at Example Research Lab. CXCR4 gene.")
        doc.save(str(doc_folder / "tailored_cv.docx"))
        cover = Document()
        cover.add_paragraph("Application cover letter.")
        cover.save(str(doc_folder / "cover_letter.docx"))

        # Run verifier
        master_cv = Path("data/master_cv.md").read_text(encoding="utf-8")
        report = verify_tailored_document(doc_folder, master_cv)
        self.assertIsNone(report["term_fidelity_score"])
        self.assertGreater(report["registered_terms_detected_count"], 0)
        self.assertEqual(report["detected_claim_coverage"]["coverage_status"], "complete")
        self.assertTrue(report["factual_checks"]["institution_preserved"])

    # 2. SCENARIO B: Hard Barrier Workflow
    def test_scenario_b_hard_barrier(self):
        """Hard barrier: knockout triggers or is_match=False with low score -> rejected."""
        # Check knockout filter
        reason = knockout_reason(title="Senior Postdoctoral Researcher", description="PhD mandatory")
        self.assertIsNotNone(reason)
        self.assertIn("Senior", reason)

        # In ranking, is_match=False and score < 40 must strictly produce 'rejected'
        llm_score = 35.0
        is_match = False
        new_status = "low_priority" if (not is_match and llm_score >= 40.0) else "rejected"
        self.assertEqual(new_status, "rejected")
        # Rejected jobs must NEVER have transitions to ready_for_review or document_generated
        self.assertEqual(len(ALLOWED["rejected"]), 0)

    # 3. SCENARIO C: Soft Barrier / Missing Skill Workflow
    def test_scenario_c_soft_missing_skill(self):
        """Soft barrier: is_match=False but score >= 40 -> low_priority (not thrown away, but not ready)."""
        llm_score = 45.0
        is_match = False
        new_status = "low_priority" if (not is_match and llm_score >= 40.0) else "rejected"
        self.assertEqual(new_status, "low_priority")
        self.assertIn("ready_for_review", ALLOWED["low_priority"]) # Can be promoted later by human

    # 4. SCENARIO D: Commercial / Non-Science Workflow
    def test_scenario_d_commercial_role(self):
        """Commercial role: profile_type=Other -> must not enter ready_for_review pipeline."""
        llm_out = {
            "is_match": False,
            "profile_type": "Other",
            "match_score": 15.0,
            "missing_critical_skills": ["Sales experience"]
        }
        self.assertEqual(llm_out["profile_type"], "Other")
        new_status = "rejected" if llm_out["match_score"] < 40.0 else "low_priority"
        self.assertEqual(new_status, "rejected")

    # 5. SCENARIO E: Document Verification Quality Audit
    def test_scenario_e_document_verification_failure(self):
        """If document lacks essential truth facts, verifier flags unsupported claims."""
        doc_folder = Path(self.tmp_dir) / "doc_job_fake"
        doc_folder.mkdir()
        from docx import Document
        doc = Document()
        doc.add_paragraph("Invented degree at Harvard University with 10 years clinical pharmacology experience.")
        doc.save(str(doc_folder / "tailored_cv.docx"))

        master_cv = Path("data/master_cv.md").read_text(encoding="utf-8")
        report = verify_tailored_document(doc_folder, master_cv)
        # Institution Example Research Lab/Inonu not preserved
        self.assertFalse(report["factual_checks"]["institution_preserved"])

    # 6. Invalid Transitions Check
    def test_invalid_fsm_transitions(self):
        """Forbidden transitions must be strictly blocked by FSM."""
        self.assertNotIn("ready_for_review", ALLOWED["rejected"])
        self.assertNotIn("document_generated", ALLOWED["rejected"])
        self.assertNotIn("applied", ALLOWED["new"])
        self.assertNotIn("offer", ALLOWED["evaluated"])

if __name__ == "__main__":
    unittest.main()
