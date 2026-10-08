"""Contract tests for offline evaluation of saved production predictions."""

from __future__ import annotations

import unittest

from src.evaluation.production_eval import EvaluationInputError, compare_reports, evaluate_predictions


def prediction(
    job_id: str,
    is_match: bool,
    score: int,
    *,
    cost: float | None = 0.1,
    keyword_score: float = 50,
    semantic_score: float = 0.5,
) -> dict:
    return {
        "job_id": job_id,
        "llm_judge_result_json": {
            "is_match": is_match,
            "profile_type": "Wet Lab",
            "match_score": score,
            "missing_critical_skills": [],
            "relocation_supported": False,
            "confidence": 0.8,
            "reasoning": "Saved production judge output.",
        },
        "keyword_score": keyword_score,
        "semantic_score": semantic_score,
        "cost_usd": cost,
        "llm_model": "offline-test-model",
        "prompt_version": "prompt-test-1",
        "profile_version": "profile-test-1",
    }


def label(job_id: str, is_match: bool, reason: str = "Human reviewed label.") -> dict:
    return {"job_id": job_id, "is_match": is_match, "reason": reason}


class TestProductionEval(unittest.TestCase):
    def test_evaluates_saved_judge_results_and_keeps_false_negative_reason(self) -> None:
        report = evaluate_predictions(
            [prediction("1", False, 30), prediction("2", True, 90)],
            [label("1", True, "Relevant despite judge rejection."), label("2", True)],
        )

        self.assertEqual(report["metrics"]["true_positives"], 1)
        self.assertEqual(report["metrics"]["false_negatives"], 1)
        self.assertEqual(report["metrics"]["precision"], 1.0)
        self.assertEqual(report["metrics"]["recall"], 0.5)
        self.assertEqual(report["metrics"]["f1"], 2 / 3)
        self.assertEqual(report["false_negatives"][0]["human_reason"], "Relevant despite judge rejection.")
        self.assertEqual(report["versions"]["models"], ["offline-test-model"])
        # Positive LLM scores flow through CareerOS's production score function.
        self.assertEqual(report["judged_predictions"][0]["final_score"], 90.0)

    def test_precision_at_k_uses_available_judged_predictions_as_denominator(self) -> None:
        report = evaluate_predictions(
            [prediction("1", False, 90), prediction("2", True, 80), prediction("3", True, 70)],
            [label("1", False), label("2", True), label("3", False)],
        )

        self.assertEqual(report["metrics"]["precision_at_k"], 1 / 3)
        self.assertEqual(report["metrics"]["precision_at_k_denominator"], 3)
        self.assertEqual(report["metrics"]["precision_at_k_name"], "precision@10")

    def test_undefined_classification_metrics_and_missing_usage_are_null(self) -> None:
        report = evaluate_predictions([prediction("1", False, 0, cost=None)], [label("1", False)])

        self.assertIsNone(report["metrics"]["precision"])
        self.assertIsNone(report["metrics"]["recall"])
        self.assertIsNone(report["metrics"]["f1"])
        self.assertIsNone(report["usage"]["cost_usd_total"])
        self.assertIsNone(report["usage"]["cost_usd_per_useful_suggestion"])
        self.assertEqual(report["usage"]["cost_coverage_count"], 0)

    def test_db_export_json_string_is_accepted(self) -> None:
        row = prediction("1", True, 75)
        import json

        row["llm_judge_result_json"] = json.dumps(row["llm_judge_result_json"])
        report = evaluate_predictions([row], [label("1", True)])
        self.assertEqual(report["metrics"]["true_positives"], 1)

    def test_duplicate_ids_fail(self) -> None:
        with self.assertRaisesRegex(EvaluationInputError, "duplicate prediction ID"):
            evaluate_predictions([prediction("1", True, 80), prediction("1", False, 20)], [label("1", True)])

    def test_missing_or_extra_prediction_ids_fail_with_counts(self) -> None:
        with self.assertRaisesRegex(EvaluationInputError, r"missing_predictions=1"):
            evaluate_predictions([prediction("1", True, 80)], [label("1", True), label("2", False)])
        with self.assertRaisesRegex(EvaluationInputError, r"unlabelled_predictions=1"):
            evaluate_predictions([prediction("1", True, 80), prediction("2", False, 20)], [label("1", True)])

    def test_label_requires_boolean_and_human_reason(self) -> None:
        with self.assertRaisesRegex(EvaluationInputError, "is_match must be a JSON boolean"):
            evaluate_predictions([prediction("1", True, 80)], [{"job_id": "1", "is_match": 1, "reason": "reviewed"}])
        with self.assertRaisesRegex(EvaluationInputError, "reason must be a non-empty human explanation"):
            evaluate_predictions([prediction("1", True, 80)], [{"job_id": "1", "is_match": True, "reason": " "}])

    def test_invalid_saved_judge_output_fails_validation(self) -> None:
        row = prediction("1", True, 80)
        del row["llm_judge_result_json"]["profile_type"]
        with self.assertRaisesRegex(EvaluationInputError, "failed production judge validation"):
            evaluate_predictions([row], [label("1", True)])

    def test_snapshot_comparison_keeps_unknown_deltas_null(self) -> None:
        before = {"versions": {"models": ["a"]}, "metrics": {"precision": None, "recall": 0.5}}
        after = {"versions": {"models": ["b"]}, "metrics": {"precision": 1.0, "recall": 0.75}}

        comparison = compare_reports(after, before)
        self.assertIsNone(comparison["metric_deltas"]["precision"])
        self.assertEqual(comparison["metric_deltas"]["recall"], 0.25)
        self.assertEqual(comparison["previous_versions"]["models"], ["a"])

    def test_eligibility_decision_contract_requires_all_criteria_and_evidence(self) -> None:
        from src.eligibility.models import (
            CriterionDecision,
            CriterionStatus,
            EligibilityDecision,
            LivenessDecision,
            LivenessStatusContract,
            OverallEligibilityStatus,
        )

        decision = EligibilityDecision(
            job_id="4624",
            overall_status=OverallEligibilityStatus.ELIGIBLE,
            can_recommend=True,
            can_generate_documents=True,
            can_notify=True,
            experience=CriterionDecision(
                status=CriterionStatus.MET,
                reason="Student qualification matches verified B.Sc. candidate.",
                evidence_spans=["Student in computational biology, bioinformatics, or a related field."],
                checked_at="2026-10-08T12:00:00Z",
                rule_version="1.0.0",
            ),
            language=CriterionDecision(
                status=CriterionStatus.MET,
                reason="English job post matches candidate B2 English.",
                evidence_spans=["English text"],
                checked_at="2026-10-08T12:00:00Z",
                rule_version="1.0.0",
            ),
            posting_language_preference=CriterionDecision(
                status=CriterionStatus.MET,
                reason="English is preferred posting language.",
                evidence_spans=["Language: EN"],
                checked_at="2026-10-08T12:00:00Z",
                rule_version="1.0.0",
            ),
            liveness=LivenessDecision(
                status=LivenessStatusContract.OPEN,
                reason="HTTP 200 with active application controls.",
                evidence_spans=["apply button present"],
                checked_at="2026-10-08T12:00:00Z",
                http_code=200,
                rule_version="1.0.0",
            ),
            evaluated_at="2026-10-08T12:00:00Z",
            rule_version="1.0.0",
            profile_version="1.0.0",
        )

        dumped = decision.to_dict()
        self.assertEqual(dumped["overall_status"], "eligible")
        self.assertTrue(dumped["can_recommend"])
        self.assertEqual(dumped["experience"]["status"], "met")
        self.assertEqual(dumped["liveness"]["status"], "open")


if __name__ == "__main__":
    unittest.main()
