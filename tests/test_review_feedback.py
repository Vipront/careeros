import pytest

from src import db
from src.review_feedback import load_feedback_labels, record_feedback, request_retry
from tests.db_helpers import isolated_database, seed_job


def test_feedback_preserves_status_and_latest_label_is_used():
    with isolated_database() as database:
        job_id = seed_job(database, fingerprint="feedback-1", title="Synthetic role", company="Synthetic Co", status="applied")
        record_feedback(job_id, False, "Experience requirement is too high")
        record_feedback(job_id, True, "Employer clarified experience is optional")
        assert load_feedback_labels() == [{"job_id": job_id, "is_match": True,
                                           "reason": "Employer clarified experience is optional"}]
        con = db.get_connection(path=database)
        try:
            assert con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == "applied"
            assert con.execute("SELECT COUNT(*) FROM events WHERE event_type='review_feedback'").fetchone()[0] == 2
        finally:
            con.close()


def test_feedback_requires_an_explicit_decision_and_reason():
    with pytest.raises(ValueError):
        record_feedback(1, "yes", "Reason")
    with pytest.raises(ValueError):
        record_feedback(1, True, " ")


def test_retry_preserves_applied_state_and_only_requeues_failed_evaluation():
    with isolated_database() as database:
        job_id = seed_job(database, fingerprint="retry-1", title="Synthetic role", company="Synthetic Co", status="evaluated")
        con = db.get_connection(path=database)
        con.execute("UPDATE jobs SET llm_judge_status='failed',llm_judge_terminal=1 WHERE id=?", (job_id,))
        con.commit()
        con.close()
        request_retry(job_id)
        con = db.get_connection(path=database)
        assert con.execute("SELECT status,llm_judge_status,llm_judge_terminal FROM jobs WHERE id=?", (job_id,)).fetchone() == (
            "evaluated", "not_attempted", 0)
        con.execute("UPDATE jobs SET status='applied' WHERE id=?", (job_id,))
        con.commit()
        con.close()
        with pytest.raises(ValueError):
            request_retry(job_id)
