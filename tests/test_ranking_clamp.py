import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.final_ranking import clamp

class TestRankingClamp(unittest.TestCase):
    def test_clamp_valid_within_bounds(self):
        self.assertEqual(clamp(50.0), 50.0)
        self.assertEqual(clamp(75.5), 75.5)
        self.assertEqual(clamp("42.5"), 42.5)

    def test_clamp_below_lower_bound(self):
        self.assertEqual(clamp(-10.0), 0.0)
        self.assertEqual(clamp(-5, lo=10.0), 10.0)

    def test_clamp_above_upper_bound(self):
        self.assertEqual(clamp(150.0), 100.0)
        self.assertEqual(clamp(85.0, hi=80.0), 80.0)

    def test_clamp_none_and_empty(self):
        self.assertEqual(clamp(None), 0.0)
        self.assertEqual(clamp(""), 0.0)

    def test_clamp_invalid_non_numeric_strings(self):
        # Should safely default to lo without throwing ValueError
        self.assertEqual(clamp("invalid"), 0.0)
        self.assertEqual(clamp("N/A", lo=5.0), 5.0)

if __name__ == "__main__":
    unittest.main()
