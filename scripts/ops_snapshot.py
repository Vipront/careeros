"""Read-only operational inventory; never print credentials or job contents."""
import json
import sqlite3
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
con = sqlite3.connect((root / "data/jobs.db").as_uri() + "?mode=ro", uri=True)
def rows(sql):
    return con.execute(sql).fetchall()
provider = "SQLite"
try:
    tables = [r[0] for r in rows("SELECT name FROM sqlite_master WHERE type='table'")]
    report = {"provider": provider, "tables": tables}
    if "jobs" in tables:
        report["job_columns"] = [r[1] for r in rows("PRAGMA table_info(jobs)")]
        report["job_status_counts"] = [list(r) for r in rows("SELECT status,count(*) FROM jobs GROUP BY status")]
    if "events" in tables:
        report["human_feedback_events"] = rows("SELECT count(*) FROM events WHERE event_type='review_feedback'")[0][0]
    print(json.dumps(report))
finally:
    con.close()
