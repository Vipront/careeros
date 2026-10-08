"""Regression checks for real filters and authoritative evidence in Sage B."""
import json

import pandas as pd

from src.dashboard_workspace import decision_evidence, desk_jobs, escape, filter_jobs, verdict


def sample():
    return pd.DataFrame([
        {"id": 1, "title": "Python (junior)", "company": "Alpha", "location": "EU", "requirements_text": "Python", "final_score": 98,
         "created_at": "2026-10-01", "eligibility_status": "ineligible", "status": "ready_for_review", "is_ready_recommendation": False},
        {"id": 2, "title": "Research assistant", "company": "Beta", "location": "EU", "requirements_text": "PCR", "final_score": 78,
         "created_at": "2026-10-02", "eligibility_status": "eligible", "status": "applied", "is_ready_recommendation": False},
        {"id": 3, "title": "Research trainee", "company": "Gamma", "location": "EU", "requirements_text": "PCR", "final_score": 90,
         "created_at": "2026-10-03", "eligibility_status": "review", "status": "evaluated", "is_ready_recommendation": False},
    ])


def test_high_score_cannot_turn_blocked_into_suitable():
    rows = sample()
    assert filter_jobs(rows, group="Uygun")["id"].tolist() == [2]
    assert filter_jobs(rows, group="Engellendi")["id"].tolist() == [1]
    assert verdict(rows.iloc[0])[:2] == ("blocked", "Engellendi")


def test_search_is_literal_and_application_view_is_real_subset():
    rows = sample()
    assert filter_jobs(rows, query="Python (")["id"].tolist() == [1]
    assert filter_jobs(rows, applications_only=True)["id"].tolist() == [2]
    assert filter_jobs(rows, group="İnceleme")["id"].tolist() == [3]


def test_application_process_is_excluded_from_review_queue():
    rows = pd.concat([sample().iloc[[2]].assign(id=number, status=status)
                      for number, status in enumerate(["evaluated", "applied", "interview", "offer"], 1)])
    assert filter_jobs(rows, group="İnceleme")["id"].tolist() == [1]
    assert set(filter_jobs(rows, applications_only=True)["id"]) == {2, 3, 4}


def test_simple_desk_separates_applications_and_recoverable_archive():
    rows = pd.concat([sample(), sample().iloc[[0]].assign(id=4, status="rejected")])
    assert set(desk_jobs(rows)["id"]) == {1, 3}
    assert desk_jobs(rows, view="Başvurulanlar")["id"].tolist() == [2]
    assert desk_jobs(rows, view="Arşiv")["id"].tolist() == [4]
    assert desk_jobs(rows, query="Research trainee")["id"].tolist() == [3]


def test_review_queue_uses_pending_eligibility_and_supports_score_and_sort():
    rows = sample()
    rows.loc[0, ["eligibility_status", "is_ready_recommendation"]] = ["eligible", True]
    rows = pd.concat([rows, rows.iloc[[0]].assign(id=4, status="applied", is_ready_recommendation=True)])
    assert desk_jobs(rows, view="İncelenecek")["id"].tolist() == [3]
    assert desk_jobs(rows, high_match=True)["id"].tolist() == [1, 3]
    assert desk_jobs(rows, sort="Tarih (En Yeni)")["id"].tolist() == [3, 1]


def test_review_queue_respects_shared_score_threshold():
    from src.final_ranking import READY_THRESHOLD
    rows = pd.concat([sample().iloc[[2]].assign(id=index, final_score=score)
                      for index, score in enumerate([58, READY_THRESHOLD - 0.01, READY_THRESHOLD, 75, None], 1)])
    assert desk_jobs(rows, view="İncelenecek")["id"].tolist() == [4, 3]
    assert set(desk_jobs(rows)["id"]) == {1, 2, 3, 4, 5}


def test_absent_or_malformed_evidence_is_never_reported_as_verified():
    for raw in (None, "broken", "[]", "{}"):
        evidence = decision_evidence({"eligibility_decision_json": raw, "final_score": 99})
        assert len(evidence) == 4
        assert {item["kind"] for item in evidence} == {"pending"}


def test_recorded_closed_source_and_language_barrier_remain_visible():
    raw = json.dumps({"language": {"status": "unmet", "reason": "German C1 required"},
                      "liveness": {"status": "closed", "reason": "HTTP 410"}})
    evidence = decision_evidence({"eligibility_decision_json": raw})
    assert evidence[1]["kind"] == "blocked"
    assert evidence[1]["reason"] == "German C1 required"
    assert evidence[3]["kind"] == "blocked"
    assert evidence[3]["reason"] == "HTTP 410"


def test_untrusted_card_text_is_escaped():
    assert escape('<img src=x onerror="alert(1)">') == '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;'
