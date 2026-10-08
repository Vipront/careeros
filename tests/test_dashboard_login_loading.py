"""Login must not wait for analytics unrelated to the requested workspace."""
from pathlib import Path

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from src import dashboard_data, db


def test_all_detail_panels_render_without_errors():
    app = AppTest.from_string('''
import streamlit as st
from src.dashboard_workspace import render_detail
render_detail({"id": 1, "title": "Synthetic", "status": "evaluated", "final_score": 85,
               "description": "Synthetic description", "eligibility_decision_json": "{}"},
              {"update_job_status": lambda *a: None, "get_job_timeline_events": lambda job_id: [],
               "get_job_assets_index": lambda: {}})
''')
    for panel in ("evaluation", "description", "documents", "feedback", "history"):
        app.session_state[f"detail_{panel}_1"] = True
    app.run(timeout=10)
    assert not app.exception
    assert any(m.value == "Synthetic description" for m in app.markdown)


def test_feedback_submission_displays_saved_note_after_rerun(monkeypatch):
    import json
    from src import dashboard_workspace
    events = []

    def save(job_id, is_match, reason):
        events.append(("review_feedback", "2026-10-08 21:00", json.dumps({"is_match": is_match, "reason": reason})))

    monkeypatch.setattr(dashboard_workspace, "record_feedback", save)
    app = AppTest.from_string('''
import streamlit as st
from src.dashboard_workspace import render_detail
render_detail({"id": 1, "title": "Synthetic", "status": "evaluated", "final_score": 85},
              {"update_job_status": lambda *a: None, "get_job_timeline_events": lambda job_id: st.session_state.events})
''')
    app.session_state["events"] = events
    app.session_state["detail_feedback_1"] = True
    app.run(timeout=10)
    assert not app.exception
    app.text_area[0].set_value("Synthetic saved feedback")
    next(b for b in app.button if b.label == "Geri bildirimi kaydet").click().run()
    assert not app.exception
    assert len(events) == 1
    assert any(s.value == "Kayıtlı geri bildirimin: Uygun" for s in app.success)
    assert any(m.value == "Synthetic saved feedback" for m in app.markdown)


@pytest.mark.parametrize("status", ["evaluated", "ready_for_review", "low_priority"])
def test_manual_application_can_be_recorded_while_checks_are_pending(monkeypatch, status):
    from src import dashboard_cache
    monkeypatch.setenv("DASHBOARD_PASSWORD", "synthetic-login-regression")
    rows = pd.DataFrame([{"id": 1, "title": "Synthetic role", "company": "Synthetic",
                          "status": status, "eligibility_status": "review", "final_score": 85,
                          "created_at": "2026-10-01", "location": "EU", "requirements_text": "PCR",
                          "eligibility_decision_json": "{}", "can_recommend": False,
                          "pipeline_status": "FAILED", "is_ready_recommendation": False}])
    calls = []
    monkeypatch.setattr(dashboard_cache, "get_dashboard_jobs", lambda *a, **k: rows.copy())
    monkeypatch.setattr(dashboard_cache, "patch_cached_status", lambda *a: None)
    def transition(job_id, target, **kwargs):
        calls.append((job_id, target))
        rows.loc[rows["id"] == job_id, "status"] = target
    monkeypatch.setattr(db, "transition", transition)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "src/dashboard.py"))
    app.session_state["authenticated"] = True
    app.session_state["app_workspace_mode"] = "İlan Değerlendirme"
    app.session_state["workbench_status_filter"] = "İnceleme"
    app.session_state["triage_filter_status"] = "Arşiv / Reddedilenler"
    app.run(timeout=10)
    assert not app.exception
    button = next(b for b in app.button if b.key == "btn_act_apply_1")
    assert not button.disabled
    button.click().run()
    assert not app.exception
    assert calls == [(1, "applied")]
    assert not any("within a callback" in warning.value for warning in app.warning)
    assert app.session_state["selected_job_id"] is None
    assert not any(b.key == "btn_act_apply_1" for b in app.button)
    next(r for r in app.radio if r.key == "desk_view").set_value("Başvurulanlar").run()
    assert not app.exception
    assert app.session_state["selected_job_id"] == 1
    assert any(b.key == "btn_act_interview_1" for b in app.button)


