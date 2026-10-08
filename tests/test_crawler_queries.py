import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

class TestCrawlerQueries(unittest.TestCase):
    def setUp(self):
        from src.collectors import linkedin_crawler
        self.targets = linkedin_crawler.SEARCH_TARGETS

    def test_targets_structure_and_count(self):
        """Ensure search targets maintain exact 6 entries with required schema."""
        self.assertEqual(len(self.targets), 6, "Target count must remain exactly 6")
        for idx, t in enumerate(self.targets):
            self.assertIn("keywords", t, f"Target {idx} missing keywords")
            self.assertIn("location", t, f"Target {idx} missing location")
            self.assertIsInstance(t["keywords"], str)
            self.assertIsInstance(t["location"], str)

    def test_locations_preserved(self):
        """Ensure Germany, European Union, and Istanbul locations are preserved."""
        locations = [t["location"] for t in self.targets]
        self.assertEqual(locations[0], "Germany")
        self.assertEqual(locations[1], "Germany")
        self.assertEqual(locations[2], "European Union")
        self.assertEqual(locations[3], "European Union")
        self.assertEqual(locations[4], "Istanbul, Turkey")
        self.assertEqual(locations[5], "Istanbul, Turkey")

    def test_target_5_no_standalone_ar_ge(self):
        """Target 5 must not have standalone 'Ar-Ge' or 'R&D' that triggers industrial non-bio jobs."""
        t5_kw = self.targets[4]["keywords"]
        # Must not contain "Ar-Ge" or "R&D"
        self.assertNotIn('"Ar-Ge"', t5_kw, "Target 5 should not contain standalone 'Ar-Ge'")
        self.assertNotIn('"R&D"', t5_kw, "Target 5 should not contain standalone 'R&D'")
        # Must still anchor on biology/laboratory
        self.assertIn("Laboratuvar", t5_kw)
        self.assertIn("Biyoloji", t5_kw)

    def test_target_6_no_standalone_veri_analizi(self):
        """Target 6 must not have standalone 'Veri Analizi' that pulls generic banking/IT data science."""
        t6_kw = self.targets[5]["keywords"]
        # If "Veri Analizi" is present, it must be combined with domain terms (e.g., AND (Biyoloji...))
        self.assertNotIn('OR "Veri Analizi")', t6_kw, "Target 6 must not have standalone 'Veri Analizi'")
        self.assertIn('"Veri Analizi"', t6_kw, "Target 6 should still retain biological 'Veri Analizi' capability")
        self.assertIn("Biyoloji", t6_kw, "Veri Analizi must be anchored to biology/health domain")

    def test_target_2_seniority_exclusion(self):
        """Target 2 should include seniority exclusion (NOT (Senior OR Director OR Lead))."""
        t2_kw = self.targets[1]["keywords"]
        self.assertIn("NOT", t2_kw, "Target 2 should include negative exclusion for seniority")
        self.assertIn("Senior", t2_kw)

    def test_target_1_and_3_preserved(self):
        """Target 1 and 3 core biological & student/intern anchors must remain intact."""
        t1_kw = self.targets[0]["keywords"]
        self.assertIn("Molecular Biology", t1_kw)
        self.assertIn("Working Student", t1_kw)

        t3_kw = self.targets[2]["keywords"]
        self.assertIn("Molecular Biology", t3_kw)
        self.assertIn("Intern", t3_kw)

if __name__ == "__main__":
    unittest.main()
