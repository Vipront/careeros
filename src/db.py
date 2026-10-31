import sqlite3
from typing import Any
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
except ImportError:
    pass

DB_PATH = ROOT / "data" / "jobs.db"


def get_connection_provider() -> str:
    return "SQLite"


ALLOWED = {
    "new": {"evaluated", "rejected"},
    "evaluated": {"ready_for_review", "low_priority", "rejected", "withdrawn", "applied"},
    "low_priority": {"ready_for_review", "rejected", "withdrawn", "applied"},
    "ready_for_review": {"applied", "rejected", "withdrawn"},
    "applied": {"interview", "rejected", "withdrawn", "ready_for_review"},
    "interview": {"offer", "rejected", "withdrawn", "applied"},
    "offer": {"interview", "rejected", "withdrawn"},
    "rejected": set(),
    "withdrawn": set(),
}

# Automated reevaluation is limited to jobs that have not entered an application process.
SYSTEM_ALLOWED = {
    status: {"evaluated", "ready_for_review", "low_priority", "rejected"}
    for status in ("evaluated", "ready_for_review", "low_priority", "normal")
}
SYSTEM_ALLOWED["new"] = {"evaluated", "rejected"}

REJECTION_REASONS = (
    "Lokasyon",
    "Dil",
    "Deneyim / Teknik Uyum",
    "Eğitim / Derece",
    "Çalışma İzni / Sponsorluk",
    "Maaş / Koşullar",
    "Şirket / Kişisel Tercih",
    "Diğer",
)

def utc_now():
    """Canonical UTC timestamp generator. All modules should import this."""
    return datetime.now(timezone.utc).isoformat()

def now():
    return utc_now()

def ensure_v2_schema(con: Any) -> bool:
    """Ensure v2 columns exist and report whether the jobs schema was validated."""
    try:
        rows = con.execute("PRAGMA table_info(jobs)").fetchall()
    except Exception as exc:
        raise RuntimeError("Could not inspect jobs schema") from exc

    # Check if table has any columns (i.e. table exists)
    if not rows:
        return False

    columns = {r[1] for r in rows}

    # Add missing v2 columns if they don't exist
    new_cols = [
        ("raw_html", "TEXT"),
        ("workplace_type", "TEXT"),
        ("employment_type", "TEXT"),
        ("experience_level", "TEXT"),
        ("salary_raw", "TEXT"),
        ("skills_detected", "TEXT"),
        ("is_easy_apply", "INTEGER DEFAULT 0"),
        ("job_fingerprint", "TEXT"),
        ("last_evaluated", "TEXT"),
        ("review_priority", "TEXT"),
        ("review_notes", "TEXT"),
        ("metrics_json", "TEXT"),
        ("application_materials_path", "TEXT"),
        ("llm_model", "TEXT"),
        ("version_metadata", "TEXT"),
        ("retry_count", "INTEGER DEFAULT 0"),
        ("last_error", "TEXT"),
        ("pipeline_status", "TEXT DEFAULT 'COMPLETED'"),
        ("liveness_status", "TEXT"),
        ("liveness_checked_at", "TEXT"),
        ("liveness_http_code", "INTEGER"),
        ("liveness_detail", "TEXT"),
    ]

    for col_name, col_type in new_cols:
        if col_name not in columns:
            try:
                con.execute(f"ALTER TABLE jobs ADD COLUMN {col_name} {col_type}")
                if hasattr(con, "commit"):
                    con.commit()
            except Exception as exc:
                # A concurrent initializer may have won the same ALTER. Accept
                # that only when a fresh schema read proves the column exists.
                try:
                    current = {r[1] for r in con.execute("PRAGMA table_info(jobs)").fetchall()}
                except Exception:
                    current = set()
                if col_name not in current:
                    raise RuntimeError(f"Could not add required jobs column: {col_name}") from exc
                columns = current

    try:
        validated_columns = {r[1] for r in con.execute("PRAGMA table_info(jobs)").fetchall()}
    except Exception as exc:
        raise RuntimeError("Could not validate jobs schema after migration") from exc
    missing_columns = {name for name, _ in new_cols} - validated_columns
    if missing_columns:
        raise RuntimeError("Jobs schema migration incomplete; missing columns: " + ", ".join(sorted(missing_columns)))

    # Ensure index exists for fast UI queries
    try:
        con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status_final_score ON jobs(status, final_score)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_events_job_id ON events(job_id, id)")
        if hasattr(con, "commit"):
            con.commit()
    except Exception:  # noqa: S110
        pass

    return True


