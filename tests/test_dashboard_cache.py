"""Snapshot performance must preserve freshness and user transition correctness."""
import time

import pandas as pd
import pytest

from src.dashboard_cache import SnapshotStore, SnapshotUnavailable


def frame():
    return pd.DataFrame([{"id": 1, "title": "Synthetic", "status": "ready_for_review",
                          "can_recommend": False, "is_ready_recommendation": False}])


def test_warmed_snapshot_reads_without_remote_queries_and_is_private(tmp_path):
    path = tmp_path / "snapshot.json"
    original = SnapshotStore(path, "profile-a", frame, start=False)
    original.refresh()
    def forbidden_query():
        raise AssertionError("UI must not issue a remote query")
    loaded = SnapshotStore(path, "profile-a", forbidden_query, start=False)
    assert loaded.read()["id"].tolist() == [1]
    changed = loaded.read()
    changed.loc[0, "title"] = "Changed"
    assert loaded.read().iloc[0]["title"] == "Synthetic"


def test_profile_change_or_expired_snapshot_fails_closed(tmp_path):
    path = tmp_path / "snapshot.json"
    store = SnapshotStore(path, "profile-a", frame, start=False)
    store.refresh()
    with pytest.raises(SnapshotUnavailable):
        SnapshotStore(path, "profile-b", frame, start=False).read()
    store.generated_at = time.time() - 121
    with pytest.raises(SnapshotUnavailable):
        store.read()


def test_corrupt_snapshot_is_not_used(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text('{"broken":true}', encoding="utf-8")
    with pytest.raises(SnapshotUnavailable):
        SnapshotStore(path, "profile-a", frame, start=False).read()


def test_transition_during_refresh_wins_over_old_database_result(tmp_path):
    store = SnapshotStore(tmp_path / "snapshot.json", "profile-a", frame, start=False)
    store.refresh()
    def concurrent_fetch():
        store.patch_status(1, "applied")
        return frame()
    store.fetch = concurrent_fetch
    store.refresh()
    assert store.read().iloc[0]["status"] == "applied"
    assert not store.read().iloc[0]["is_ready_recommendation"]
    assert SnapshotStore(store.path, "profile-a", frame, start=False).read().iloc[0]["status"] == "applied"


def test_manual_refresh_invalidates_instead_of_returning_old_data(tmp_path):
    store = SnapshotStore(tmp_path / "snapshot.json", "profile-a", frame, start=False)
    store.refresh()
    store.invalidate()
    assert store.wakeup.is_set()
    with pytest.raises(SnapshotUnavailable):
        store.read()


def test_source_age_does_not_trigger_gate_work_on_snapshot_read(tmp_path, monkeypatch):
    from src.eligibility import gate
    data = frame()
    data["can_recommend"] = True
    data["liveness_checked_at"] = "2026-01-01"
    data["eligibility_status"] = "eligible"
    data["eligibility_decision_json"] = "{}"
    def unexpected_evaluation(*args, **kwargs):
        raise AssertionError("Source age must not block interactive snapshot reads")
    monkeypatch.setattr(gate, "evaluate_job_eligibility", unexpected_evaluation)
    store = SnapshotStore(tmp_path / "snapshot.json", "profile-a", lambda: data, start=False)
    store.refresh()
    result = store.read().iloc[0]
    assert result["can_recommend"]
    assert result["eligibility_status"] == "eligible"
