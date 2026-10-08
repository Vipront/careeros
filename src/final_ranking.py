from src.db import get_connection, transition
from src.ops.artifact_qa import application_package_error
import json
from pathlib import Path

from src.db import utc_now as now
from src.eligibility import gate
from src.eligibility.models import LivenessStatusContract, OverallEligibilityStatus

ROOT = Path(__file__).resolve().parents[1]

READY_THRESHOLD = 60.0
NORMAL_THRESHOLD = 45.0

def clamp(v, lo=0.0, hi=100.0):
    try:
        val = float(v or 0.0)
    except (ValueError, TypeError):
        return lo
    return max(lo, min(hi, val))

def calculate_final_score(keyword_score, semantic_score, llm_score):
    if llm_score is not None and float(llm_score) > 0:
        # LLM Judge performs deep contextual reasoning on the full CV & job requirements.
        # Use LLM Judge score directly as the definitive final score.
        return round(float(llm_score), 2)
    keyword = clamp(keyword_score)
    # Normalized semantic scaling
    semantic = clamp(float(semantic_score or 0.0) * 100.0)
    return round(0.40 * keyword + 0.60 * semantic, 2)

def load_llm_results():
    """
    Loads LLM Judge evaluation results directly and exclusively from the database (Single Source of Truth).
    JSON artifacts are no longer read at runtime.
    """
    results = {}
    con = get_connection()
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info(jobs)").fetchall()}
        if "llm_judge_result_json" in columns:
            rows = con.execute(
                "SELECT id, llm_judge_result_json FROM jobs WHERE llm_judge_result_json IS NOT NULL"
            ).fetchall()
            for job_id, raw in rows:
                try:
                    row = json.loads(raw)
                    if isinstance(row, dict):
                        row["job_id"] = int(job_id)
                        results[int(job_id)] = row
                except (json.JSONDecodeError, TypeError) as parse_err:
                    print(f"[Final Ranking] Warning: failed to parse llm_judge_result_json for job #{job_id}: {parse_err}")
                    continue
    finally:
        con.close()
    return results

