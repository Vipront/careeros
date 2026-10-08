"""Evaluate a completed human labels.csv against its frozen prediction sample."""
import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.evaluation.production_eval import evaluate_predictions  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("sample", type=Path)
parser.add_argument("--output", type=Path, default=None, help="Path to write evaluation report (default: sample/evaluation_reproduced.json)")
args = parser.parse_args()

labels = []
with (args.sample / "labels.csv").open(encoding="utf-8-sig", newline="") as handle:
    for row in csv.DictReader(handle):
        value = row["is_match"].strip().lower()
        if value not in {"true", "false"} or not row["reason"].strip():
            raise SystemExit(f"Complete human is_match=true/false and reason for job {row['job_id']}")
        labels.append({"job_id": row["job_id"], "is_match": value == "true", "reason": row["reason"].strip()})

predictions = [json.loads(line) for line in (args.sample / "predictions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
report = evaluate_predictions(predictions, labels)
report["sampling_scope"] = "small balanced convenience sample; not representative of production prevalence"
report["label_source"] = "user-completed human labels.csv"

# Historical closure notes in human labels vs current live status:
# Free-text comments from past reviews are NOT current closure evidence;
# they are historical human notes. Current liveness requires fresh timestamped probe.
historical_closure_notes = []
for lbl in labels:
    reason_lower = lbl["reason"].lower()
    if "closed" in reason_lower or "kapan" in reason_lower:
        historical_closure_notes.append({
            "job_id": lbl["job_id"],
            "historical_human_note": lbl["reason"],
            "note_time": "2026-10-08",
        })

report["liveness_breakdown"] = {
    "historical_human_closure_notes_count": len(historical_closure_notes),
    "historical_human_closure_notes": historical_closure_notes,
    "current_liveness_status": "unknown (requires fresh timestamped probe; not inferred from historical free-text)",
}

output_path = args.output if args.output is not None else (args.sample / "evaluation_reproduced.json")
output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))
