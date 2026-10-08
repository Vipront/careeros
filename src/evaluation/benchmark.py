import json
from pathlib import Path
from typing import Dict, Any

ROOT = Path(__file__).resolve().parents[2]

def evaluate_job_eligibility(job: Dict[str, Any]) -> int:
    """
    Evaluates a job posting using V2.0 eligibility and returns predicted class:
    0 = Reject (Irrelevant / Hard constraint mismatch)
    1 = Review / Borderline
    2 = Perfect / Strong Match
    """
    desc = (job.get("description", "") + " " + job.get("title", "")).lower()

    # Hard Knockouts
    if "8+ years" in desc or "10+ years" in desc or "native german" in desc or "c2" in desc:
        return 0
    if "kubernetes" in desc and "terraform" in desc and "biology" not in desc:
        return 0

    # Strong Match Signals
    wet_lab_score = sum(1 for k in ["qpcr", "western blot", "western blotting", "rna", "dna", "cell culture", "immunofluorescence"] if k in desc)
    comp_score = sum(1 for k in ["autodock", "vina", "molecular docking", "tcga", "bioinformatics", "survival analysis", "cancer"] if k in desc)

    if wet_lab_score >= 2 or comp_score >= 2:
        return 2
    elif wet_lab_score >= 1 or comp_score >= 1:
        return 1
    return 0

def run_benchmark(dataset_path: Path = None) -> Dict[str, Any]:
    dataset_file = dataset_path or (ROOT / "data" / "benchmark_dataset.json")
    if not dataset_file.exists():
        raise FileNotFoundError(f"Benchmark dataset not found at {dataset_file}")

    dataset = json.loads(dataset_file.read_text(encoding="utf-8"))

    tp = 0 # True Positive (Human >= 1 & Pred >= 1)
    fp = 0 # False Positive (Human == 0 & Pred >= 1)
    fn = 0 # False Negative (Human >= 1 & Pred == 0)
    tn = 0 # True Negative (Human == 0 & Pred == 0)

    exact_matches = 0
    results = []

    for item in dataset:
        human_label = item["human_label"]
        pred_label = evaluate_job_eligibility(item)

        # Exact multiclass match
        if human_label == pred_label:
            exact_matches += 1

        # Binary match (Eligible >= 1 vs Reject == 0)
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
    accuracy_3class = exact_matches / len(dataset) if dataset else 1.0

    report = {
        "dataset_size": len(dataset),
        "binary_metrics": {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1_score": round(f1, 4),
            "true_positives": tp,
            "false_positives": fp,
            "true_negatives": tn,
            "false_negatives": fn
        },
        "multiclass_accuracy_3class": round(accuracy_3class, 4),
        "detailed_results": results
    }
    return report

if __name__ == "__main__":
    report = run_benchmark()
    print("=== V2.0 BENCHMARK EVALUATION REPORT ===")
    print(f"Total Benchmark Jobs Evaluated: {report['dataset_size']}")
    print(f"Eligibility Precision:          {report['binary_metrics']['precision']*100:.1f}%")
    print(f"Eligibility Recall:             {report['binary_metrics']['recall']*100:.1f}%")
    print(f"Binary F1-Score:                {report['binary_metrics']['f1_score']*100:.1f}%")
    print(f"3-Class Exact Accuracy:         {report['multiclass_accuracy_3class']*100:.1f}%")
    print(f"False Positive Rate:            {report['binary_metrics']['false_positives']} / {report['dataset_size']}")
