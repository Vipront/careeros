import json
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

import run_daily


def _queue_db(path):
    con = sqlite3.connect(path)
    con.execute("""
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY, status TEXT, description_available INTEGER,
            semantic_score REAL, profile_type TEXT, llm_judge_status TEXT,
            llm_judge_terminal INTEGER, llm_judge_next_retry_at TEXT,
            match_score REAL, enrichment_status TEXT,
            enrichment_terminal INTEGER, enrichment_next_attempt_at TEXT
        )
    """)
    con.commit()
    con.close()


def test_queue_inspection_failure_is_explicit_and_connection_closes(monkeypatch):
    connection = Mock()
    connection.execute.side_effect = sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(run_daily, "_open_queue_connection", lambda: connection)

    with pytest.raises(run_daily.QueueInspectionError, match="pending pipeline queue"):
        run_daily.get_pending_count()

    connection.close.assert_called_once()


def test_semantic_and_llm_dispatch_use_their_production_eligibility_predicates(monkeypatch, tmp_path):
    db_path = tmp_path / "jobs.sqlite"
    _queue_db(db_path)
    monkeypatch.setattr(run_daily, "_open_queue_connection", lambda: sqlite3.connect(db_path))
    con = sqlite3.connect(db_path)
    con.execute("INSERT INTO jobs VALUES (1,'new',1,NULL,NULL,NULL,0,NULL,90,'not_attempted',0,NULL)")
    con.execute("INSERT INTO jobs VALUES (2,'evaluated',1,NULL,NULL,NULL,0,NULL,80,'not_attempted',0,NULL)")
    con.commit()
    con.close()

    assert run_daily.stage_has_work("semantic") is True
    assert run_daily.stage_has_work("llm") is False

    con = sqlite3.connect(db_path)
    con.execute("UPDATE jobs SET semantic_score=0.6,profile_type='Bioinformatics' WHERE id=2")
    con.commit()
    con.close()
    assert run_daily.stage_has_work("llm") is True


def test_only_known_empty_stages_skip_and_run_report_records_durations(monkeypatch, tmp_path):
    reports = tmp_path / "run.json"
    seen = []
    monkeypatch.setattr(run_daily, "acquire_lock", lambda: object())
    monkeypatch.setattr(run_daily, "release_lock", lambda _lock: None)
    monkeypatch.setattr(run_daily, "STEPS_CRAWL", [])
    monkeypatch.setattr(run_daily, "STEPS_PROCESS", [
        ("enrichment", ["unused"]),
        ("semantic", ["unused"]),
        ("llm", ["unused"]),
    ])
    monkeypatch.setattr(run_daily, "STEPS_FINAL", [])
    monkeypatch.setattr(run_daily, "stage_has_work", lambda _stage: False)
    monkeypatch.setattr(run_daily, "get_pending_count", lambda: 0)
    monkeypatch.setattr(run_daily, "run_step", lambda name, _command, _failures: seen.append(name))
    monkeypatch.setattr(run_daily, "cleanup_old_outputs", lambda **_kwargs: 0)

    assert run_daily.run(report_path=reports) == 0

    assert seen == ["enrichment"]
    report = json.loads(reports.read_text(encoding="utf-8"))
    events = {event["name"]: event for event in report["stages"]}
    assert events["enrichment"]["result"] == "passed"
    assert events["semantic"]["result"] == "skipped"
    assert events["llm"]["result"] == "skipped"
    assert all("duration_seconds" in event for event in report["stages"])


def test_queue_failure_stops_loop_returns_failure_and_writes_report(monkeypatch, tmp_path):
    report_path = tmp_path / "queue-failure.json"
    monkeypatch.setattr(run_daily, "acquire_lock", lambda: object())
    monkeypatch.setattr(run_daily, "release_lock", lambda _lock: None)
    monkeypatch.setattr(run_daily, "STEPS_CRAWL", [])
    monkeypatch.setattr(run_daily, "STEPS_PROCESS", [])
    monkeypatch.setattr(run_daily, "STEPS_FINAL", [])
    monkeypatch.setattr(run_daily, "get_pending_count", Mock(side_effect=run_daily.QueueInspectionError("DB unavailable")))

    assert run_daily.run(report_path=report_path) == 1

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert "queue_inspection" in report["failures"]
    assert any(event.get("result") == "failed" for event in report["stages"])


def test_lock_busy_exit_code_remains_two(monkeypatch, tmp_path):
    monkeypatch.setattr(run_daily, "acquire_lock", lambda: None)

    assert run_daily.run(report_path=tmp_path / "unused.json") == 2


