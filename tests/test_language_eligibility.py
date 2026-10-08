"""Tests for language eligibility and posting language preference decisions."""

from __future__ import annotations

import unittest

from src.eligibility.language import (
    evaluate_language_eligibility,
    evaluate_posting_language_preference,
)
from src.eligibility.models import CriterionStatus


class TestLanguageEligibility(unittest.TestCase):
    def setUp(self) -> None:
        self.mock_candidate = {
            "languages": {
                "Turkish": "Native",
                "English": "Professional working proficiency, B2",
            }
        }

    def test_job_4515_french_technician_posting_preference_unmet(self) -> None:
        text = (
            "Vous êtes une personne passionnée par la biologie moléculaire et souhaitez "
            "contribuer à l'excellence de la recherche scientifique ? Rejoignez-nous en tant "
            "que technicien supérieur pour un CDD de 18 mois au sein de l'Institut JACOB ! "
            "Vos missions sont : L'extraction des acides nucléiques, les contrôles qualitatif..."
        )
        # Posting language preference is strictly UNMET (no EN/TR waiver)
        pref_decision = evaluate_posting_language_preference(text)
        self.assertEqual(pref_decision.status, CriterionStatus.UNMET)
        self.assertIn("French", pref_decision.reason)

    def test_job_4515_with_explicit_mandatory_french_is_unmet(self) -> None:
        text = (
            "Technicien supérieur. Maîtrise du français exigée pour la rédaction des cahiers de laboratoire."
        )
        candidate = {
            "languages": {
                "Turkish": "Native",
                "English": "Professional working proficiency, B2",
            },
            "absent_languages": ["French"],
        }
        lang_decision = evaluate_language_eligibility(text, candidate=candidate)
        self.assertEqual(lang_decision.status, CriterionStatus.UNMET)
        self.assertIn("French", lang_decision.reason)

    def test_job_4515_french_without_explicit_absence_is_unknown(self) -> None:
        text = (
            "Technicien supérieur. Maîtrise du français exigée pour la rédaction des cahiers de laboratoire."
        )
        lang_decision = evaluate_language_eligibility(text, candidate=self.mock_candidate)
        self.assertEqual(lang_decision.status, CriterionStatus.UNKNOWN)
        self.assertIn("French", lang_decision.reason)


    def test_job_4599_french_bioinformatics_posting_preference_unmet(self) -> None:
        text = (
            "Institut de Génétique Humaine | Montpellier, France. "
            "Assurer la curation scientifique, le contrôle qualité et la mise à jour continue "
            "des bases IMGT/mAb-DB, IMGT/2Dstructure-DB et IMGT/3Dstructure-DB. "
            "Vous intégrerez une équipe de recherche dynamique..."
        )
        pref_decision = evaluate_posting_language_preference(text)
        self.assertEqual(pref_decision.status, CriterionStatus.UNMET)

    def test_job_4624_english_internship_met(self) -> None:
        text = (
            "AIXIAL GROUP | Sèvres, France. Computational Biology internship. "
            "We are seeking a motivated and talented intern to join our research and innovation team. "
            "Student in computational biology, bioinformatics, or a related field. "
            "Experience in programming using Python, R. Excellent communication and presentation skills."
        )
        pref_decision = evaluate_posting_language_preference(text)
        self.assertEqual(pref_decision.status, CriterionStatus.MET)

        lang_decision = evaluate_language_eligibility(text, candidate=self.mock_candidate)
        self.assertEqual(lang_decision.status, CriterionStatus.MET)

    def test_french_posting_with_english_work_fails_posting_preference_but_passes_working_language(self) -> None:
        text = (
            "Rejoignez notre équipe de recherche à Paris. Vous participerez aux missions de développement "
            "dans notre laboratoire d'excellence au sein de notre institut. Ce poste s'adresse aux candidats "
            "motivés souhaitant réaliser des travaux scientifiques. "
            "Working language is English across all project teams."
        )
        # Posting language preference is UNMET (written in French, no automatic waiver)
        pref_decision = evaluate_posting_language_preference(text)
        self.assertEqual(pref_decision.status, CriterionStatus.UNMET)
        self.assertIn("French", pref_decision.reason)

        # Working language is MET (English matches candidate)
        lang_decision = evaluate_language_eligibility(text, candidate=self.mock_candidate)
        self.assertEqual(lang_decision.status, CriterionStatus.MET)

    def test_preferred_language_does_not_block(self) -> None:
        text = (
            "Bioinformatics Analyst position in Munich, Germany. "
            "Required: Fluent English and experience in Python/R. "
            "German is a plus for everyday local interactions."
        )
        lang_decision = evaluate_language_eligibility(text, candidate=self.mock_candidate)
        self.assertEqual(lang_decision.status, CriterionStatus.MET)
        self.assertIn("preferred", lang_decision.reason.lower())


if __name__ == "__main__":
    unittest.main()
