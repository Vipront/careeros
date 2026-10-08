"""Read-only deployment readiness: imports, database schema and profile availability."""

import json
import os
from pathlib import Path
from typing import Any

from src import db


def check_readiness(root: Path | None = None) -> dict:
    target = root or db.ROOT
    con: Any = None
    try:
        import pydantic  # noqa: F401
        import src.ingestion.manual_ingestion  # noqa: F401
        import src.ops.liveness  # noqa: F401

        if root is None and db.get_connection_provider() == "Turso":
            con = db.TursoConnection(os.getenv("TURSO_DB_URL") or db.TURSO_URL,
                                     os.getenv("TURSO_AUTH_TOKEN") or db.TURSO_TOKEN)
        else:
            import sqlite3
            path = (target / "data" / "jobs.db").resolve()
            con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        # Read schema directly: db.get_connection intentionally performs migrations.
        con.execute("SELECT id,status,llm_judge_status,pipeline_status FROM jobs LIMIT 0")
        con.execute("SELECT job_id,event_type,event_time,note FROM events LIMIT 0")
        con.execute("SELECT job_id,applied_at FROM applications LIMIT 0")
        for filename in ("semantic_profiles.json", "master_cv.json", "master_cv.md"):
            path = target / "data" / filename
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError(f"Required runtime file missing: {filename}")
        return {"ready": True, "scope": "imports, read-only database schema, required runtime files"}
    except Exception as exc:
        # Do not echo remote error payloads, paths or credentials to deployment logs.
        return {"ready": False, "error_type": type(exc).__name__}
    finally:
        if con is not None:
            con.close()


if __name__ == "__main__":
    report = check_readiness()
    print(json.dumps(report))
    raise SystemExit(0 if report["ready"] else 1)
