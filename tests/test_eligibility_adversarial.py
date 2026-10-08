"""Adversarial and boundary tests for eligibility models, profile adapter, and language/experience decisions."""

from __future__ import annotations

import unittest

from src.eligibility.experience import evaluate_experience_eligibility
from src.eligibility.language import (
    evaluate_language_eligibility,
    evaluate_posting_language_preference,
)
from src.eligibility.models import CriterionStatus
from src.eligibility.profile_adapter import (
    get_candidate_profile_facts,
    parse_cefr_level,
)
from src.filters.knockout import knockout_reason


class TestEligibilityAdversarial(unittest.TestCase):
    def test_absent_work_experience_is_none_not_zero(self) -> None:
        profile_data = {
            "education": [{"degree": "B.Sc.", "dates": "2021-2025"}],
            # No work_experience or industry_experience key
        }
        facts = get_candidate_profile_facts(profile_data)
        self.assertIsNone(facts["industry_experience_years"])
        self.assertIsNone(facts["total_work_experience_years"])

    def test_bare_language_string_is_unspecified_not_native(self) -> None:
        profile_data = {
            "skills": {
                "languages": ["German", "English (B2)"]
            }
        }
        facts = get_candidate_profile_facts(profile_data)
        langs = facts["languages"]
        self.assertIsNotNone(langs)
        self.assertEqual(langs.get("German"), "unspecified")
        self.assertEqual(langs.get("English"), "B2")

    def test_future_or_expected_education_is_not_graduated(self) -> None:
        profile_data = {
            "education": [
                {"degree": "B.Sc. Molecular Biology", "dates": "Sep 2021 – 2027 (expected)"}
            ]
        }
        facts = get_candidate_profile_facts(profile_data)
        self.assertFalse(facts["is_graduated"])
        self.assertIn("B.Sc. Molecular Biology", facts["enrolled_programs"])
        self.assertEqual(facts["completed_degrees"], [])

    def test_union_overlapping_dated_intervals_prevents_double_counting(self) -> None:
        profile_data = {
            "research_experience": [
                {"role": "Intern 1", "dates": "Jan 2025 – Jun 2025"},  # 6 months
                {"role": "Intern 2", "dates": "Mar 2025 – Aug 2025"},  # Overlaps Jan-Jun, extends to Aug (8 months total)
            ]
        }
        facts = get_candidate_profile_facts(profile_data)
        research_years = facts["research_experience_years"]
        self.assertIsNotNone(research_years)
        # 8 months is ~0.67 years, NOT 6+6=12 months (1.0 year)
        self.assertAlmostEqual(research_years, 0.67, delta=0.08)

    def test_incomplete_date_evidence_yields_none_for_totals(self) -> None:
        profile_data = {
            "research_experience": [
                {"role": "Intern", "dates": "No date provided"}
            ]
        }
        facts = get_candidate_profile_facts(profile_data)
        self.assertIsNone(facts["research_experience_years"])

    def test_posting_language_preference_not_waived_for_international_work_text(self) -> None:
        # A French posting is UNMET for posting language preference even if it mentions international teams
        french_text = (
            "Rejoignez notre équipe internationale dans un environnement d'excellence. "
            "Nous recherchons un ingénieur de recherche pour nos activités à Paris. "
            "Working language is English."
        )
        pref_decision = evaluate_posting_language_preference(french_text)
        self.assertEqual(pref_decision.status, CriterionStatus.UNMET)
        self.assertIn("French", pref_decision.reason)

    def test_mandatory_working_language_cefr_comparison(self) -> None:
        # Candidate has B2 English
        candidate = {
            "languages": {
                "English": "Professional working proficiency, B2",
                "Turkish": "Native",
            }
        }
        # Job strictly requires C1 English
        job_text_c1 = "Senior Scientist position. Fluent C1 English required for international grant writing."
        lang_decision = evaluate_language_eligibility(job_text_c1, candidate=candidate)
        self.assertEqual(lang_decision.status, CriterionStatus.UNMET)
        self.assertIn("requires C1", lang_decision.reason)

    def test_mandatory_working_language_with_unspecified_level_is_unknown(self) -> None:
        # Candidate has English with unspecified level
        candidate = {
            "languages": {
                "English": "unspecified",
            }
        }
        job_text = "Fluent English required."
        lang_decision = evaluate_language_eligibility(job_text, candidate=candidate)
        self.assertEqual(lang_decision.status, CriterionStatus.UNKNOWN)

    def test_knockout_allows_verified_candidate_with_sufficient_years(self) -> None:
        candidate_5y = {
            "is_graduated": True,
            "completed_degrees": ["B.Sc.", "M.Sc."],
            "enrolled_programs": [],
            "research_experience_years": 6.0,
            "industry_experience_years": 0.0,
            "total_work_experience_years": 6.0,
            "languages": {"English": "C1"},
        }
        title = "Senior Bioinformatician"
        desc = "Requires 5+ years of experience in bioinformatics."
        decision = evaluate_experience_eligibility(title, desc, candidate=candidate_5y)
        self.assertEqual(decision.status, CriterionStatus.MET)


if __name__ == "__main__":
    unittest.main()
