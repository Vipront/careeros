"""Read-only automated quality audit for CareerOS recommendations.

Audits saved recommendations against the shared recommendation quality gate
using current runtime profile facts. Supports Turso and read-only SQLite.
Never mutates the database.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.eligibility.gate import (
    GATE_RULE_VERSION,
    evaluate_job_eligibility,
)
from src.eligibility.models import (
    CriterionStatus,
    EligibilityDecision,
    LivenessStatusContract,
    OverallEligibilityStatus,
)
from src.eligibility.profile_adapter import (
    compute_profile_version,
    get_candidate_profile_facts,
)
from src.final_ranking import READY_THRESHOLD, calculate_final_score
from src.ops.liveness import LIVENESS_CLASSIFIER_VERSION

ROOT = Path(__file__).resolve().parents[2]
HISTORICAL_10_CAVEAT = (
    "The historical 10-job sample is a small convenience sample (baseline TP=1, FP=3, TN=6, FN=0). "
    "It was used during root-cause bug fixing for experience and language rules. "
    "Testing against this frozen sample confirms regression prevention, but does not constitute an "
    "independent blind holdout quality measure or prove production generalization. "
    "Automated system decisions are never treated as ground truth."
)


class QualityAuditDatabaseError(RuntimeError):
    """Raised when the database cannot be queried or lacks expected tables."""


def _parse_score(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (ValueError, TypeError):
        return None


def _extract_judge_result(raw_json: Any) -> tuple[bool, float | None, bool]:
    """Returns (is_match, match_score, is_valid). Strict bool required for is_match."""
    if raw_json is None:
        return False, None, False
    d = raw_json
    if isinstance(raw_json, str):
        try:
            d = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError):
            return False, None, False
    if not isinstance(d, dict):
        return False, None, False

    raw_is_match = d.get("is_match")
    if not isinstance(raw_is_match, bool):
        return False, None, False

    match_score = _parse_score(d.get("match_score"))
    if match_score is None or not 0 <= match_score <= 100 or "error" in d:
        return False, None, False
    return raw_is_match, match_score, True


def get_connection_for_audit(
    provider: str = "auto",
    db_path: Path | str | None = None,
) -> tuple[Any, str]:
    """Obtain a strictly read-only DB connection (SELECT only, never mutating)."""
    if db_path is not None:
        uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
        return sqlite3.connect(uri, uri=True), "SQLite"

    if provider == "sqlite":
        uri = f"{(ROOT / 'data' / 'jobs.db').resolve().as_uri()}?mode=ro"
        return sqlite3.connect(uri, uri=True), "SQLite"

    if provider in ("auto", "turso"):
        import src.db as db_mod

        turso_url = os.getenv("TURSO_DB_URL") or getattr(db_mod, "TURSO_URL", None)
        turso_token = os.getenv("TURSO_AUTH_TOKEN") or getattr(db_mod, "TURSO_TOKEN", None)
        if turso_url and turso_token:
            return db_mod.TursoConnection(turso_url, turso_token), "Turso"
        if provider == "turso":
            raise QualityAuditDatabaseError("Turso provider requested but TURSO_DB_URL or TURSO_AUTH_TOKEN not set")
        uri = f"{(ROOT / 'data' / 'jobs.db').resolve().as_uri()}?mode=ro"
        return sqlite3.connect(uri, uri=True), "SQLite"

    raise ValueError(f"Unknown database provider: {provider}")


def fetch_database_jobs(
    con: Any,
    limit: int | None = None,
    ids: list[str | int] | None = None,
) -> list[dict[str, Any]]:
    """Fetch jobs matching criteria using SELECT only. Never swallows DB errors."""
    try:
        cur = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'")
        tables = cur.fetchall()
    except Exception as exc:
        raise QualityAuditDatabaseError("Database table inspection failed") from exc

    if not tables:
        raise QualityAuditDatabaseError("Table 'jobs' does not exist in database")

    bounded_limit = max(1, min(int(limit or 100), 500))
    bounded_ids = ids[:500] if ids else None

    if bounded_ids:
        placeholders = ",".join("?" for _ in bounded_ids)
        sql = f"SELECT * FROM jobs WHERE id IN ({placeholders}) ORDER BY id DESC"
        sql += f" LIMIT {bounded_limit}"
        cur = con.execute(sql, [str(i) for i in bounded_ids])
    else:
        sql = (
            "SELECT * FROM jobs WHERE status IN "
            "('evaluated', 'ready_for_review', 'low_priority', 'normal', 'rejected') "
            "AND (llm_judge_result_json IS NOT NULL OR status='ready_for_review') "
            f"ORDER BY id DESC LIMIT {bounded_limit}"
        )
        cur = con.execute(sql)

    cols = [col[0] for col in cur.description] if cur.description else []
    return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def parse_frozen_ilanlar(text: str) -> dict[int, dict[str, Any]]:
    """Parse output/human-review-20261008/ilanlar.md into structured job dicts."""
    jobs: dict[int, dict[str, Any]] = {}
    pattern = re.compile(r"^##\s*(\d+)(?:\s*[\u2014\u2013\-]\s*(.*?))?$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    for i, m in enumerate(matches):
        job_id = int(m.group(1))
        title = (m.group(2) or "").strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end].strip()
        lines = [line.strip() for line in block.splitlines() if line.strip()]

        company, location, url = "", "", ""
        idx = 0
        if idx < len(lines) and "|" in lines[idx]:
            parts = lines[idx].split("|", 1)
            company, location = parts[0].strip(), parts[1].strip()
            idx += 1
        if idx < len(lines) and lines[idx].startswith("http"):
            url = lines[idx]
            idx += 1
        description = "\n".join(lines[idx:]).strip()
        jobs[job_id] = {
            "id": job_id,
            "title": title,
            "company": company,
            "location": location,
            "url": url,
            "description": description,
        }
    return jobs


def parse_sample_dir(sample_dir: Path | None) -> dict[str, Any]:
    """Parse frozen review sample artifacts if present."""
    if not sample_dir or not sample_dir.is_dir():
        return {"available": False, "reason": "Sample directory absent or not a directory"}

    ilanlar_file = sample_dir / "ilanlar.md"
    preds_file = sample_dir / "predictions.jsonl"
    labels_file = sample_dir / "labels.csv"

    if not ilanlar_file.is_file() or not preds_file.is_file():
        return {"available": False, "reason": "Required sample files missing"}

    jobs = parse_frozen_ilanlar(ilanlar_file.read_text(encoding="utf-8"))
    preds: dict[int, dict[str, Any]] = {}
    for line in preds_file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                row = json.loads(line)
                preds[int(row["job_id"])] = row
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                continue

    labels: dict[int, dict[str, Any]] = {}
    if labels_file.is_file():
        with labels_file.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                jid_str = r.get("job_id", "").strip()
                val_str = r.get("is_match", "").strip().lower()
                if jid_str.isdigit() and val_str in ("true", "false") and r.get("reason", "").strip():
                    labels[int(jid_str)] = {"is_match": val_str == "true", "reason": r.get("reason", "").strip()}

    return {"available": True, "jobs": jobs, "predictions": preds, "labels": labels}


def evaluate_job_record(
    job: Mapping[str, Any],
    candidate_facts: Mapping[str, Any],
    *,
    probe_live: bool = False,
) -> tuple[dict[str, Any], EligibilityDecision, bool]:
    """Evaluate candidate suitability, actionable recommendation, and gate decision."""
    gate_decision = evaluate_job_eligibility(job, candidate_facts, probe_live_if_missing=probe_live, con=None)

    is_match, match_score, is_valid_judge = _extract_judge_result(job.get("llm_judge_result_json"))
    if not is_valid_judge:
        fallback_score = _parse_score(job.get("llm_score"))
        match_score = fallback_score

    kw_score = _parse_score(job.get("keyword_score")) or 0.0
    sem_score = _parse_score(job.get("semantic_score")) or 0.0
    final_score = calculate_final_score(kw_score, sem_score, match_score or 0.0)

    old_recommended = bool(
        (is_match and final_score >= READY_THRESHOLD) or str(job.get("status")) == "ready_for_review"
    )

    all_criteria_met = (
        gate_decision.experience.status == CriterionStatus.MET
        and gate_decision.language.status == CriterionStatus.MET
        and gate_decision.posting_language_preference.status == CriterionStatus.MET
    )

    candidate_suitable = bool(all_criteria_met and is_match and final_score >= READY_THRESHOLD)
    actionable_recommended = bool(gate_decision.can_recommend and is_match and final_score >= READY_THRESHOLD)

    details = {
        "job_id": str(job.get("id")),
        "title": str(job.get("title") or ""),
        "company": str(job.get("company") or ""),
        "final_score": final_score,
        "is_match": is_match,
        "is_valid_judge": is_valid_judge,
        "old_recommended": old_recommended,
        "candidate_suitable": candidate_suitable,
        "actionable_recommended": actionable_recommended,
        "overall_status": gate_decision.overall_status.value,
        "can_recommend": gate_decision.can_recommend,
        "hard_block_reasons": list(gate_decision.hard_block_reasons),
        "review_reasons": list(gate_decision.review_reasons),
        "liveness_status": gate_decision.liveness.status.value,
        "liveness_reason": gate_decision.liveness.reason,
    }
    return details, gate_decision, is_valid_judge


def compute_confusion_matrix(pairs: list[tuple[bool, bool]]) -> dict[str, Any]:
    """Compute precision, recall, F1, accuracy for list of (predicted, actual) boolean pairs."""
    if not pairs:
        return {"tp": 0, "fp": 0, "tn": 0, "fn": 0, "precision": None, "recall": None, "f1": None, "accuracy": None}
    tp = sum(1 for p, a in pairs if p and a)
    fp = sum(1 for p, a in pairs if p and not a)
    tn = sum(1 for p, a in pairs if not p and not a)
    fn = sum(1 for p, a in pairs if not p and a)
    total = len(pairs)
    prec = tp / (tp + fp) if (tp + fp) > 0 else None
    rec = tp / (tp + fn) if (tp + fn) > 0 else None
    f1 = 2 * tp / (2 * tp + fp + fn) if (2 * tp + fp + fn) else None
    acc = (tp + tn) / total if total > 0 else None
    return {
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision": round(prec, 4) if prec is not None else None,
        "recall": round(rec, 4) if rec is not None else None,
        "f1": round(f1, 4) if f1 is not None else None,
        "accuracy": round(acc, 4) if acc is not None else None,
    }


def run_quality_audit(
    *,
    db_path: Path | str | None = None,
    provider: str = "auto",
    sample_dir: Path | str | None = None,
    limit: int | None = 100,
    ids: list[str | int] | None = None,
    probe_live: bool = False,
) -> dict[str, Any]:
    """Execute quality audit across current DB and optional historical frozen sample."""
    candidate = get_candidate_profile_facts()
    profile_ver = str(candidate.get("profile_version") or compute_profile_version(candidate))
    sample_path = Path(sample_dir) if sample_dir else ROOT / "output" / "human-review-20261008"

    bounded_ids = ids[:500] if ids else None
    bounded_limit = max(1, min(int(limit or 100), 500))

    con, resolved_provider = get_connection_for_audit(provider=provider, db_path=db_path)
    try:
        db_jobs = fetch_database_jobs(con, limit=bounded_limit, ids=bounded_ids)
    finally:
        if hasattr(con, "close"):
            con.close()

    eval_db: list[dict[str, Any]] = []
    blocked_counts = {"experience": 0, "language": 0, "posting_language": 0, "liveness": 0}
    review_counts = {"liveness_unknown": 0, "criteria_unknown": 0}
    transitions = {
        "old_pos_to_new_pos": 0,
        "old_pos_to_blocked": 0,
        "old_pos_to_review": 0,
        "old_neg_to_neg": 0,
        "old_neg_to_pos": 0,
    }
    invalid_judge_rows = 0

    for job in db_jobs:
        details, dec, is_valid_j = evaluate_job_record(job, candidate, probe_live=probe_live)
        if not is_valid_j:
            invalid_judge_rows += 1
        eval_db.append(details)

        for b in dec.hard_block_reasons:
            if b.startswith("Experience:"):
                blocked_counts["experience"] += 1
            elif b.startswith("Language:"):
                blocked_counts["language"] += 1
            elif b.startswith("Posting Language:"):
                blocked_counts["posting_language"] += 1
            elif b.startswith("Liveness:"):
                blocked_counts["liveness"] += 1

        if dec.liveness.status == LivenessStatusContract.UNKNOWN:
            review_counts["liveness_unknown"] += 1
        if any(r for r in dec.review_reasons if not r.startswith("Liveness:")):
            review_counts["criteria_unknown"] += 1

        old_p = details["old_recommended"]
        new_p = details["actionable_recommended"]
        if old_p and new_p:
            transitions["old_pos_to_new_pos"] += 1
        elif old_p and dec.overall_status == OverallEligibilityStatus.INELIGIBLE:
            transitions["old_pos_to_blocked"] += 1
        elif old_p and dec.overall_status == OverallEligibilityStatus.REVIEW:
            transitions["old_pos_to_review"] += 1
        elif not old_p and not new_p:
            transitions["old_neg_to_neg"] += 1
        elif not old_p and new_p:
            transitions["old_neg_to_pos"] += 1

    total_db = len(eval_db)
    review_rate = (
        round(sum(1 for d in eval_db if d["overall_status"] == "review") / total_db, 4) if total_db > 0 else 0.0
    )

    # Historical frozen sample replay
    sample_res = parse_sample_dir(sample_path)
    historical_replay: dict[str, Any] = {"available": False, "caveat": HISTORICAL_10_CAVEAT}

    if sample_res["available"]:
        h_jobs = sample_res["jobs"]
        h_preds = sample_res["predictions"]
        h_labels = sample_res["labels"]

        job_ids = set(h_jobs.keys())
        pred_ids = set(h_preds.keys())
        label_ids = set(h_labels.keys())
        common_ids = sorted(list(job_ids & pred_ids & label_ids))

        missing_ids = {
            "missing_predictions": sorted(list((job_ids | label_ids) - pred_ids)),
            "missing_labels": sorted(list((job_ids | pred_ids) - label_ids)),
            "missing_descriptions": sorted(list((pred_ids | label_ids) - job_ids)),
        }

        h_details_list: list[dict[str, Any]] = []
        old_pairs: list[tuple[bool, bool]] = []
        suit_pairs: list[tuple[bool, bool]] = []
        act_pos_count = 0

        for jid in common_ids:
            j_data = dict(h_jobs[jid])
            pred_meta = h_preds[jid]
            j_data["keyword_score"] = pred_meta.get("keyword_score")
            j_data["semantic_score"] = pred_meta.get("semantic_score")
            j_data["llm_judge_result_json"] = pred_meta.get("llm_judge_result_json")

            # Frozen replay tests suitability only. Current network state cannot
            # establish the openness at the historical prediction time.
            details, _, valid_prediction = evaluate_job_record(j_data, candidate, probe_live=False)
            if not valid_prediction or not j_data.get("description", "").strip():
                continue
            h_label = h_labels[jid]
            details["human_label"] = h_label

            actual = bool(h_label["is_match"])
            old_pairs.append((details["old_recommended"], actual))
            suit_pairs.append((details["candidate_suitable"], actual))
            if details["actionable_recommended"]:
                act_pos_count += 1

            h_details_list.append(details)

        historical_replay = {
            "available": True,
            "caveat": HISTORICAL_10_CAVEAT,
            "sample_jobs_count": len(common_ids),
            "labeled_jobs_count": len(old_pairs),
            "invalid_or_empty_records_count": len(common_ids) - len(old_pairs),
            "missing_ids": missing_ids,
            "actionable_recommendations": {
                "count": act_pos_count,
                "note": "Labels unavailable for current openness; actionable recommendations require verified live status.",
            },
            "confusion_matrices": {
                "historical_old_predictions_vs_human": compute_confusion_matrix(old_pairs),
                "candidate_suitability_vs_human": compute_confusion_matrix(suit_pairs),
            },
            "per_job": h_details_list,
        }

    return {
        "report_version": "1.0.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": resolved_provider,
        "evidence_versions": {
            "profile_version": profile_ver,
            "gate_rule_version": GATE_RULE_VERSION,
            "liveness_classifier_version": LIVENESS_CLASSIFIER_VERSION,
        },
        "audit_scope": {
            "selection": "latest judged jobs and ready rows; explicit IDs override this filter",
            "profile_scope": "current runtime profile; historical profile snapshot unavailable",
            "quality_scope": "automatic policy audit; fresh production precision/recall unmeasured",
            "limit": bounded_limit,
            "ids_filter": bounded_ids,
            "probe_live": probe_live,
            "sample_dir": str(sample_path) if sample_path.exists() else None,
        },
        "current_db_summary": {
            "total_sampled": total_db,
            "invalid_judge_rows_count": invalid_judge_rows,
            "old_positives": sum(1 for d in eval_db if d["old_recommended"]),
            "candidate_suitable": sum(1 for d in eval_db if d["candidate_suitable"]),
            "new_actionable_positives": sum(1 for d in eval_db if d["actionable_recommended"]),
            "review_rate": review_rate,
            "transitions": transitions,
            "blocked_reasons": blocked_counts,
            "review_reasons": review_counts,
        },
        "current_db_jobs": eval_db,
        "historical_replay": historical_replay,
    }
