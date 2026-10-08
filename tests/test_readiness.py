import sqlite3

from src.ops.readiness import check_readiness


def test_readiness_missing_database_is_not_created(tmp_path):
    assert check_readiness(tmp_path)["ready"] is False
    assert not (tmp_path / "data" / "jobs.db").exists()


def test_readiness_does_not_write_rows(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    database = data / "jobs.db"
    con = sqlite3.connect(database)
    con.executescript("""CREATE TABLE jobs(id,status,llm_judge_status,pipeline_status);
        CREATE TABLE events(job_id,event_type,event_time,note);
        CREATE TABLE applications(job_id,applied_at);""")
    con.close()
    for filename in ("semantic_profiles.json", "master_cv.json", "master_cv.md"):
        (data / filename).write_text("{}", encoding="utf-8")
    before = database.read_bytes()
    assert check_readiness(tmp_path)["ready"] is True
    assert database.read_bytes() == before
