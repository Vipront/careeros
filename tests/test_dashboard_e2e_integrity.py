import unittest
from pathlib import Path

from src import db
from tests.dashboard_helpers import load_dashboard_helpers
from tests.db_helpers import isolated_database, seed_event, seed_job


class TestDashboardE2EIntegrity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dashboard = load_dashboard_helpers()

    def setUp(self):
        self.database = isolated_database()
        self.db_path = self.database.__enter__()
        self.addCleanup(self.database.__exit__, None, None, None)
        self.job_a_id = seed_job(
            self.db_path,
            fingerprint="synthetic-alpha",
            title="Senior Computational Biologist",
            company="AlphaBio Tech",
            status="ready_for_review",
            requirements="About AlphaBio: PhD in Bioinformatics, NGS pipeline development, Python & R, Linux HPC.",
            description="Develop omics pipelines.",
        )
        self.job_b_id = seed_job(
            self.db_path,
            fingerprint="synthetic-beta",
            title="Quality Control Bioassay Specialist",
            company="BetaPharma Corp",
            status="rejected",
            requirements="About BetaPharma: BSc in Molecular Biology, cell culture, qPCR, GMP documentation, Bioassay.",
            description="Execute QC assays in wet lab.",
        )
        seed_event(self.db_path, self.job_a_id, "alpha only")
        seed_event(self.db_path, self.job_b_id, "beta only")

    def test_selecting_different_jobs_is_fully_isolated(self):
        parse = self.dashboard["parse_job_sections"]
        skills = self.dashboard["get_job_skills_tags"]
        evidence = self.dashboard["get_job_evidence_checklist"]
        job_a = {
            "id": self.job_a_id,
            "title": "Senior Computational Biologist",
            "company": "AlphaBio Tech",
            "location": "Berlin, Germany",
            "requirements_text": "About AlphaBio: PhD in Bioinformatics, NGS pipeline development, Python & R, Linux HPC.",
            "description": "Develop omics pipelines.",
        }
        job_b = {
            "id": self.job_b_id,
            "title": "Quality Control Bioassay Specialist",
            "company": "BetaPharma Corp",
            "location": "Paris, France",
            "requirements_text": "About BetaPharma: BSc in Molecular Biology, cell culture, qPCR, GMP documentation, Bioassay.",
            "description": "Execute QC assays in wet lab.",
        }

        overview_a, _ = parse(job_a["id"], job_a["title"], job_a["company"], job_a["requirements_text"], job_a["description"])
        overview_b, _ = parse(job_b["id"], job_b["title"], job_b["company"], job_b["requirements_text"], job_b["description"])
        self.assertIn("AlphaBio", overview_a)
        self.assertNotIn("BetaPharma", overview_a)
        self.assertIn("BetaPharma", overview_b)
        self.assertNotIn("AlphaBio", overview_b)

        tags_a = skills(job_a["title"], job_a["company"], job_a["location"], job_a["requirements_text"], job_a["description"])
        tags_b = skills(job_b["title"], job_b["company"], job_b["location"], job_b["requirements_text"], job_b["description"])
        self.assertIn("Bioinformatics", tags_a)
        self.assertIn("Linux", tags_a)
        self.assertNotIn("Cell Culture", tags_a)
        self.assertIn("Bioassay", tags_b)
        self.assertIn("Cell Culture", tags_b)
        self.assertNotIn("Bioinformatics", tags_b)

        titles_a = [item["title"] for item in evidence(job_a)]
        titles_b = [item["title"] for item in evidence(job_b)]
        self.assertTrue(any("Biyoinformatik" in title for title in titles_a))
        self.assertTrue(any("Linux" in title for title in titles_a))
        self.assertTrue(any("Islak-Lab" in title or "QC" in title for title in titles_b))

    def test_filter_changes_selected_job_safely(self):
        active = self.dashboard["load_data"](include_rejected=False)
        self.assertEqual(active["id"].tolist(), [self.job_a_id])
        self.assertEqual(active.iloc[0]["status"], "ready_for_review")

        all_jobs = self.dashboard["load_data"](include_rejected=True)
        rejected = all_jobs[all_jobs["status"] == "rejected"]
        self.assertEqual(rejected["id"].tolist(), [self.job_b_id])

    def test_empty_filter_clears_detail_state(self):
        data = self.dashboard["load_data"](include_rejected=False)
        subset = data[data["title"].astype(str).str.contains("NO_SUCH_SYNTHETIC_JOB", na=False)]
        self.assertTrue(subset.empty)

    def test_search_matches_seeded_company(self):
        data = self.dashboard["load_data"](include_rejected=False)
        company = "AlphaBio Tech"
        filtered = data[data["company"].astype(str).str.lower().str.contains(company.lower(), na=False)]
        self.assertEqual(filtered["id"].tolist(), [self.job_a_id])

    def test_status_action_persists_to_isolated_db(self):
        con = db.get_connection()
        try:
            before = con.execute("SELECT status FROM jobs WHERE id = ?", (self.job_a_id,)).fetchone()
        finally:
            con.close()
        self.assertEqual(before[0], "ready_for_review")

        db.transition(self.job_a_id, "applied", note="isolated E2E test")

        con = db.get_connection()
        try:
            after = con.execute("SELECT status FROM jobs WHERE id = ?", (self.job_a_id,)).fetchone()
            audit = con.execute(
                "SELECT event_type, note FROM events WHERE job_id = ? ORDER BY id DESC LIMIT 1",
                (self.job_a_id,),
            ).fetchone()
            unrelated_job = con.execute(
                "SELECT status FROM jobs WHERE id = ?", (self.job_b_id,)
            ).fetchone()
            unrelated_audit = con.execute(
                "SELECT event_type, note FROM events WHERE job_id = ? ORDER BY id DESC LIMIT 1",
                (self.job_b_id,),
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(after[0], "applied")
        self.assertEqual(audit[0], "status_change")
        self.assertIn("ready_for_review -> applied", audit[1])
        self.assertEqual(unrelated_job[0], "rejected")
        self.assertEqual(tuple(unrelated_audit), ("test_event", "beta only"))

    def test_history_is_job_specific(self):
        timeline = self.dashboard["get_job_timeline_events"]
        alpha = timeline(self.job_a_id)
        beta = timeline(self.job_b_id)
        self.assertEqual(len(alpha), 1)
        self.assertEqual(len(beta), 1)
        self.assertEqual(alpha[0][2], "alpha only")
        self.assertEqual(beta[0][2], "beta only")

    def test_documents_are_job_specific_without_reading_workspace_output(self):
        temp_root = Path(self.db_path).parent / "assets-root"
        folder = temp_root / "output" / "run" / f"application_{self.job_a_id}"
        folder.mkdir(parents=True)
        assets = load_dashboard_helpers(root=temp_root)["get_job_assets_index"]()
        self.assertEqual(set(assets), {self.job_a_id})
        self.assertEqual(assets[self.job_a_id], folder)

    def test_no_static_job_mock_data_in_render_helpers(self):
        overview, _ = self.dashboard["parse_job_sections"](999, "Senior Scientist", "AcmeGenomics", "", "")
        self.assertIn("AcmeGenomics", overview)
        self.assertNotIn("Biomemory", overview)
        self.assertNotIn("Regeneron", overview)


if __name__ == "__main__":
    unittest.main()
