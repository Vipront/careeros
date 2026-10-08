import json
from datetime import datetime, timezone

import pytest

from scripts import ops_guard


def test_pipeline_health_rejects_missing_stale_and_failed_reports(tmp_path):
    now = datetime.now(timezone.utc)
    assert ops_guard.pipeline_issues(tmp_path, now.timestamp()) == ["pipeline:missing_report"]
    path = tmp_path / "pipeline_20261008.json"
    path.write_text(json.dumps({"finished_at": now.isoformat(), "status": "failed"}))
    assert ops_guard.pipeline_issues(tmp_path, now.timestamp()) == ["pipeline:failed"]
    path.write_text(json.dumps({"finished_at": now.isoformat(), "status": "passed"}))
    assert ops_guard.pipeline_issues(tmp_path, now.timestamp()) == []
    assert ops_guard.pipeline_issues(tmp_path, now.timestamp() + 27 * 3600) == ["pipeline:stale_report"]


def test_notifications_only_on_change_and_recovery(tmp_path, monkeypatch):
    issues = ["backup:missing_or_stale"]
    notices = []
    monkeypatch.setattr(ops_guard, "collect_issues", lambda *_: list(issues))
    monkeypatch.setattr(ops_guard, "send_notice", notices.append)
    assert ops_guard.run(tmp_path) == 1
    assert ops_guard.run(tmp_path) == 1
    assert len(notices) == 1
    issues.clear()
    assert ops_guard.run(tmp_path) == 0
    assert len(notices) == 2
    assert ops_guard.run(tmp_path) == 0
    assert len(notices) == 2


def test_failed_notification_is_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(ops_guard, "collect_issues", lambda *_: ["service:careeros.service"])
    monkeypatch.setattr(ops_guard, "send_notice", lambda *_: (_ for _ in ()).throw(RuntimeError("offline")))
    with pytest.raises(RuntimeError):
        ops_guard.run(tmp_path)
    assert not (tmp_path / "data/runtime/ops-guard.json").exists()
