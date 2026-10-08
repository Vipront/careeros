"""Shared recommendation quality gate for CareerOS.

Unifies eligibility, experience, language, posting language, and liveness into
a single authoritative decision.
High LLM scores cannot bypass hard eligibility barriers.
All entry and consumption paths (daily, manual, ranking, documents, dashboard, Telegram)
pass through this gate.
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from src.db import utc_now
from src.eligibility.experience import evaluate_experience_eligibility
from src.eligibility.language import (
    evaluate_language_eligibility,
    evaluate_posting_language_preference,
)
from src.eligibility.models import (
    CriterionStatus,
    EligibilityDecision,
    LivenessDecision,
    LivenessStatusContract,
    OverallEligibilityStatus,
)
from src.eligibility.profile_adapter import (
    compute_profile_version,
    get_candidate_profile_facts,
)
from src.ops.liveness import (
    CACHE_DURATION_HOURS,
    LIVENESS_CLASSIFIER_VERSION,
    LivenessStatus,
    check_liveness,
    is_liveness_stale,
)

GATE_RULE_VERSION = "1.2.0"


def ensure_eligibility_schema(con) -> None:
    """Additive schema migration: adds eligibility columns to jobs table safely."""
    columns = {row[1] for row in con.execute("PRAGMA table_info(jobs)").fetchall()}
    added = False
    if "eligibility_status" not in columns:
        con.execute("ALTER TABLE jobs ADD COLUMN eligibility_status TEXT")
        added = True
    if "eligibility_decision_json" not in columns:
        con.execute("ALTER TABLE jobs ADD COLUMN eligibility_decision_json TEXT")
        added = True
    if "eligibility_checked_at" not in columns:
        con.execute("ALTER TABLE jobs ADD COLUMN eligibility_checked_at TEXT")
        added = True
    if "eligibility_rule_version" not in columns:
        con.execute("ALTER TABLE jobs ADD COLUMN eligibility_rule_version TEXT")
        added = True
    if added and hasattr(con, "commit"):
        con.commit()


def evaluate_job_eligibility(
    job: Mapping[str, Any],
    candidate: Optional[Mapping[str, Any]] = None,
    *,
    probe_live_if_missing: bool = False,
    ttl_hours: float = CACHE_DURATION_HOURS,
    con: Any = None,
) -> EligibilityDecision:
    """Evaluate full recommendation eligibility for a job mapping."""
    if candidate is None:
        candidate = get_candidate_profile_facts()
    profile_ver = str(candidate.get("profile_version") or compute_profile_version(candidate))

    now_str = datetime.now(timezone.utc).isoformat()
    job_id = str(job.get("id", job.get("job_id", "0")))
    title = str(job.get("title") or "")
    description = str(job.get("description") or "")
    req_text = str(job.get("requirements_text") or "")
    exp_req = str(job.get("experience_requirements") or "")
    edu_req = str(job.get("education_requirements") or "")
    eligibility_req = str(job.get("eligibility_text") or "")
    url = str(job.get("url") or "")

    full_text = f"{title}\n{req_text}\n{exp_req}\n{edu_req}\n{eligibility_req}\n{description}".strip()

    # 1. Experience decision
    exp_decision = evaluate_experience_eligibility(
        title=title,
        description=description,
        experience_requirements=f"{req_text}\n{exp_req}\n{eligibility_req}".strip(),
        education_requirements=edu_req,
        candidate=candidate,
    )
    exp_decision.profile_version = profile_ver

    # 2. Language decision (working language)
    lang_decision = evaluate_language_eligibility(
        text=full_text,
        candidate=candidate,
    )
    lang_decision.profile_version = profile_ver

    # 3. Posting language preference
    pref_decision = evaluate_posting_language_preference(
        text=full_text,
        candidate=candidate,
    )
    pref_decision.profile_version = profile_ver

    # 4. Liveness decision
    liveness_status = str(job.get("liveness_status") or "").upper()
    liveness_checked_at = job.get("liveness_checked_at")
    http_code = job.get("liveness_http_code")
    liveness_detail = job.get("liveness_detail")
    stale = is_liveness_stale(liveness_checked_at, ttl_hours=ttl_hours)

    # Positive evidence validation: genuine positive cues requiring the exact new classifier version marker
    has_positive_evidence = bool(
        http_code == 200
        and liveness_detail
        and LIVENESS_CLASSIFIER_VERSION in str(liveness_detail)
    )

    if probe_live_if_missing and (not liveness_status or stale or (liveness_status == "ACTIVE" and not has_positive_evidence)) and url.startswith("http"):
        with contextlib.suppress(Exception):
            live_res = check_liveness(url)
            liveness_status = live_res.status.value
            http_code = live_res.http_status
            liveness_checked_at = now_str
            liveness_detail = live_res.detail or (
                f"{LIVENESS_CLASSIFIER_VERSION} - Page accessible and application open"
                if live_res.status == LivenessStatus.ACTIVE
                else live_res.status.value
            )
            stale = False
            has_positive_evidence = bool(
                live_res.status == LivenessStatus.ACTIVE
                and LIVENESS_CLASSIFIER_VERSION in str(liveness_detail)
            )
            if con and job_id and job_id != "0":
                with contextlib.suppress(Exception):
                    from src.db import ensure_v2_schema
                    ensure_v2_schema(con)
                    con.execute(
                        """
                        UPDATE jobs
                        SET liveness_status = ?,
                            liveness_checked_at = ?,
                            liveness_http_code = ?,
                            liveness_detail = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (liveness_status, liveness_checked_at, http_code, liveness_detail, now_str, job_id),
                    )
                    if hasattr(con, "commit"):
                        con.commit()

    if liveness_status in (LivenessStatus.CLOSED.value, "CLOSED", "NOT_FOUND", LivenessStatus.NOT_FOUND.value):
        liveness_contract = LivenessDecision(
            status=LivenessStatusContract.CLOSED,
            reason=f"Job posting is verified closed or removed (HTTP {http_code or '410/404'}).",
            evidence_spans=[str(liveness_detail or "Closed / not found")],
            checked_at=str(liveness_checked_at or now_str),
            http_code=http_code,
            rule_version=GATE_RULE_VERSION,
            is_stale=False,
        )
    elif liveness_status in (LivenessStatus.ACTIVE.value, "ACTIVE") and not stale and has_positive_evidence:
        liveness_contract = LivenessDecision(
            status=LivenessStatusContract.OPEN,
            reason="Job posting is verified reachable and open with positive application controls.",
            evidence_spans=[str(liveness_detail or "Active")],
            checked_at=str(liveness_checked_at or now_str),
            http_code=http_code,
            rule_version=GATE_RULE_VERSION,
            is_stale=False,
        )
    else:
        # Unknown, stale, temporary error, rate-limited, auth required, or old ACTIVE without positive evidence
        reason = "Job openness is unverified or evidence is stale."
        if stale and liveness_checked_at:
            reason = f"Liveness check is stale (checked {liveness_checked_at}; TTL {ttl_hours}h)."
        elif liveness_status == "ACTIVE" and not has_positive_evidence:
            reason = "Liveness was recorded as ACTIVE without verified positive posting evidence from current classifier."
        elif liveness_status:
            reason = f"Liveness check pending resolution: {liveness_status}."
        liveness_contract = LivenessDecision(
            status=LivenessStatusContract.UNKNOWN,
            reason=reason,
            evidence_spans=[str(liveness_detail or liveness_status or "Unchecked")],
            checked_at=str(liveness_checked_at or now_str),
            http_code=http_code,
            rule_version=GATE_RULE_VERSION,
            is_stale=stale,
        )

    # Synthesize overall eligibility
    hard_blocks: list[str] = []
    review_reasons: list[str] = []

    if exp_decision.status == CriterionStatus.UNMET:
        hard_blocks.append(f"Experience: {exp_decision.reason}")
    elif exp_decision.status == CriterionStatus.UNKNOWN:
        review_reasons.append(f"Experience: {exp_decision.reason}")

    if lang_decision.status == CriterionStatus.UNMET:
        hard_blocks.append(f"Language: {lang_decision.reason}")
    elif lang_decision.status == CriterionStatus.UNKNOWN:
        review_reasons.append(f"Language: {lang_decision.reason}")

    if pref_decision.status == CriterionStatus.UNMET:
        hard_blocks.append(f"Posting Language: {pref_decision.reason}")
    elif pref_decision.status == CriterionStatus.UNKNOWN:
        review_reasons.append(f"Posting Language: {pref_decision.reason}")

    if liveness_contract.status == LivenessStatusContract.CLOSED:
        hard_blocks.append(f"Liveness: {liveness_contract.reason}")

    if hard_blocks:
        overall = OverallEligibilityStatus.INELIGIBLE
        can_rec = False
        can_docs = False
        can_notif = False
    elif review_reasons:
        overall = OverallEligibilityStatus.REVIEW
        can_rec = False
        can_docs = False
        can_notif = False
    else:
        overall = OverallEligibilityStatus.ELIGIBLE
        can_rec = True
        can_docs = True
        can_notif = True

    return EligibilityDecision(
        job_id=job_id,
        overall_status=overall,
        can_recommend=can_rec,
        can_generate_documents=can_docs,
        can_notify=can_notif,
        experience=exp_decision,
        language=lang_decision,
        posting_language_preference=pref_decision,
        liveness=liveness_contract,
        hard_block_reasons=hard_blocks,
        review_reasons=review_reasons,
        evaluated_at=now_str,
        rule_version=GATE_RULE_VERSION,
        profile_version=profile_ver,
    )


def persist_eligibility_decision(con, job_id: int | str, decision: EligibilityDecision) -> None:
    """Save the decision to database."""
    ensure_eligibility_schema(con)
    decision_json = json.dumps(decision.to_dict(), ensure_ascii=False)
    now_ts = utc_now()
    con.execute(
        """
        UPDATE jobs
        SET eligibility_status = ?,
            eligibility_decision_json = ?,
            eligibility_checked_at = ?,
            eligibility_rule_version = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            decision.overall_status.value,
            decision_json,
            now_ts,
            decision.rule_version,
            now_ts,
            job_id,
        ),
    )
    if hasattr(con, "commit"):
        con.commit()
