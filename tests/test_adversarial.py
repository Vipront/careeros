import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.extraction.job_parser import parse_and_validate_job, sanitize_untrusted_text
from src.fact_registry import get_fact_registry

def test_all_adversarial_injections_neutralized():
    """Verify that all adversarial dataset payloads are detected and neutralized."""
    adv_file = ROOT / "data" / "adversarial_dataset.json"
    dataset = json.loads(adv_file.read_text(encoding="utf-8"))

    for item in dataset:
        raw_desc = item["description"]
        parsed = parse_and_validate_job(raw_desc, item["title"], item["company"])

        # 1. Check that instruction overrides are sanitized
        if item["attack_type"] in ["DIRECT_INSTRUCTION_OVERRIDE", "SYSTEM_OVERRIDE_AND_XSS", "DEVELOPER_MODE_JAILBREAK"]:
            assert parsed.is_injection_detected is True
            assert "ignore all previous instructions" not in str(parsed.dict()).lower()
            assert "<script>" not in str(parsed.dict()).lower()

        # 2. Check that hallucinated bait numbers (-45.0 kcal/mol) are not in Master CV Fact Registry
        if item["attack_type"] == "HALLUCINATION_BAIT_NUMBERS":
            reg = get_fact_registry()
            matched_facts = reg.match_claim_to_facts(raw_desc)
            assert len(matched_facts) == 0 # FactRegistry completely blocks bait numbers

if __name__ == "__main__":
    test_all_adversarial_injections_neutralized()
    print("All Adversarial Injection Tests Passed (100% Neutralized)!")