@pytest.mark.parametrize("status", ["evaluated", "ready_for_review", "low_priority"])
def test_manual_application_transition_is_saved_with_audit_event(status):
    import sqlite3
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, status TEXT, updated_at TEXT)")
        connection.execute("CREATE TABLE events (job_id INTEGER, event_type TEXT, event_time TEXT, note TEXT)")
        connection.execute("INSERT INTO jobs (id, status) VALUES (1, ?)", (status,))
        db.transition(1, "applied", connection=connection)
        assert connection.execute("SELECT status FROM jobs").fetchone()[0] == "applied"
        assert connection.execute("SELECT note FROM events").fetchone()[0] == f"{status} -> applied"


@pytest.mark.parametrize("mode", ["İlan Değerlendirme", "Belgelerim"])
def test_authenticated_workspace_does_not_query_analytics(monkeypatch, mode):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "synthetic-login-regression")
    calls = []

    def unexpected_query(*args, **kwargs):
        calls.append("database")
        raise AssertionError("Unrelated analytics must not block login")

    monkeypatch.setattr(db, "get_connection", unexpected_query)
    monkeypatch.setattr(dashboard_data, "fetch_jobs", lambda *args, **kwargs: pd.DataFrame(columns=dashboard_data.JOB_COLUMNS))
    st.cache_data.clear()
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "src/dashboard.py"))
    app.session_state["authenticated"] = True
    app.session_state["app_workspace_mode"] = mode
    app.run(timeout=10)
    assert not app.exception
    assert not calls
    assert any(button.key == "nav_jobs" for button in app.button)
    st.cache_data.clear()


def test_large_workspace_pages_cards_but_searches_all_jobs(monkeypatch):
    from src import dashboard_cache
    monkeypatch.setenv("DASHBOARD_PASSWORD", "synthetic-login-regression")
    rows = pd.DataFrame([{"id": number, "title": f"Role {number}", "company": "Synthetic",
                          "location": "EU", "requirements_text": "PCR", "status": "evaluated",
                          "final_score": 100-number/100, "created_at": "2026-10-01",
                          "eligibility_status": "review", "eligibility_decision_json": "{}",
                          "can_recommend": False, "is_ready_recommendation": False}
                         for number in range(1, 85)])
    monkeypatch.setattr(dashboard_cache, "get_dashboard_jobs", lambda *args, **kwargs: rows.copy())
    def forbidden_query(*args, **kwargs):
        raise AssertionError("Closed details must not query the database")
    monkeypatch.setattr(db, "get_connection", forbidden_query)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "src/dashboard.py"))
    app.session_state["authenticated"] = True
    app.session_state["app_workspace_mode"] = "İlan Değerlendirme"
    app.run(timeout=10)
    assert not app.exception
    assert len([b for b in app.button if str(b.key).startswith("sel_m_")]) == 30
    assert app.session_state["selected_job_id"] == 1
    next(box for box in app.selectbox if box.key == "workbench_page").set_value(2).run()
    assert not app.exception
    assert app.session_state["selected_job_id"] == 31
    next(box for box in app.text_input if box.key == "triage_search_query").set_value("Role 84").run()
    assert not app.exception
    assert app.session_state["selected_job_id"] == 84
    assert len([b for b in app.button if str(b.key).startswith("sel_m_")]) == 1


def test_first_authenticated_visit_opens_today(monkeypatch):
    from src import dashboard_cache
    monkeypatch.setenv("DASHBOARD_PASSWORD", "synthetic-login-regression")
    monkeypatch.setattr(dashboard_cache, "get_dashboard_jobs", lambda *a, **k: pd.DataFrame(columns=dashboard_data.JOB_COLUMNS))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "src/dashboard.py"))
    app.session_state["authenticated"] = True
    app.run(timeout=10)
    assert not app.exception
    assert app.session_state["app_workspace_mode"] == "Genel Bakış"
    assert any("Bugünün odağı" in m.value for m in app.markdown)
