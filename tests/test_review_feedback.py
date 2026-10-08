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


def test_turso_retry_uses_one_guarded_batch_for_update_and_audit(monkeypatch):
    from types import SimpleNamespace

    connection = object.__new__(db.TursoConnection)
    connection.execute = lambda _sql, _params=(): SimpleNamespace(fetchone=lambda: ("evaluated", "failed", None))
    connection.close = lambda: None
    batch_calls = []

    class _Statement:
        def __init__(self, sql, args):
            self.sql = sql
            self.args = args

    def batch(statements):
        batch_calls.append(statements)
        return [SimpleNamespace(rows_affected=1), SimpleNamespace(rows_affected=1)]

    connection.client = SimpleNamespace(batch=batch)
    monkeypatch.setattr(db, "get_connection", lambda: connection)
    monkeypatch.setattr(db, "libsql_client", SimpleNamespace(Statement=_Statement))

    request_retry(42)

    assert len(batch_calls) == 1
    assert len(batch_calls[0]) == 2
    assert "UPDATE jobs" in batch_calls[0][0].sql
    assert "WHERE changes()=1" in batch_calls[0][1].sql


def test_turso_retry_batch_failure_preserves_original_error(monkeypatch):
    from types import SimpleNamespace

    connection = object.__new__(db.TursoConnection)
    connection.execute = lambda _sql, _params=(): SimpleNamespace(fetchone=lambda: ("evaluated", "failed", None))
    connection.close = lambda: None

    def batch(_statements):
        raise RuntimeError("mocked batch failure")

    connection.client = SimpleNamespace(batch=batch)
    monkeypatch.setattr(db, "get_connection", lambda: connection)
    monkeypatch.setattr(db, "libsql_client", SimpleNamespace(Statement=lambda sql, args: (sql, args)))

    with pytest.raises(RuntimeError, match="mocked batch failure"):
        request_retry(42)


def test_turso_validation_value_error_is_not_masked_by_missing_rollback(monkeypatch):
    from types import SimpleNamespace

    connection = object.__new__(db.TursoConnection)
    connection.execute = lambda _sql, _params=(): SimpleNamespace(fetchone=lambda: ("applied", "success", None))
    connection.close = lambda: None
    connection.client = SimpleNamespace(batch=lambda _statements: pytest.fail("unexpected batch"))
    monkeypatch.setattr(db, "get_connection", lambda: connection)

    with pytest.raises(ValueError, match="değerlendirme kuyruğundaki"):
        request_retry(42)