def test_pending_count_matches_enrichment_retry_and_llm_profile_eligibility(monkeypatch, tmp_path):
    db_path = tmp_path / "jobs.sqlite"
    _queue_db(db_path)
    monkeypatch.setattr(run_daily, "_open_queue_connection", lambda: sqlite3.connect(db_path))
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO jobs VALUES (1,'evaluated',NULL,NULL,NULL,NULL,0,NULL,90,'not_found',0,'2000-01-01T00:00:00+00:00')"
    )
    con.execute(
        "INSERT INTO jobs VALUES (2,'new',0,NULL,NULL,NULL,0,NULL,80,'not_found',0,'2999-01-01T00:00:00+00:00')"
    )
    con.execute(
        "INSERT INTO jobs VALUES (3,'new',0,NULL,NULL,NULL,0,NULL,70,'not_found',1,'2000-01-01T00:00:00+00:00')"
    )
    con.execute(
        "INSERT INTO jobs VALUES (4,'evaluated',1,0.8,'Other','not_attempted',0,NULL,60,NULL,0,NULL)"
    )
    con.commit()
    con.close()

    assert run_daily.get_pending_count() == 1

    con = sqlite3.connect(db_path)
    con.execute("UPDATE jobs SET profile_type='Bioinformatics' WHERE id=4")
    con.commit()
    con.close()
    assert run_daily.get_pending_count() == 2


def test_noncritical_failure_has_noncontradictory_run_report_and_console_label(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "noncritical.json"
    monkeypatch.setattr(run_daily, "acquire_lock", lambda: object())
    monkeypatch.setattr(run_daily, "release_lock", lambda _lock: None)
    monkeypatch.setattr(run_daily, "STEPS_CRAWL", [])
    monkeypatch.setattr(run_daily, "STEPS_PROCESS", [("enrichment", ["unused"])])
    monkeypatch.setattr(run_daily, "STEPS_FINAL", [])
    monkeypatch.setattr(run_daily, "stage_has_work", lambda _stage: False)
    monkeypatch.setattr(run_daily, "get_pending_count", lambda: 0)

    def failed_document_step(name, _command, failures):
        failures.append(name)

    monkeypatch.setattr(run_daily, "run_step", failed_document_step)
    monkeypatch.setattr(run_daily, "cleanup_old_outputs", lambda **_kwargs: 0)

    assert run_daily.run(report_path=report_path) == 0

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "passed_with_noncritical_failures"
    assert report["exit_code"] == 0
    assert "PASS WITH NONCRITICAL FAILURES" in capsys.readouterr().out


@pytest.mark.parametrize("stage", ["documents", "telegram"])
def test_delivery_failure_fails_run_even_when_processing_queue_is_empty(monkeypatch, tmp_path, stage):
    report_path = tmp_path / "delivery-failure.json"
    monkeypatch.setattr(run_daily, "acquire_lock", lambda: object())
    monkeypatch.setattr(run_daily, "release_lock", lambda _lock: None)
    monkeypatch.setattr(run_daily, "STEPS_CRAWL", [])
    monkeypatch.setattr(run_daily, "STEPS_PROCESS", [(stage, ["unused"])])
    monkeypatch.setattr(run_daily, "STEPS_FINAL", [])
    monkeypatch.setattr(run_daily, "get_pending_count", lambda: 0)
    monkeypatch.setattr(run_daily, "run_step", lambda name, _command, failures: failures.append(name))
    cleanup = Mock()
    monkeypatch.setattr(run_daily, "cleanup_old_outputs", cleanup)

    assert run_daily.run(report_path=report_path) == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert stage in report["failures"]
    cleanup.assert_not_called()


def _stub_pipeline_for_backlog(monkeypatch, report_path, pending_counter):
    monkeypatch.setattr(run_daily, "acquire_lock", lambda: object())
    monkeypatch.setattr(run_daily, "release_lock", lambda _lock: None)
    monkeypatch.setattr(run_daily, "STEPS_CRAWL", [])
    monkeypatch.setattr(run_daily, "STEPS_PROCESS", [])
    monkeypatch.setattr(run_daily, "STEPS_FINAL", [])
    monkeypatch.setattr(run_daily, "get_pending_count", pending_counter)
    monkeypatch.setattr(run_daily, "cleanup_old_outputs", lambda **_kwargs: 0)
    monkeypatch.setattr(run_daily, "write_run_report", lambda report, _path: (report_path.write_text(json.dumps(report), encoding="utf-8"), report_path)[1])


def test_stalled_nonempty_backlog_is_reported_as_incomplete(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "stalled.json"
    _stub_pipeline_for_backlog(monkeypatch, report_path, lambda: 3)

    assert run_daily.run(report_path=report_path) == 3

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "incomplete"
    assert report["exit_code"] == 3
    assert report["remaining_pending_count"] == 3
    assert report["stop_reason"] == "stalled_backlog"
    assert "3 pending item(s) remain" in capsys.readouterr().out


def test_loop_limit_with_nonempty_backlog_is_reported_as_incomplete(monkeypatch, tmp_path):
    report_path = tmp_path / "loop-limit.json"
    observations = iter(range(1, 20))
    _stub_pipeline_for_backlog(monkeypatch, report_path, lambda: next(observations))

    assert run_daily.run(report_path=report_path) == 3

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "incomplete"
    assert report["remaining_pending_count"] == 6
    assert report["stop_reason"] == "loop_limit"
    assert len(report["pending_count_observations"]) == 6
