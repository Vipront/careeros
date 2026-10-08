from src.dashboard_data import explain_review, fetch_jobs
from tests.db_helpers import isolated_database, seed_job


def test_data_queries_preserve_visibility_and_metadata():
    with isolated_database() as database:
        active = seed_job(database, fingerprint="visible", title="Example role", company="Example", status="evaluated")
        rejected = seed_job(database, fingerprint="hidden", title="Other role", company="Example", status="rejected")
        assert fetch_jobs()["id"].tolist() == [active]
        assert set(fetch_jobs(True)["id"]) == {active, rejected}
        assert "deadline" in fetch_jobs().columns


def test_review_explanation_does_not_invent_reason_or_ignore_failure():
    result = explain_review({"status": "evaluated", "pipeline_status": "FAILED"})
    assert result["stage"] == "Belge hazırlığı başarısız"
    assert result["reason"] is None
    assert result["deadline"] is None


def test_recorded_reason_and_retry_are_visible():
    result = explain_review({"status": "evaluated", "description_available": True,
                             "semantic_score": 0.7, "llm_judge_status": "rate_limited",
                             "llm_judge_next_retry_at": "2026-10-09T00:00:00Z",
                             "llm_judge_result_json": '{"reason":"Relevant experience","uncertainties":["Visa eligibility"]}'})
    assert result["reason"] == "Relevant experience"
    assert result["uncertainties"] == ["Visa eligibility"]
    assert result["retry_at"] == "2026-10-09T00:00:00Z"


def test_ready_for_review_when_gate_says_review_is_not_called_ready_recommendation():
    # If job status is ready_for_review but gate says review / can_recommend is False:
    # explain_review must NOT say "Başvuru paketi hazır"
    res_review = explain_review({
        "status": "ready_for_review",
        "description_available": 1,
        "eligibility_status": "review",
        "can_recommend": False,
    })
    assert res_review["stage"] == "Uygunluk incelemesi bekliyor"
    assert "Başvuru paketi hazır" not in res_review["stage"]

    # When eligible and can_recommend is True:
    res_ready = explain_review({
        "status": "ready_for_review",
        "description_available": 1,
        "semantic_score": 0.8,
        "llm_judge_status": "success",
        "eligibility_status": "eligible",
        "can_recommend": True,
    })
    assert res_ready["stage"] == "Başvuru paketi hazır"
