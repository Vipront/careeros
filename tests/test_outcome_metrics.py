from unittest.mock import patch

from src import db
from src.evaluation import calibration
from tests.db_helpers import isolated_database, seed_job


def _job(database, fingerprint, status):
    return seed_job(
        database,
        fingerprint=fingerprint,
        title=f"Role {fingerprint}",
        company="Example",
        status=status,
    )


def test_outcome_report_uses_submission_and_status_history():
    with isolated_database() as database:
        interviewed = _job(database, "outcome-interviewed", "ready_for_review")
        db.transition(interviewed, "applied")
        db.transition(interviewed, "interview")
        db.transition(interviewed, "rejected")

        withdrawn = _job(database, "outcome-withdrawn", "ready_for_review")
        db.transition(withdrawn, "applied")
        db.transition(withdrawn, "withdrawn")

        reopened = _job(database, "outcome-reopened", "ready_for_review")
        db.transition(reopened, "applied")
        db.transition(reopened, "ready_for_review")

        current_offer = _job(database, "outcome-offer", "offer")

        preapplication_rejection = _job(database, "outcome-pre-rejection", "ready_for_review")
        db.transition(preapplication_rejection, "rejected")

        _job(database, "outcome-legacy-rejection", "rejected")

        applied_at_rejection = _job(database, "outcome-applied-at", "rejected")
        con = db.get_connection(path=database)
        try:
            con.execute(
                """INSERT INTO applications(job_id, applied_at, created_at, updated_at)
                   VALUES(?, ?, datetime('now'), datetime('now'))""",
                (applied_at_rejection, "2026-01-01T12:00:00+00:00"),
            )
            con.commit()
        finally:
            con.close()

        report = calibration.compute_outcome_calibration()

    assert report["total_historical_jobs"] == 7
    assert report["total_submitted_applications"] == 5
    assert report["total_interviews_secured"] == 2
    assert report["interview_conversion_rate"] == 40.0
    assert report["total_rejected_applications"] == 2
    assert report["total_postsubmission_rejections"] == 2
    assert report["total_preapplication_rejections"] == 1
    assert report["total_unknown_legacy_rejections"] == 1
    assert report["total_withdrawn_applications"] == 1
    assert report["model_error_status"] == "DESCRIPTIVE_ONLY"
    assert report["recommended_weights"] is None


def test_no_confirmed_submissions_is_insufficient_data_with_null_rate():
    with isolated_database() as database:
        rejected = _job(database, "outcome-no-submissions", "ready_for_review")
        db.transition(rejected, "rejected")

        report = calibration.compute_outcome_calibration()

    assert report["total_submitted_applications"] == 0
    assert report["total_interviews_secured"] == 0
    assert report["total_rejected_applications"] == 0
    assert report["interview_conversion_rate"] is None
    assert report["model_error_status"] == "INSUFFICIENT_DATA"
    assert report["recommended_weights"] is None
    assert report["total_preapplication_rejections"] == 1


def test_database_failure_is_reported_as_unavailable():
    with patch.object(calibration, "get_connection", side_effect=RuntimeError("database unavailable")):
        report = calibration.compute_outcome_calibration()

    assert report["model_error_status"] == "DATA_UNAVAILABLE"
    assert report["interview_conversion_rate"] is None
    assert report["recommended_weights"] is None
