import os
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

try:
    import libsql_client
except ImportError:
    libsql_client = None

DB_PATH = ROOT / "data" / "jobs.db"

# Turso DB Configuration
TURSO_URL = os.getenv("TURSO_DB_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")


def get_connection_provider() -> str:
    """Return the configured database provider without exposing credentials."""
    url = os.getenv("TURSO_DB_URL") or TURSO_URL
    token = os.getenv("TURSO_AUTH_TOKEN") or TURSO_TOKEN
    return "Turso" if url and token else "SQLite"

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

class TursoCursor:
    """Wrapper to mimic sqlite3 Cursor behavior."""
    def __init__(self, result_set):
        self._res = result_set
        self.description = [(col, None, None, None, None, None, None) for col in result_set.columns] if result_set else []

    def __iter__(self):
        if not self._res or not self._res.rows:
            return iter(())
        return iter(self._res.rows)

    def fetchall(self):
        if not self._res:
            return []
        return self._res.rows

    def fetchone(self):
        if not self._res or not self._res.rows:
            return None
        return self._res.rows[0]

    @property
    def rowcount(self):
        return self._res.rows_affected if self._res else 0

    @property
    def lastrowid(self):
        return getattr(self._res, "last_insert_rowid", None)

class TursoConnection:
    """Wrapper to mimic sqlite3 Connection behavior using libsql-client sync API."""
    def __init__(self, url, token):
        self.client = libsql_client.create_client_sync(url, auth_token=token)
        self.row_factory = None

    def cursor(self):
        return self

    def execute(self, sql, parameters=()):
        if isinstance(parameters, (tuple, list)):
            args: Any = list(parameters)
        elif isinstance(parameters, dict):
            args = parameters
        else:
            args = parameters

        try:
            rs = self.client.execute(sql, args)
            return TursoCursor(rs)
        except Exception as e:
            raise e

    def executemany(self, sql, seq_of_parameters):
        stmts = [libsql_client.Statement(sql, list(p)) for p in seq_of_parameters]
        if stmts:
            self.client.batch(stmts)

    def executescript(self, sql_script):
        stmts = [s.strip() for s in sql_script.split(";") if s.strip()]
        if stmts:
            self.client.batch([libsql_client.Statement(s, []) for s in stmts])

    def commit(self):
        pass

    def close(self):
        if hasattr(self.client, "close"):
            self.client.close()


_SQLITE_SCHEMA_CHECKED = False
_TURSO_SCHEMA_CHECKED = False


def ensure_v2_schema(con: Any) -> bool:
    """Ensure v2 columns exist and report whether the jobs schema was validated."""
    global _SQLITE_SCHEMA_CHECKED, _TURSO_SCHEMA_CHECKED
    if isinstance(con, TursoConnection):
        if _TURSO_SCHEMA_CHECKED:
            return True

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

    if isinstance(con, TursoConnection):
        _TURSO_SCHEMA_CHECKED = True
    return True


def get_connection(path=None) -> Any:
    global _SQLITE_SCHEMA_CHECKED
    # Always reload in case .env was populated recently
    url = os.getenv("TURSO_DB_URL") or TURSO_URL
    token = os.getenv("TURSO_AUTH_TOKEN") or TURSO_TOKEN

    if path is None and get_connection_provider() == "Turso":
        turso_con = TursoConnection(url, token)
        try:
            ensure_v2_schema(turso_con)
            return turso_con
        except Exception:
            turso_con.close()
            raise
    else:
        import sqlite3

        sqlite_con = sqlite3.connect(path or DB_PATH, timeout=30.0)
        sqlite_con.execute("PRAGMA foreign_keys=ON")
        sqlite_con.execute("PRAGMA journal_mode=WAL")
        if path is not None or not _SQLITE_SCHEMA_CHECKED:
            try:
                schema_validated = ensure_v2_schema(sqlite_con)
                if path is None and schema_validated:
                    _SQLITE_SCHEMA_CHECKED = True
            except Exception:
                sqlite_con.close()
                raise
        return sqlite_con

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

        if isinstance(con, TursoConnection):
            stmt1 = libsql_client.Statement(sql_update, params_update)
            conditional_event = sql_event.replace(
                "VALUES(?,?,?,?)",
                "SELECT ?,?,?,? WHERE changes() = 1 AND EXISTS "
                "(SELECT 1 FROM jobs WHERE id=? AND status=?)",
            )
            stmt2 = libsql_client.Statement(conditional_event, params_event + [job_id, new_status])
            batch_results = con.client.batch([stmt1, stmt2])
            if isinstance(batch_results, list) and batch_results and batch_results[0].rows_affected != 1:
                raise ValueError(f"Job {job_id} changed status concurrently; reload before retrying")
        else:
            result = con.execute(sql_update, params_update)
            if result.rowcount != 1:
                raise ValueError(f"Job {job_id} changed status concurrently; reload before retrying")
            con.execute(sql_event, params_event)
            con.commit()
    except Exception:
        if not isinstance(con, TursoConnection):
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
        if not isinstance(con, TursoConnection):
            con.rollback()
        raise
    finally:
        if owns_connection:
            con.close()
