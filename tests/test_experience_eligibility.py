"""Tests for evidence-based mandatory and preferred experience decisions."""

from __future__ import annotations

import unittest

from src.eligibility.experience import (
    evaluate_experience_eligibility,
    extract_mandatory_experience_years,
    extract_preferred_experience_years,
)
from src.eligibility.models import CriterionStatus


class TestExperienceEligibility(unittest.TestCase):
    def setUp(self) -> None:
        self.mock_candidate = {
            "is_graduated": False,
            "completed_degrees": [],
            "enrolled_programs": ["B.Sc. Molecular Biology and Genetics"],
            "research_experience_years": 0.25,
            "industry_experience_years": 0.0,
            "total_work_experience_years": 0.25,
            "languages": {"Turkish": "Native", "English": "B2"},
        }

    def test_job_4291_mlo_degree_and_work_experience_unmet(self) -> None:
        title = "Associate Research Technician"
        desc = (
            "We expect our associate research technician: "
            "To have an MLO degree level 4 with 0-2 years working experience (preferably in industry)"
        )
        decision = evaluate_experience_eligibility(title, desc, candidate=self.mock_candidate)
        self.assertEqual(decision.status, CriterionStatus.UNMET)
        self.assertIn("MLO degree", decision.reason)

    def test_job_4624_internship_student_accepted(self) -> None:
        title = "Computational Biology internship"
        desc = (
            "Student in computational biology, bioinformatics, or a related field. "
            "Experience in programming using Python, R, or other languages."
        )
        decision = evaluate_experience_eligibility(title, desc, candidate=self.mock_candidate)
        self.assertEqual(decision.status, CriterionStatus.MET)

    def test_mandatory_experience_deficit_is_strictly_unmet(self) -> None:
        title = "Laboratory Technician"
        desc = "Requires a minimum of 2 years of relevant experience in a molecular biology laboratory."
        decision = evaluate_experience_eligibility(title, desc, candidate=self.mock_candidate)
        self.assertEqual(decision.status, CriterionStatus.UNMET)
        self.assertIn("Requires 2+ years mandatory experience", decision.reason)

    def test_mandatory_industry_experience_deficit_is_unmet(self) -> None:
        title = "Bioinformatics Scientist"
        desc = "Must have industry experience in pharmaceutical biotechnology."
        decision = evaluate_experience_eligibility(title, desc, candidate=self.mock_candidate)
        self.assertEqual(decision.status, CriterionStatus.UNMET)
        self.assertIn("industry experience", decision.reason.lower())

    def test_preferred_experience_does_not_disqualify(self) -> None:
        title = "Research Assistant"
        desc = "B.Sc. in life sciences. 2+ years of lab experience preferred."
        decision = evaluate_experience_eligibility(title, desc, candidate=self.mock_candidate)
        self.assertEqual(decision.status, CriterionStatus.MET)
        self.assertIn("preferred", decision.reason.lower())

    def test_missing_candidate_facts_yields_unknown_never_assumed_zero(self) -> None:
        empty_candidate = {
            "is_graduated": None,
            "completed_degrees": [],
            "enrolled_programs": [],
            "research_experience_years": None,
            "industry_experience_years": None,
            "total_work_experience_years": None,
        }
        title = "Research Assistant"
        desc = "Requires 3+ years of experience."
        decision = evaluate_experience_eligibility(title, desc, candidate=empty_candidate)
        self.assertEqual(decision.status, CriterionStatus.UNKNOWN)

    def test_postdoc_requirement_is_unmet_for_undergrad(self) -> None:
        title = "Postdoctoral Fellow in Genomics"
        desc = "Conduct research on evolutionary genetics."
        decision = evaluate_experience_eligibility(title, desc, candidate=self.mock_candidate)
        self.assertEqual(decision.status, CriterionStatus.UNMET)

    def test_master_thesis_student_requirement_is_unmet_for_undergrad(self) -> None:
        title = "Master Thesis Student - Biochemistry"
        desc = "Role is a master's thesis requiring current enrollment in a Master's programme."
        decision = evaluate_experience_eligibility(title, desc, candidate=self.mock_candidate)
        self.assertEqual(decision.status, CriterionStatus.UNMET)


if __name__ == "__main__":
    unittest.main()