_SQLITE_SCHEMA_CHECKED = False


def get_connection(path=None) -> sqlite3.Connection:
    global _SQLITE_SCHEMA_CHECKED
    con = sqlite3.connect(path or DB_PATH, timeout=30.0)
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA journal_mode=WAL")
        if path is not None or not _SQLITE_SCHEMA_CHECKED:
            validated = ensure_v2_schema(con)
            if path is None and validated:
                _SQLITE_SCHEMA_CHECKED = True
        return con
    except Exception:
        con.close()
        raise


def transition(job_id, new_status, note=None, allow_unarchive=False, *, connection=None, system=False):
    """Apply a validated status change and its audit event on the same connection.

    A supplied connection remains open. System reevaluation cannot move submitted
    applications or terminal jobs back into the processing queue.
    """
    owns_connection = connection is None
    con = get_connection() if owns_connection else connection
    try:
        row = con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError(f"Job {job_id} not found")

        old_status = row[0]

        if system and old_status == new_status:
            return

        is_unarchive = allow_unarchive and old_status in ("rejected", "withdrawn") and new_status == "ready_for_review"
        allowed = SYSTEM_ALLOWED.get(old_status, set()) if system else ALLOWED.get(old_status, set())
        if not is_unarchive and new_status not in allowed:
            raise ValueError(f"Invalid transition: {old_status} -> {new_status}")

        stamp = utc_now()
        sql_update = "UPDATE jobs SET status=?, updated_at=? WHERE id=? AND status=?"
        params_update = [new_status, stamp, job_id, old_status]

        note_text = f"{old_status} -> {new_status}" + (f": {note}" if note else "")
        sql_event = "INSERT INTO events(job_id,event_type,event_time,note) VALUES(?,?,?,?)"
        params_event = [job_id, "status_change", stamp, note_text]

        result = con.execute(sql_update, params_update)
        if result.rowcount != 1:
            raise ValueError(f"Job {job_id} changed status concurrently; reload before retrying")
        con.execute(sql_event, params_event)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        if owns_connection:
            con.close()


def reconcile_application_paths(output_root, connection=None):
    """Clear missing local artifacts after retention without deleting job history."""
    from src.ops.artifact_qa import application_package_error

    root = Path(output_root).resolve()
    owns_connection = connection is None
    con = get_connection() if owns_connection else connection
    changed = 0

    def local_path(value):
        if not value:
            return None
        path = Path(value)
        if not path.is_absolute():
            path = ROOT / path
        path = path.resolve()
        return path if path.is_relative_to(root) else None

    try:
        rows = con.execute("""
            SELECT j.id, j.status, j.application_materials_path,
                   a.cv_path, a.cover_letter_path, a.application_prep_path
            FROM jobs j LEFT JOIN applications a ON a.job_id=j.id
            WHERE j.application_materials_path IS NOT NULL
               OR a.cv_path IS NOT NULL OR a.cover_letter_path IS NOT NULL
               OR a.application_prep_path IS NOT NULL
        """).fetchall()
        for job_id, status, folder_value, *artifacts in rows:
            folder = local_path(folder_value)
            missing = False
            for column, value in zip(("cv_path", "cover_letter_path", "application_prep_path"), artifacts, strict=True):
                artifact = local_path(value)
                if artifact is not None and not artifact.is_file():
                    con.execute(f"UPDATE applications SET {column}=NULL, updated_at=? WHERE job_id=?", (utc_now(), job_id))
                    missing = True
            if folder is not None and application_package_error(folder) is not None:
                missing = True
                if not folder.is_dir():
                    con.execute("UPDATE jobs SET application_materials_path=NULL WHERE id=?", (job_id,))
            if missing:
                if status == "ready_for_review":
                    transition(job_id, "evaluated", note="Application files missing after retention", connection=con, system=True)
                con.execute("""
                    UPDATE jobs SET pipeline_status='FAILED', last_error=?, updated_at=? WHERE id=?
                """, ("Application package must be regenerated: local files are missing", utc_now(), job_id))
                changed += 1
        con.commit()
        return changed
    except Exception:
        con.rollback()
        raise
    finally:
        if owns_connection:
            con.close()
