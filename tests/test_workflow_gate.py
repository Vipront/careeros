"""Regression checks for audited system transitions and application readiness."""

import json
import pytest

from src import db, final_ranking, telegram_bot
from tests.db_helpers import isolated_database, seed_job
from tests.test_document_acceptance import _write_package


def test_system_transition_cannot_regress_an_applied_job():
    with isolated_database() as database:
        job_id = seed_job(database, fingerprint="submitted", title="Scientist", company="Lab", status="applied")
        with pytest.raises(ValueError, match="Invalid transition"):
            db.transition(job_id, "ready_for_review", system=True)
        con = db.get_connection()
        try:
            assert con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == "applied"
            assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        finally:
            con.close()


def test_transition_rolls_back_status_when_event_write_fails():
    with isolated_database() as database:
        job_id = seed_job(database, fingerprint="audit", title="Scientist", company="Lab", status="evaluated")
        con = db.get_connection()
        try:
            con.execute("CREATE TRIGGER reject_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'audit failed'); END")
            con.commit()
            with pytest.raises(Exception, match="audit failed"):
                db.transition(job_id, "low_priority", connection=con, system=True)
            assert con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == "evaluated"
        finally:
            con.close()


def test_ranking_requires_an_accepted_document_package(tmp_path):
    with isolated_database() as database:
        job_id = seed_job(database, fingerprint="rank", title="Scientist", company="Lab", status="evaluated")
        con = db.get_connection()
        try:
            con.execute("""UPDATE jobs SET profile_type='Bioinformatics', llm_judge_status='success',
                llm_judge_result_json=?, llm_score=88,
                description='Bioinformatics research analyst role in English. Requires Python knowledge. 0-1 years experience.',
                liveness_status='ACTIVE', liveness_http_code=200,
                liveness_detail='liveness-v2:positive-posting - Page accessible and application open',
                liveness_checked_at=datetime('now')
                WHERE id=?""", (json.dumps({"is_match": True, "match_score": 88}), job_id))
            con.commit()
            final_ranking.run()
            assert con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == "evaluated"
            _write_package(tmp_path)
            con.execute("UPDATE jobs SET application_materials_path=?, pipeline_status='COMPLETED' WHERE id=?", (str(tmp_path), job_id))
            con.commit()
            final_ranking.run()
            assert con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == "ready_for_review"
            assert con.execute("SELECT COUNT(*) FROM events WHERE event_type='status_change'").fetchone()[0] == 1
        finally:
            con.close()


def test_retention_reconciliation_preserves_submitted_jobs(tmp_path):
    with isolated_database() as database:
        con = db.get_connection()
        try:
            for status in ("ready_for_review", "applied"):
                job_id = seed_job(database, fingerprint=status, title="Scientist", company="Lab", status=status)
                folder = tmp_path / status
                con.execute("UPDATE jobs SET application_materials_path=? WHERE id=?", (str(folder), job_id))
                con.execute("""INSERT INTO applications(job_id,cv_path,cover_letter_path,created_at,updated_at)
                    VALUES(?,?,?,datetime('now'),datetime('now'))""", (job_id, str(folder / "tailored_cv.docx"), str(folder / "cover_letter.docx")))
                con.commit()
            assert db.reconcile_application_paths(tmp_path, connection=con) == 2
            assert dict(con.execute("SELECT fingerprint,status FROM jobs")) == {"ready_for_review": "evaluated", "applied": "applied"}
            assert all(cv is None and cover is None for cv, cover in con.execute("SELECT cv_path,cover_letter_path FROM applications"))
        finally:
            con.close()


@pytest.mark.parametrize("configured,operator,user,chat,expected", [
    (None, None, 123, 123, False),
    ("123", None, 123, 123, True),
    ("123", None, 456, 123, False),
    ("123", None, 123, 456, False),
    ("-100", None, 123, -100, False),
    ("-100", "123", 123, -100, True),
    ("-100", "123", 456, -100, False),
])
def test_telegram_requires_the_operator_and_configured_chat(monkeypatch, configured, operator, user, chat, expected):
    monkeypatch.setattr(telegram_bot, "CHAT_ID", configured)
    if operator:
        monkeypatch.setenv("TELEGRAM_OPERATOR_ID", operator)
    else:
        monkeypatch.delenv("TELEGRAM_OPERATOR_ID", raising=False)
    assert telegram_bot.is_authorized_user(user, chat) is expected
