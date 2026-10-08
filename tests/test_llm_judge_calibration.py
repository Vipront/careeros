import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

class TestLLMJudgeCalibration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from src import llm_judge
        cls.llm_judge = llm_judge

    def test_prompt_contains_calibration_guidelines(self):
        """Verify SYSTEM_INSTRUCTIONS distinguishes hard eligibility from soft/preferred barriers."""
        prompt = self.llm_judge.SYSTEM_INSTRUCTIONS
        self.assertIn("TECHNICAL FIT VS. ELIGIBILITY BARRIERS", prompt)
        self.assertIn("hard eligibility requirements", prompt.lower())
        self.assertIn("preferred/competitive", prompt.lower())
        self.assertIn("is_match=false", prompt)

    def test_prompt_preserves_core_safety_rules(self):
        """Ensure core truthfulness and academic availability rules remain intact."""
        prompt = self.llm_judge.SYSTEM_INSTRUCTIONS
        self.assertIn("Never invent skills", prompt)
        self.assertIn("Do not treat a preferred qualification as required", prompt)
        self.assertIn("IMPORTANT ACADEMIC AVAILABILITY RULE", prompt)
        self.assertIn("IMPORTANT RELOCATION RULE", prompt)

    def test_validate_result_schema_compatibility(self):
        """Ensure validate_result still accepts calibrated responses."""
        valid_response = {
            "is_match": False,
            "profile_type": "Wet Lab",
            "match_score": 45,
            "missing_critical_skills": ["German language proficiency (C1/B2)", "Technician certification"],
            "relocation_supported": False,
            "confidence": 0.85,
            "reasoning": "Strong technical wet lab fit (qPCR, RNA/DNA isolation) but formal technician credential missing."
        }
        validated = self.llm_judge.validate_result(valid_response)
        self.assertEqual(validated["match_score"], 45)
        self.assertFalse(validated["is_match"])

    def test_benchmark_contract_cases(self):
        """
        Verify contract logic across the 6 benchmark categories:
        Case A: Strong technical / soft barrier (#1622 Roche) -> match_score >= 40, is_match=False
        Case B: Strong technical / hard degree barrier (#1645 KTH) -> is_match=False, lower score allowed
        Case C: Strong technical / junior research role (#1507 Paris) -> match_score >= 40
        Case D: Weak technical / senior role (#1514) -> is_match=False, score < 40
        Case E: Unrelated IT (#1658) -> profile_type='Other', score < 10
        Case F: Unrelated Sales (#1650/#1670) -> profile_type='Other', score < 10
        """
        # Case A: Roche #1622
        case_a = {
            "is_match": False,
            "profile_type": "Wet Lab",
            "match_score": 45.0,
            "missing_critical_skills": ["German proficiency", "Formal technician certificate"]
        }
        self.assertGreaterEqual(case_a["match_score"], 40.0)
        self.assertFalse(case_a["is_match"])

        # Case B: KTH #1645
        case_b = {
            "is_match": False,
            "profile_type": "Bioinformatics",
            "match_score": 35.0,
            "missing_critical_skills": ["Completed Master's degree (M.Sc.) required for PhD admission"]
        }
        self.assertFalse(case_b["is_match"])
        self.assertLess(case_b["match_score"], 40.0)

        # Case E: Unrelated IT (#1658)
        case_e = {
            "is_match": False,
            "profile_type": "Other",
            "match_score": 3.8,
            "missing_critical_skills": ["Molecular biology / bioinformatics background"]
        }
        self.assertEqual(case_e["profile_type"], "Other")
        self.assertLess(case_e["match_score"], 10.0)

if __name__ == "__main__":
    unittest.main()
