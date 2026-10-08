"""Offline evaluation of saved CareerOS production judge results.

This module reads JSONL snapshots and human-authored labels. It never opens the
application database and never invokes an LLM provider.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.final_ranking import calculate_final_score
from src.llm_judge import validate_result


class EvaluationInputError(ValueError):
    """Raised when a predictions, labels, or comparison file is malformed."""


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EvaluationInputError(
                    f"{path}:{line_number}: invalid JSON: {exc.msg}"
                ) from exc
            if not isinstance(value, dict):
                raise EvaluationInputError(
                    f"{path}:{line_number}: each JSONL row must be an object"
                )
            rows.append(value)
    return rows


def _record_id(row: Mapping[str, Any], *, kind: str, index: int) -> str:
    raw_id = row.get("job_id", row.get("id"))
    if isinstance(raw_id, bool) or raw_id is None or not str(raw_id).strip():
        raise EvaluationInputError(f"{kind} row {index} must contain a non-empty job_id or id")
    return str(raw_id)


def _index_unique(rows: Iterable[dict[str, Any]], *, kind: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows, start=1):
        identifier = _record_id(row, kind=kind, index=index)
        if identifier in indexed:
            raise EvaluationInputError(f"duplicate {kind} ID {identifier!r}")
        indexed[identifier] = row
    return indexed


def _prediction_result(row: Mapping[str, Any], *, identifier: str) -> dict[str, Any]:
    """Adapt DB-export rows, JSONL judge rows, and direct judge objects."""
    result: Any = row.get("llm_judge_result_json")
    if result is None:
        result = row.get("result", row.get("prediction"))
    if result is None and {"is_match", "match_score"} <= row.keys():
        result = row
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError as exc:
            raise EvaluationInputError(f"prediction {identifier!r} has invalid saved judge JSON") from exc
    if not isinstance(result, dict):
        raise EvaluationInputError(f"prediction {identifier!r} has no judge result object")
    try:
        validated = validate_result(result)
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvaluationInputError(f"prediction {identifier!r} failed production judge validation: {exc}") from exc
    if not isinstance(validated.get("is_match"), bool):
        raise EvaluationInputError(f"prediction {identifier!r} is_match must be a JSON boolean")
    return validated


def _number_or_none(value: Any, *, field: str, identifier: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise EvaluationInputError(f"prediction {identifier!r} field {field} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise EvaluationInputError(f"prediction {identifier!r} field {field} must be numeric") from exc
    if not math.isfinite(parsed):
        raise EvaluationInputError(f"prediction {identifier!r} field {field} must be finite")
    return parsed


def _metric(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _metadata_values(rows: Mapping[str, dict[str, Any]]) -> dict[str, list[str]]:
    metadata: dict[str, list[str]] = {}
    for output_field, source_fields in {
        "models": ("llm_model", "model"),
        "prompt_versions": ("prompt_version",),
        "profile_versions": ("profile_version",),
    }.items():
        values = {
            str(row[field]).strip()
            for row in rows.values()
            for field in source_fields
            if row.get(field) is not None and str(row[field]).strip()
        }
        metadata[output_field] = sorted(values)
    return metadata


def evaluate_predictions(
    predictions: list[dict[str, Any]],
    labels: list[dict[str, Any]],
    *,
    top_k: int = 10,
) -> dict[str, Any]:
    """Evaluate saved predictions against supplied labels using strict ID joins."""
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    prediction_by_id = _index_unique(predictions, kind="prediction")
    label_by_id = _index_unique(labels, kind="label")

    invalid_labels: list[str] = []
    for identifier, label in label_by_id.items():
        if not isinstance(label.get("is_match"), bool):
            invalid_labels.append(f"{identifier}: is_match must be a JSON boolean")
        if not isinstance(label.get("reason"), str) or not label["reason"].strip():
            invalid_labels.append(f"{identifier}: reason must be a non-empty human explanation")
    if invalid_labels:
        raise EvaluationInputError("invalid labels: " + "; ".join(invalid_labels))

    missing_predictions = sorted(set(label_by_id) - set(prediction_by_id))
    unlabelled_predictions = sorted(set(prediction_by_id) - set(label_by_id))
    if missing_predictions or unlabelled_predictions:
        raise EvaluationInputError(
            "prediction/label IDs must match exactly; "
            f"missing_predictions={len(missing_predictions)} {missing_predictions[:10]}; "
            f"unlabelled_predictions={len(unlabelled_predictions)} {unlabelled_predictions[:10]}"
        )

    tp = fp = tn = fn = 0
    judged: list[dict[str, Any]] = []
    supplied_costs: list[float] = []
    all_costs_supplied = bool(prediction_by_id)

    for identifier, prediction in prediction_by_id.items():
        result = _prediction_result(prediction, identifier=identifier)
        label = label_by_id[identifier]
        predicted_match = result["is_match"]
        actual_match = label["is_match"]
        if predicted_match and actual_match:
            tp += 1
        elif predicted_match:
            fp += 1
        elif actual_match:
            fn += 1
        else:
            tn += 1

        llm_score = float(result["match_score"])
        keyword_score = _number_or_none(prediction.get("keyword_score"), field="keyword_score", identifier=identifier)
        semantic_score = _number_or_none(prediction.get("semantic_score"), field="semantic_score", identifier=identifier)
        final_score = calculate_final_score(keyword_score, semantic_score, llm_score)

        cost = _number_or_none(prediction.get("cost_usd"), field="cost_usd", identifier=identifier)
        if cost is None:
            all_costs_supplied = False
        else:
            supplied_costs.append(cost)

        judged.append({
            "job_id": identifier,
            "is_match": predicted_match,
            "human_is_match": actual_match,
            "human_reason": label["reason"].strip(),
            "match_score": llm_score,
            "final_score": final_score,
            "llm_model": prediction.get("llm_model", prediction.get("model")),
            "prompt_version": prediction.get("prompt_version"),
            "profile_version": prediction.get("profile_version"),
        })

    ranked = sorted(judged, key=lambda item: (-item["final_score"], item["job_id"]))
    judged_top_k = min(top_k, len(ranked))
    top_rows = ranked[:judged_top_k]
    top_k_precision = _metric(sum(row["human_is_match"] for row in top_rows), judged_top_k)
    total_cost = sum(supplied_costs) if all_costs_supplied else None

    return {
        "report_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "label_count": len(label_by_id),
            "prediction_count": len(prediction_by_id),
            "judged_count": len(judged),
            "coverage": _metric(len(judged), len(label_by_id)),
            "unlabelled_prediction_count": 0,
        },
        "metrics": {
            "true_positives": tp,
            "false_positives": fp,
            "true_negatives": tn,
            "false_negatives": fn,
            "precision": _metric(tp, tp + fp),
            "recall": _metric(tp, tp + fn),
            "f1": _metric(2 * tp, 2 * tp + fp + fn),
            "precision_at_k": top_k_precision,
            "precision_at_k_name": f"precision@{top_k}",
            "precision_at_k_denominator": judged_top_k,
            "precision_at_k_scope": "human-positive labels among the highest final-score judged predictions",
        },
        "usage": {
            "cost_usd_total": total_cost,
            "cost_usd_per_useful_suggestion": total_cost / tp if total_cost is not None and tp else None,
            "useful_suggestion_definition": "a prediction marked is_match=true and labelled is_match=true (true positive)",
            "cost_coverage_count": len(supplied_costs),
            "cost_record_count": len(prediction_by_id),
        },
        "versions": _metadata_values(prediction_by_id),
        "false_negatives": [row for row in judged if row["human_is_match"] and not row["is_match"]],
        "judged_predictions": ranked,
    }


def compare_reports(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """Return metric deltas while retaining unavailable values as null."""
    deltas: dict[str, float | None] = {}
    for key, current_value in current.get("metrics", {}).items():
        previous_value = previous.get("metrics", {}).get(key)
        if isinstance(current_value, (int, float)) and not isinstance(current_value, bool) and isinstance(
            previous_value, (int, float)
        ) and not isinstance(previous_value, bool):
            deltas[key] = current_value - previous_value
        elif current_value is None or previous_value is None:
            deltas[key] = None
    return {
        "previous_versions": previous.get("versions"),
        "current_versions": current.get("versions"),
        "metric_deltas": deltas,
    }


def _write_json(path: Path | None, value: dict[str, Any]) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if path is None:
        sys.stdout.write(rendered)
    else:
        path.write_text(rendered, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate saved production judge JSONL offline.")
    parser.add_argument("--predictions", required=True, type=Path, help="JSONL export of saved production judge results")
    parser.add_argument("--labels", required=True, type=Path, help="Human-authored JSONL labels")
    parser.add_argument("--output", type=Path, help="Write the JSON report to this path (default: stdout)")
    parser.add_argument("--compare", type=Path, help="Compare this report with a previous report JSON")
    parser.add_argument("--top-k", type=int, default=10, help="Precision-at-k cutoff (default: 10)")
    args = parser.parse_args(argv)

    try:
        report = evaluate_predictions(_read_jsonl(args.predictions), _read_jsonl(args.labels), top_k=args.top_k)
        if args.compare:
            previous = json.loads(args.compare.read_text(encoding="utf-8"))
            if not isinstance(previous, dict):
                raise EvaluationInputError("comparison snapshot must be a JSON object")
            report["comparison"] = compare_reports(report, previous)
        _write_json(args.output, report)
    except (OSError, json.JSONDecodeError, EvaluationInputError, ValueError) as exc:
        print(f"production evaluation failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
