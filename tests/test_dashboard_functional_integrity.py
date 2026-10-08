import unittest
from tests.dashboard_helpers import load_dashboard_helpers


class TestDashboardFunctionalIntegrity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dashboard = load_dashboard_helpers()

    def test_parse_job_sections_isolation(self):
        parse = self.dashboard["parse_job_sections"]
        ov_biomemory, b_biomemory = parse(
            1795,
            "R&D-Biotech engineer",
            "Biomemory",
            "About Biomemory: DNA digital data storage. Requirements:\n- MSc in Bioinformatics\n- NGS libraries",
            "",
        )
        ov_regeneron, b_regeneron = parse(
            1790,
            "Temp QC Analyst",
            "Regeneron",
            "At Regeneron, life-changing medicines. Requirements:\n- Bioassay testing\n- GMP standards",
            "",
        )

        self.assertIn("Biomemory", ov_biomemory)
        self.assertIn("DNA", ov_biomemory)
        self.assertNotIn("Regeneron", ov_biomemory)
        self.assertTrue(any("Bioinformatics" in x for x in b_biomemory))

        self.assertIn("Regeneron", ov_regeneron)
        self.assertNotIn("Biomemory", ov_regeneron)
        self.assertTrue(any("Bioassay" in x for x in b_regeneron))

    def test_skills_tags_isolation(self):
        skills = self.dashboard["get_job_skills_tags"]
        tags_bio = skills(
            "Bioinformatics Scientist", "Thermo Fisher", "Munich", "NGS, Python, R, Microarray", ""
        )
        tags_ferm = skills(
            "Fermentation Engineer", "Fooditive", "Rotterdam", "Fermentation, Bioprocessing, Cell culture", ""
        )

        self.assertIn("NGS", tags_bio)
        self.assertIn("Bioinformatics", tags_bio)
        self.assertNotIn("Fermentation", tags_bio)

        self.assertIn("Fermentation", tags_ferm)
        self.assertIn("Bioprocessing", tags_ferm)
        self.assertNotIn("NGS", tags_ferm)

    def test_evidence_checklist_isolation(self):
        evidence = self.dashboard["get_job_evidence_checklist"]
        job_thermo = {
            "title": "Bioinformatics Support Scientist",
            "company": "Thermo Fisher Scientific",
            "requirements_text": "NGS and Microarray analysis, Python, R pipeline, HPC cluster",
            "description": "Office based",
        }
        job_regen = {
            "title": "Temp QC Analyst - Bioassay",
            "company": "Regeneron",
            "requirements_text": "Bioassay, wet lab, cell culture, quality control, GMP",
            "description": "Lab based",
        }

        ev_thermo = evidence(job_thermo)
        ev_regen = evidence(job_regen)

        thermo_titles = [e["title"] for e in ev_thermo]
        regen_titles = [e["title"] for e in ev_regen]

        self.assertTrue(any("Biyoinformatik" in t for t in thermo_titles))
        self.assertTrue(any("HPC" in t for t in thermo_titles))
        self.assertTrue(any("Islak-Lab" in t or "QC" in t for t in regen_titles))

    def test_evidence_and_sections_no_raw_html_leaks(self):
        """Regression test: ensure evidence and section helpers return clean text without HTML tags."""
        evidence = self.dashboard["get_job_evidence_checklist"]
        job = {
            "title": "Bioinformatics Engineer",
            "company": "TestBio",
            "requirements_text": "Python, NGS, Linux",
            "description": "Lab description",
        }
        ev_list = evidence(job)
        for ev in ev_list:
            self.assertNotIn("<div", ev["title"])
            self.assertNotIn("<span", ev["title"])
            self.assertNotIn("<div", ev["desc"])
            self.assertNotIn("<span", ev["desc"])

        overview, bullets = self.dashboard["parse_job_sections"](
            1, job["title"], job["company"], job["requirements_text"], job["description"]
        )
        self.assertNotIn("<div", overview)
        self.assertNotIn("<span", overview)
        for b in bullets:
            self.assertNotIn("<div", b)


if __name__ == "__main__":
    unittest.main()
