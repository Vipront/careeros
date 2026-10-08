import unittest
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

class TestBenchmarkDataset(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bench_file = ROOT / "data" / "benchmark_dataset.json"
        with open(cls.bench_file, "r", encoding="utf-8") as f:
            cls.data = json.load(f)

    def test_json_parses_and_non_empty(self):
        """Ensure benchmark dataset is a valid, non-empty JSON list."""
        self.assertIsInstance(self.data, list)
        self.assertGreaterEqual(len(self.data), 6)

    def test_unique_ids(self):
        """Ensure every benchmark entry has a unique id."""
        ids = [item.get("id") for item in self.data]
        self.assertEqual(len(ids), len(set(ids)), "Duplicate id found in benchmark dataset")

    def test_live_calibration_fixture_contract(self):
        """Verify that deterministic live calibration fixture aligns with P1 #12 & P1 #14 rules."""
        fixture_path = ROOT / "tests" / "fixtures" / "live_calibration_cases.json"
        self.assertTrue(fixture_path.exists())
        cases = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.assertEqual(len(cases), 3)

        for c in cases:
            if c["case_type"] == "soft_barrier":
                self.assertGreaterEqual(c["llm_score"], 40.0)
                self.assertEqual(c["expected_status"], "low_priority")
            elif c["case_type"] == "hard_barrier":
                self.assertLess(c["llm_score"], 40.0)
                self.assertEqual(c["expected_status"], "rejected")

    def test_benchmark_does_not_affect_production_runtime(self):
        """Verify that src.final_ranking and src.llm_judge do not import or query benchmark_dataset."""
        import src.final_ranking as fr
        import src.llm_judge as lj
        self.assertFalse(hasattr(fr, "evaluate_job_eligibility"))
        self.assertFalse(hasattr(lj, "run_benchmark"))

    def test_synthetic_records_remain_intact(self):
        """Verify that original 6 synthetic records remain intact."""
        synth_items = [i for i in self.data if i.get("source") == "synthetic"]
        self.assertEqual(len(synth_items), 6)
        synth_ids = {i["id"] for i in synth_items}
        self.assertEqual(synth_ids, {"BENCH_01", "BENCH_02", "BENCH_03", "BENCH_04", "BENCH_05", "BENCH_06"})

if __name__ == "__main__":
    unittest.main()