def run():
    llm_results = load_llm_results()

    con = get_connection()
    con.execute("PRAGMA foreign_keys=ON")
    package_columns = {row[1] for row in con.execute("PRAGMA table_info(jobs)").fetchall()}

    # Score all evaluated and reviewable jobs
    rows = con.execute("""
        SELECT id, profile_type, COALESCE(keyword_score,match_score,0) AS keyword_score, semantic_score, llm_score
        FROM jobs
        WHERE status IN ('evaluated', 'ready_for_review', 'low_priority', 'normal')
    """).fetchall()

    counts = {
        "ready_for_review": 0,
        "normal_evaluated": 0,
        "low_priority": 0,
        "rejected": 0,
        "unchanged": 0,
    }

    for job_id, profile_type, keyword_score, semantic_score, db_llm_score in rows:
        llm = llm_results.get(job_id)

        # "Other" candidates that were deliberately not sent to the LLM
        # stay in evaluated state but are marked low priority instead of
        # being silently left in limbo.
        if profile_type == "Other" and not llm:
            transition(job_id, "low_priority", note="Other profile; no LLM review", connection=con, system=True)
            con.execute("""
                UPDATE jobs
                SET review_priority='low',
                    final_score=?,
                    last_evaluated=?,
                    updated_at=?
                WHERE id=?
            """, (
                round(
                    0.20 * clamp(keyword_score)
                    + 0.25 * clamp(float(semantic_score or 0.0) * 100.0),
                    2
                ),
                now(),
                now(),
                job_id,
            ))
            con.execute("""
                INSERT INTO events(job_id,event_type,event_time,note)
                VALUES(?,?,?,?)
            """, (
                job_id,
                "final_ranking",
                now(),
                "Other profile; low priority because no LLM review was performed",
            ))
            counts["low_priority"] += 1
            continue

        if not llm or "error" in llm:
            provisional_llm = clamp(db_llm_score or 0)
            provisional_final = calculate_final_score(
                keyword_score,
                semantic_score,
                provisional_llm,
            )
            # Do not prematurely reject unjudged candidates; keep evaluated/preview state
            prov_status = "evaluated"
            prov_prio = "high" if provisional_final >= 75.0 else ("normal" if provisional_final >= READY_THRESHOLD else "low")

            transition(job_id, prov_status, note="Awaiting LLM evaluation", connection=con, system=True)
            con.execute("""
                UPDATE jobs
                SET final_score=?,
                    review_priority=?,
                    updated_at=?
                WHERE id=?
            """, (
                provisional_final,
                prov_prio,
                now(),
                job_id,
            ))

            counts["unchanged"] += 1
            continue

        gate.ensure_eligibility_schema(con)
        table_cols = [r[1] for r in con.execute("PRAGMA table_info(jobs)").fetchall()]
        sel_query = f"SELECT {', '.join(table_cols)} FROM jobs WHERE id=?"
        row_tuple = con.execute(sel_query, (job_id,)).fetchone()
        job_data = dict(zip(table_cols, row_tuple, strict=False)) if row_tuple else {}
        job_data["id"] = job_id

        gate_decision = gate.evaluate_job_eligibility(job_data)
        gate.persist_eligibility_decision(con, job_id, gate_decision)

        is_match = bool(llm.get("is_match", False))
        llm_score = clamp(llm.get("match_score", db_llm_score or 0))
        final = calculate_final_score(
            keyword_score,
            semantic_score,
            llm_score,
        )

        note = f"Final ranking: score={final:.1f}; priority=low"
        # Closure alone must not transition rejected; leave in evaluated blocked so liveness retries and reopening can recover
        closure_only = (
            gate_decision.liveness.status == LivenessStatusContract.CLOSED
            and not any(
                b.startswith("Experience:") or b.startswith("Language:") or b.startswith("Posting Language:")
                for b in gate_decision.hard_block_reasons
            )
        )
        if closure_only:
            new_status = "evaluated"
            priority = "low"
            counts["unchanged"] += 1
            note = f"Shared gate closed: {gate_decision.liveness.reason}; awaiting liveness retry"
        elif gate_decision.overall_status == OverallEligibilityStatus.INELIGIBLE:
            new_status = "rejected"
            priority = "low"
            counts["rejected"] += 1
            barrier_reason = "; ".join(gate_decision.hard_block_reasons)
            note = f"Shared gate ineligible: {barrier_reason}"
        elif gate_decision.overall_status == OverallEligibilityStatus.REVIEW:
            new_status = "low_priority" if final < READY_THRESHOLD else "evaluated"
            priority = "low" if final < READY_THRESHOLD else ("high" if final >= 75.0 else "normal")
            counts["low_priority" if new_status == "low_priority" else "unchanged"] += 1
            review_reason = "; ".join(gate_decision.review_reasons)
            note = f"Shared gate review: {review_reason}"
        elif not is_match:
            if llm_score >= 40.0:
                new_status = "low_priority"
                priority = "low"
                counts["low_priority"] += 1
            else:
                new_status = "rejected"
                priority = "low"
                counts["rejected"] += 1
            note = f"Final ranking: score={final:.1f}; priority={priority}"
        elif final >= READY_THRESHOLD:
            package = con.execute(
                "SELECT application_materials_path, pipeline_status FROM jobs WHERE id=?", (job_id,)
            ).fetchone() if {"application_materials_path", "pipeline_status"} <= package_columns else None
            package_ready = bool(package and package[1] == "COMPLETED" and application_package_error(package[0]) is None)
            new_status = "ready_for_review" if package_ready else "evaluated"
            priority = "high" if final >= 75.0 else "normal"
            counts["ready_for_review" if package_ready else "unchanged"] += 1
            note = f"Final ranking: score={final:.1f}; priority={priority}"
        elif final >= NORMAL_THRESHOLD:
            new_status = "low_priority"
            priority = "low"
            counts["normal_evaluated"] += 1
            note = f"Final ranking: score={final:.1f}; priority={priority}"
        else:
            new_status = "low_priority"
            priority = "low"
            counts["low_priority"] += 1
            note = f"Final ranking: score={final:.1f}; priority={priority}"

        transition(job_id, new_status, note=note, connection=con, system=True)
        con.execute("""
            UPDATE jobs
            SET llm_score=?,
                final_score=?,
                review_priority=?,
                last_evaluated=?,
                updated_at=?
            WHERE id=?
        """, (
            llm_score,
            final,
            priority,
            now(),
            now(),
            job_id,
        ))

        con.execute("""
            INSERT INTO events(job_id,event_type,event_time,note)
            VALUES(?,?,?,?)
        """, (
            job_id,
            "final_ranking",
            now(),
            f"keyword={clamp(keyword_score):.1f}; "
            f"semantic={clamp(float(semantic_score or 0.0)*100):.1f}; "
            f"llm={llm_score:.1f}; final={final:.1f}; "
            f"status={new_status}; priority={priority}",
        ))

    con.commit()
    con.close()

    print(
        "FINAL RANKING: "
        f"ready={counts['ready_for_review']} | "
        f"normal={counts['normal_evaluated']} | "
        f"low_priority={counts['low_priority']} | "
        f"rejected={counts['rejected']} | "
        f"unchanged={counts['unchanged']}"
    )

if __name__ == "__main__":
    run()
