from pathlib import Path
from src.db import get_connection
ROOT=Path(__file__).resolve().parents[1]
def run(db_path=None):
    con = get_connection(path=db_path)
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    cols = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
    required = {"fingerprint", "created_at", "updated_at", "is_active", "status", "match_score", "requirements_text", "education_requirements", "experience_requirements", "eligibility_text", "deadline", "description_source", "description_available", "enrichment_status", "enriched_at"}
    assert required <= cols, f"Missing: {required - cols}"
    app_cols = {r[1] for r in con.execute("PRAGMA table_info(applications)")}
    assert "status" not in app_cols, "applications.status must not be a second workflow source"
    for table in ("jobs", "applications", "events"):
        assert con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    # Verify FK behavior on isolated temporary rows.
    con.execute("INSERT INTO jobs(fingerprint,title,date_found,created_at,updated_at) VALUES('verify-fp','verify',datetime('now'),datetime('now'),datetime('now'))")
    jid = con.execute("SELECT id FROM jobs WHERE fingerprint='verify-fp'").fetchone()[0]
    con.execute("INSERT INTO events(job_id,event_type,event_time) VALUES(?,?,datetime('now'))", (jid, "verify"))
    con.execute("INSERT INTO applications(job_id,created_at,updated_at) VALUES(?,?,?)", (jid, "now", "now"))
    con.commit()
    con.execute("DELETE FROM jobs WHERE id=?", (jid,))
    con.commit()
    assert con.execute("SELECT 1 FROM events WHERE job_id=?", (jid,)).fetchone() is None
    assert con.execute("SELECT 1 FROM applications WHERE job_id=?", (jid,)).fetchone() is None
    con.close()
    print("PASS: FK enforcement, cascade delete, schema, and single workflow source verified.")
    return 0

if __name__ == "__main__":
    run()
