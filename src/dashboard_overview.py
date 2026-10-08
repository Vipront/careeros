"""Pure data helpers for the dashboard overview."""

import pandas as pd


PREVIEW_LIMIT = 3


def get_overview_metrics(active_jobs: pd.DataFrame) -> dict[str, int]:
    """Return independent workflow-status and score counts for the overview."""
    if active_jobs is None or active_jobs.empty:
        return {"ready_count": 0, "high_match_count": 0, "watchlist_count": 0}

    statuses = active_jobs["status"] if "status" in active_jobs.columns else pd.Series(dtype="object")
    scores = (
        pd.to_numeric(active_jobs["final_score"], errors="coerce")
        if "final_score" in active_jobs.columns
        else pd.Series(dtype="float64")
    )
    if "is_ready_recommendation" in active_jobs.columns:
        ready_count = int(active_jobs["is_ready_recommendation"].astype(bool).sum())
    elif "can_recommend" in active_jobs.columns:
        ready_count = int((statuses.eq("ready_for_review") & active_jobs["can_recommend"].astype(bool)).sum())
    else:
        ready_count = int(statuses.eq("ready_for_review").sum())

    return {
        "ready_count": ready_count,
        "high_match_count": int(scores.ge(80.0).sum()),
        "watchlist_count": int(statuses.eq("low_priority").sum()),
    }


def get_overview_queues(active_jobs: pd.DataFrame, limit: int = PREVIEW_LIMIT) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return separate, score-ordered review and low-priority preview queues."""
    if active_jobs is None or active_jobs.empty or "status" not in active_jobs.columns:
        empty = pd.DataFrame() if active_jobs is None else active_jobs.iloc[0:0].copy()
        return empty, empty.copy()

    sort_columns = [column for column in ("final_score", "id") if column in active_jobs.columns]
    ascending = [False for _ in sort_columns]

    def select_status(status: str) -> pd.DataFrame:
        if status == "ready_for_review":
            if "is_ready_recommendation" in active_jobs.columns:
                rows = active_jobs.loc[active_jobs["is_ready_recommendation"].astype(bool)].copy()
            elif "can_recommend" in active_jobs.columns:
                rows = active_jobs.loc[active_jobs["status"].eq(status) & active_jobs["can_recommend"].astype(bool)].copy()
            else:
                rows = active_jobs.loc[active_jobs["status"].eq(status)].copy()
        else:
            rows = active_jobs.loc[active_jobs["status"].eq(status)].copy()
        if sort_columns:
            rows = rows.sort_values(by=sort_columns, ascending=ascending)
        return rows.head(limit)

    return select_status("ready_for_review"), select_status("low_priority")
