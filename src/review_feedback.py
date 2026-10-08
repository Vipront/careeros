"""Human review feedback kept separate from production decisions."""

import json

from src import db
from src.db import now


def record_feedback(job_id: int, is_match: bool, reason: str) -> None:
    if type(is_match) is not bool:
        raise ValueError("Feedback must contain a boolean decision")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
        raise ValueError("A review reason of 1–4000 characters is required")
    con = db.get_connection()
    try:
        row = con.execute("SELECT llm_judge_result_json FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("Job not found")
        payload = {"is_match": is_match, "reason": reason.strip(), "source": "human_review",
                   "prediction_snapshot": row[0]}
        con.execute("INSERT INTO events(job_id,event_type,event_time,note) VALUES(?,?,?,?)",
                    (job_id, "review_feedback", now(), json.dumps(payload, ensure_ascii=False)))
        con.commit()
    except Exception:
        if not isinstance(con, db.TursoConnection):
            con.rollback()
        raise
    finally:
        con.close()


def load_feedback_labels() -> list[dict]:
    """Latest explicit human labels; exporting to a dataset remains opt-in."""
    con = db.get_connection()
    try:
        rows = con.execute("SELECT job_id,note FROM events WHERE event_type='review_feedback' ORDER BY id").fetchall()
    finally:
        con.close()
    labels = {}
    for job_id, note in rows:
        payload = json.loads(note)
        if payload.get("source") == "human_review":
            labels[job_id] = {"job_id": job_id, "is_match": payload["is_match"], "reason": payload["reason"]}
    return list(labels.values())


def request_retry(job_id: int) -> None:
    """Queue a failed evaluated job without running providers or changing its workflow state."""
    con = db.get_connection()
    try:
        row = con.execute("SELECT status,llm_judge_status,pipeline_status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or row[0] != "evaluated":
            raise ValueError("Yalnız değerlendirme kuyruğundaki ilanlar yeniden denenebilir.")
        if row[1] in {"failed", "rate_limited"}:
            update_sql = """UPDATE jobs SET llm_judge_status='not_attempted',llm_judge_attempts=0,
                llm_judge_terminal=0,llm_judge_next_retry_at=NULL,updated_at=?
                WHERE id=? AND status='evaluated' AND llm_judge_status IN ('failed','rate_limited')"""
            update_params = [now(), job_id]
        elif row[2] == "FAILED":
            update_sql = """UPDATE jobs SET pipeline_status='PENDING',updated_at=?
                WHERE id=? AND status='evaluated' AND pipeline_status='FAILED'"""
            update_params = [now(), job_id]
        else:
            raise ValueError("Bu ilan için yeniden denenebilecek hata bulunamadı.")
        stamp = now()
        event_note = json.dumps({
            "previous_llm_status": row[1],
            "previous_pipeline_status": row[2],
        })
        event_sql = "INSERT INTO events(job_id,event_type,event_time,note) VALUES(?,?,?,?)"
        event_params = [job_id, "retry_requested", stamp, event_note]

        if isinstance(con, db.TursoConnection):
            guarded_event_sql = "INSERT INTO events(job_id,event_type,event_time,note) SELECT ?,?,?,? WHERE changes()=1"
            statements = [
                db.libsql_client.Statement(update_sql, update_params),
                db.libsql_client.Statement(guarded_event_sql, event_params),
            ]
            results = con.client.batch(statements)
            if not isinstance(results, (list, tuple)) or len(results) < 2:
                raise RuntimeError("Retry transaction did not return both statement results.")
            if getattr(results[0], "rows_affected", None) != 1:
                raise ValueError("İlan değişti; yeniden denemeden önce sayfayı yenile.")
            if getattr(results[1], "rows_affected", None) != 1:
                raise RuntimeError("Retry audit event was not confirmed by the transaction.")
        else:
            result = con.execute(update_sql, update_params)
            if result.rowcount != 1:
                raise ValueError("İlan değişti; yeniden denemeden önce sayfayı yenile.")
            con.execute(event_sql, event_params)
            con.commit()
    except Exception:
        if not isinstance(con, db.TursoConnection):
            con.rollback()
        raise
    finally:
        con.close()
