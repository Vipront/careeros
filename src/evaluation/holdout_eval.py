import sys
import json
from pathlib import Path
from typing import Dict, Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def run_holdout_evaluation(holdout_path: Path = None) -> Dict[str, Any]:
    from src.evaluation.benchmark import evaluate_job_eligibility

    dataset_file = holdout_path or (ROOT / "data" / "holdout_dataset.json")
    if not dataset_file.exists():
        raise FileNotFoundError(f"Holdout dataset not found at {dataset_file}")

    dataset = json.loads(dataset_file.read_text(encoding="utf-8"))

    tp = 0
    fp = 0
    fn = 0
    tn = 0
    exact_matches = 0

    results = []
    for item in dataset:
        human_label = item["human_label"]
        pred_label = evaluate_job_eligibility(item)

        if human_label == pred_label:
            exact_matches += 1

        h_binary = 1 if human_label >= 1 else 0
        p_binary = 1 if pred_label >= 1 else 0

        if h_binary == 1 and p_binary == 1:
            tp += 1
        elif h_binary == 0 and p_binary == 1:
            fp += 1
        elif h_binary == 1 and p_binary == 0:
            fn += 1
        else:
            tn += 1

        results.append({
            "id": item["id"],
            "title": item["title"],
            "human_label": human_label,
            "pred_label": pred_label,
            "is_correct": human_label == pred_label
        })

    precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0
    f1 = 2 * (precision * recall) / (precision + recall) if (precision + recall) > 0 else 1.0
    accuracy = exact_matches / len(dataset) if dataset else 1.0

    return {
        "holdout_dataset_size": len(dataset),
        "unseen_precision": round(precision, 4),
        "unseen_recall": round(recall, 4),
        "unseen_f1_score": round(f1, 4),
        "unseen_exact_accuracy": round(accuracy, 4),
        "false_positives": fp,
        "false_negatives": fn,
        "unsupported_claim_rate": None,
        "unsupported_claim_rate_scope": (
            "Not measured: holdout evaluation predicts job eligibility and does not generate or verify documents."
        ),
        "detailed_results": results
    }

if __name__ == "__main__":
    rep = run_holdout_evaluation()
    print("=== V2.1 HOLDOUT EVALUATION REPORT ===")
    print(f"Unseen Jobs Evaluated:      {rep['holdout_dataset_size']}")
    print(f"Generalization Precision:   {rep['unseen_precision']*100:.1f}%")
    print(f"Generalization Recall:      {rep['unseen_recall']*100:.1f}%")
    print(f"Generalization F1-Score:    {rep['unseen_f1_score']*100:.1f}%")
    if rep["unsupported_claim_rate"] is None:
        print(f"Unsupported Claim Rate:     not measured ({rep['unsupported_claim_rate_scope']})")
    else:
        print(f"Unsupported Claim Rate:     {rep['unsupported_claim_rate']:.1f}%")
