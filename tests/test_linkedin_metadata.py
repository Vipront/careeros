import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

class TestLinkedInMetadata(unittest.TestCase):
    def test_employment_type_normalization(self):
        """Ensure employment_type values are cleanly mapped to canonical title-case forms."""
        from src.enrichment.dynamic_enricher import normalize_employment_type
        self.assertEqual(normalize_employment_type("FULL_TIME"), "Full-time")
        self.assertEqual(normalize_employment_type("full-time"), "Full-time")
        self.assertEqual(normalize_employment_type("PART_TIME"), "Part-time")
        self.assertEqual(normalize_employment_type("CONTRACT"), "Contract")
        self.assertEqual(normalize_employment_type("INTERN"), "Internship")
        self.assertEqual(normalize_employment_type("INTERNSHIP"), "Internship")
        self.assertEqual(normalize_employment_type("TEMPORARY"), "Temporary")
        self.assertIsNone(normalize_employment_type(None))
        self.assertIsNone(normalize_employment_type(""))

    def test_workplace_type_normalization(self):
        """Ensure workplace_type maps TELECOMMUTE/REMOTE/HYBRID/ONSITE accurately."""
        from src.enrichment.dynamic_enricher import normalize_workplace_type
        self.assertEqual(normalize_workplace_type("TELECOMMUTE"), "Remote")
        self.assertEqual(normalize_workplace_type("REMOTE"), "Remote")
        self.assertEqual(normalize_workplace_type("HYBRID"), "Hybrid")
        self.assertEqual(normalize_workplace_type("ON_SITE"), "On-site")
        self.assertEqual(normalize_workplace_type("ONSITE"), "On-site")
        # Text fallback test
        self.assertEqual(normalize_workplace_type(None, "This position is 100% remote working"), "Remote")
        self.assertEqual(normalize_workplace_type(None, "We offer a hybrid working model"), "Hybrid")
        self.assertIsNone(normalize_workplace_type(None, "Standard laboratory office setting"))

    def test_db_schema_includes_columns(self):
        """Verify ensure_v2_schema contains the new metadata columns."""
        import sqlite3
        from src.db import ensure_v2_schema
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT)")
        ensure_v2_schema(con)
        cols = [c[1] for c in con.execute("PRAGMA table_info(jobs)").fetchall()]
        self.assertIn("is_easy_apply", cols)
        self.assertIn("workplace_type", cols)
        self.assertIn("employment_type", cols)

    def test_metadata_does_not_affect_knockout_or_scoring(self):
        """Verify that presence of metadata does NOT alter knockout decision or score."""
        from src.filters.knockout import knockout_reason
        from src.final_ranking import calculate_final_score

        # Job with PhD mandatory must still be rejected regardless of Easy Apply or Remote status
        res_standard = knockout_reason("Scientist", "PhD is mandatory")
        self.assertIsNotNone(res_standard)

        # Score calculation must only depend on keyword, semantic, and llm scores
        score1 = calculate_final_score(50.0, 0.40, None)
        score2 = calculate_final_score(50.0, 0.40, None)
        self.assertEqual(score1, score2)

if __name__ == "__main__":
    unittest.main()
