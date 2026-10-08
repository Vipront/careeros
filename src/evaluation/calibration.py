"""Descriptive outcome reporting from application status history.

The available application outcomes can describe conversion rates, but they do
not establish model accuracy or justify changing matching weights.
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.db import get_connection


_SUBMITTED_STATUSES = {"applied", "interview", "offer"}
_PRE_APPLICATION_STATUSES = {"new", "evaluated", "low_priority", "ready_for_review", "normal"}
_STATUS_TRANSITION = re.compile(r"^([a-z_]+)\s*->\s*([a-z_]+)$")


def _empty_report(status: str, *, error: str | None = None) -> dict[str, Any]:
    """Build a stable report shape when outcome data cannot be summarized."""
    return {
        "total_historical_jobs": None,
        "total_submitted_applications": None,
        "total_interviews_secured": None,
        "interview_conversion_rate": None,
        "total_rejected_applications": None,
        "total_postsubmission_rejections": None,
        "total_preapplication_rejections": None,
        "total_unknown_legacy_rejections": None,
        "total_withdrawn_applications": None,
        "model_error_status": status,
        "recommended_weights": None,
        "calibration_insight": (
            "Sonuç verisi okunamadığı için dönüşüm raporu oluşturulamadı."
            if status == "DATA_UNAVAILABLE"
            else "Doğrulanmış gönderim bulunmadığı için dönüşüm oranı hesaplanamadı."
        ),
        "data_error": error,
    }


def _parse_status_transition(note: str) -> tuple[str, str] | None:
    """Read the ``old_status -> new_status`` prefix written by ``db.transition``."""
    transition_text = note.split(":", maxsplit=1)[0].strip()
    match = _STATUS_TRANSITION.fullmatch(transition_text)
    return (match.group(1), match.group(2)) if match else None


def compute_outcome_calibration() -> dict[str, Any]:
    """Return descriptive application outcomes without claiming calibration.

    A submission is confirmed by a non-empty ``applications.applied_at``, a
    current applied/interview/offer status, or status history entering/leaving
    one of those submitted states. A rejected job is classified as a
    pre-application rejection only when its recorded transition starts in a
    known pre-application state. Rejected rows without enough evidence remain
    unknown legacy data.
    """
    con = None
    try:
        con = get_connection()
        jobs = con.execute(
            """
            SELECT j.id, j.status, a.applied_at
            FROM jobs AS j
            LEFT JOIN applications AS a ON a.job_id = j.id
            """
        ).fetchall()
        events = con.execute(
            "SELECT job_id, note FROM events WHERE event_type='status_change' ORDER BY id ASC"
        ).fetchall()
    except Exception as exc:
        return _empty_report("DATA_UNAVAILABLE", error=str(exc))
    finally:
        if con is not None:
            con.close()

    events_by_job: dict[int, list[tuple[str, str]]] = {}
    for job_id, note in events:
        transition = _parse_status_transition(note or "")
        if transition is not None:
            events_by_job.setdefault(job_id, []).append(transition)

    submitted_count = 0
    interview_count = 0
    historical_job_count = 0
    postsubmission_rejections = 0
    preapplication_rejections = 0
    unknown_legacy_rejections = 0
    withdrawn_count = 0

    for job_id, status, applied_at in jobs:
        history = events_by_job.get(job_id, [])
        submitted_evidence = bool(applied_at) or status in _SUBMITTED_STATUSES or any(
            old_status in _SUBMITTED_STATUSES or new_status in _SUBMITTED_STATUSES
            for old_status, new_status in history
        )
        interview_evidence = status in {"interview", "offer"} or any(
            old_status in {"interview", "offer"} or new_status in {"interview", "offer"}
            for old_status, new_status in history
        )
        if status in _SUBMITTED_STATUSES | {"rejected", "withdrawn"} or submitted_evidence:
            historical_job_count += 1

        if submitted_evidence:
            submitted_count += 1
            if status == "withdrawn":
                withdrawn_count += 1
        if interview_evidence:
            interview_count += 1

        if status == "rejected":
            if submitted_evidence:
                postsubmission_rejections += 1
            else:
                rejection_transitions = [
                    (old_status, new_status)
                    for old_status, new_status in history
                    if new_status == "rejected"
                ]
                last_rejection = rejection_transitions[-1] if rejection_transitions else None
                if last_rejection and last_rejection[0] in _PRE_APPLICATION_STATUSES:
                    preapplication_rejections += 1
                else:
                    unknown_legacy_rejections += 1

    rate = interview_count / submitted_count * 100 if submitted_count else None
    status_label = "DESCRIPTIVE_ONLY" if submitted_count else "INSUFFICIENT_DATA"
    report = {
        "total_historical_jobs": historical_job_count,
        "total_submitted_applications": submitted_count,
        "total_interviews_secured": interview_count,
        "interview_conversion_rate": round(rate, 2) if rate is not None else None,
        "total_rejected_applications": postsubmission_rejections,
        "total_postsubmission_rejections": postsubmission_rejections,
        "total_preapplication_rejections": preapplication_rejections,
        "total_unknown_legacy_rejections": unknown_legacy_rejections,
        "total_withdrawn_applications": withdrawn_count,
        "model_error_status": status_label,
        "recommended_weights": None,
        "calibration_insight": (
            f"Betimsel sonuç raporu: {submitted_count} doğrulanmış gönderim, "
            f"{interview_count} mülakat. Bu veriler tek başına model doğruluğunu "
            "kanıtlamaz veya önerilen ağırlık üretmez."
            if submitted_count
            else "Doğrulanmış gönderim bulunmadığı için dönüşüm oranı hesaplanamadı."
        ),
        "data_error": None,
    }
    return report


if __name__ == "__main__":
    print("Outcome Report:")
    print(json.dumps(compute_outcome_calibration(), indent=2, ensure_ascii=False))
