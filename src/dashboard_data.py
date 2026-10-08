"""Dashboard queries and review explanations without Streamlit dependencies."""

import json

import pandas as pd

from src import db


JOB_COLUMNS = (
    "id", "title", "company", "location", "url", "status", "review_priority", "final_score",
    "semantic_score", "llm_score", "description", "requirements_text", "education_requirements",
    "experience_requirements", "eligibility_text", "knockout_reason", "created_at", "updated_at",
    "is_easy_apply", "workplace_type", "employment_type", "liveness_status", "liveness_checked_at",
    "liveness_http_code", "liveness_detail", "pipeline_status", "last_error", "review_notes", "deadline",
    "description_available", "llm_judge_status", "llm_judge_next_retry_at", "llm_judge_result_json",
    "eligibility_status", "eligibility_decision_json", "eligibility_checked_at",
)


def fetch_jobs(include_rejected: bool = False) -> pd.DataFrame:
    from src.eligibility.gate import evaluate_job_eligibility
    from src.eligibility.profile_adapter import get_candidate_profile_facts

    con = db.get_connection()
    try:
        table_cols = {row[1] for row in con.execute("PRAGMA table_info(jobs)").fetchall()}
        cols = [c for c in JOB_COLUMNS if c in table_cols]
        condition = "" if include_rejected else (
            "WHERE status IN ('evaluated','ready_for_review','low_priority','applied','interview','offer')"
        )
        cursor = con.execute(f"SELECT {','.join(cols)} FROM jobs {condition} ORDER BY final_score DESC,id DESC")
        raw_rows = cursor.fetchall()
    finally:
        con.close()

    if not raw_rows:
        return pd.DataFrame(columns=cols)

    candidate = get_candidate_profile_facts()
    processed_records = []
    for r in raw_rows:
        row_dict = dict(zip(cols, r, strict=False))
        gate_decision = evaluate_job_eligibility(row_dict, candidate)
        # Expose current decision, barriers and recommendation readiness
        row_dict["eligibility_status"] = gate_decision.overall_status.value
        row_dict["eligibility_decision_json"] = json.dumps(gate_decision.to_dict(), ensure_ascii=False)
        row_dict["eligibility_barrier"] = "; ".join(gate_decision.hard_block_reasons)
        row_dict["eligibility_reasons"] = "; ".join(gate_decision.review_reasons)
        row_dict["can_recommend"] = gate_decision.can_recommend
        row_dict["can_generate_documents"] = gate_decision.can_generate_documents
        row_dict["can_notify"] = gate_decision.can_notify
        row_dict["is_ready_recommendation"] = (
            row_dict.get("status") == "ready_for_review" and gate_decision.can_recommend
        )
        processed_records.append(row_dict)

    return pd.DataFrame(processed_records)


def fetch_description(job_id: int) -> str:
    con = db.get_connection()
    try:
        row = con.execute("SELECT description FROM jobs WHERE id=?", (job_id,)).fetchone()
        return str(row[0] or "") if row else ""
    finally:
        con.close()


def explain_review(row) -> dict:
    """Describe persisted decisions; do not invent missing LLM reasoning."""
    def present(name):
        value = row.get(name)
        return value if isinstance(value, str) and value.strip() else None

    status = present("status")
    eligibility_status = present("eligibility_status")
    eligibility_raw = present("eligibility_decision_json")

    reason = present("knockout_reason")
    uncertainties = []

    if eligibility_raw:
        try:
            e_res = json.loads(eligibility_raw)
            if isinstance(e_res, dict):
                hard_blocks = e_res.get("hard_block_reasons", [])
                if hard_blocks and not reason:
                    reason = " | ".join(hard_blocks)
                review_reasons = e_res.get("review_reasons", [])
                if review_reasons:
                    uncertainties.extend(review_reasons)
        except (ValueError, TypeError):
            pass

    if eligibility_status == "ineligible":
        stage = "Başvuru engeli (uygun değil)"
    elif eligibility_status == "review":
        stage = "Uygunluk incelemesi bekliyor"
    elif present("pipeline_status") == "FAILED":
        stage = "Belge hazırlığı başarısız"
    elif status in {"applied", "interview", "offer", "rejected", "withdrawn"}:
        stage = "Başvuru takibi" if status != "rejected" else "Elendi / reddedildi"
    elif not row.get("description_available"):
        stage = "İlan içeriği bekleniyor"
    elif pd.isna(row.get("semantic_score")):
        stage = "Semantik değerlendirme bekleniyor"
    elif present("llm_judge_status") != "success":
        stage = "LLM değerlendirmesi bekleniyor"
    elif status == "ready_for_review":
        # Consumers must NOT call ready_for_review a ready recommendation when gate says review
        if eligibility_status == "review" or not row.get("can_recommend", True):
            stage = "Uygunluk incelemesi bekliyor"
        else:
            stage = "Başvuru paketi hazır"
    elif status == "low_priority":
        stage = "Düşük öncelikli inceleme"
    else:
        stage = "Sıralama / belge hazırlığı bekleniyor"

    raw = present("llm_judge_result_json")
    if raw:
        try:
            result = json.loads(raw)
            if isinstance(result, dict):
                for key in ("reason", "reasoning", "explanation"):
                    if not reason and isinstance(result.get(key), str):
                        reason = result[key]
                llm_unc = result.get("uncertainties", [])
                if isinstance(llm_unc, list):
                    uncertainties.extend(llm_unc)
        except (ValueError, TypeError):
            uncertainties.append("Kaydedilmiş değerlendirme okunamadı.")
    return {"stage": stage, "reason": reason, "uncertainties": uncertainties,
            "deadline": present("deadline"), "retry_at": present("llm_judge_next_retry_at")}
