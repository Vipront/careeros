import unittest
from unittest.mock import patch

import pandas as pd

import src.db as db
from src.dashboard_overview import get_overview_metrics, get_overview_queues


class TestDashboardOverview(unittest.TestCase):
    def setUp(self):
        self.jobs = pd.DataFrame(
            [
                {"id": 1, "status": "ready_for_review", "final_score": 62.0},
                {"id": 2, "status": "low_priority", "final_score": 92.0},
                {"id": 3, "status": "applied", "final_score": 99.0},
                {"id": 4, "status": "ready_for_review", "final_score": 88.0},
                {"id": 5, "status": "low_priority", "final_score": 54.0},
            ]
        )

    def test_metrics_keep_workflow_status_separate_from_score(self):
        self.assertEqual(
            get_overview_metrics(self.jobs),
            {"ready_count": 2, "high_match_count": 3, "watchlist_count": 2},
        )

    def test_queues_separate_ready_and_low_priority_rows(self):
        ready, watchlist = get_overview_queues(self.jobs)

        self.assertEqual(ready["id"].tolist(), [4, 1])
        self.assertTrue(ready["status"].eq("ready_for_review").all())
        self.assertEqual(watchlist["id"].tolist(), [2, 5])
        self.assertTrue(watchlist["status"].eq("low_priority").all())
        self.assertNotIn(3, ready["id"].tolist())
        self.assertNotIn(3, watchlist["id"].tolist())

    def test_empty_review_queue_does_not_promote_other_active_jobs(self):
        jobs = self.jobs[self.jobs["status"] != "ready_for_review"]

        ready, watchlist = get_overview_queues(jobs)

        self.assertTrue(ready.empty)
        self.assertEqual(watchlist["id"].tolist(), [2, 5])
        self.assertEqual(get_overview_metrics(jobs)["ready_count"], 0)

    def test_preview_limit_applies_independently_to_each_queue(self):
        jobs = pd.concat([self.jobs] * 3, ignore_index=True)

        ready, watchlist = get_overview_queues(jobs, limit=1)

        self.assertEqual(ready["id"].tolist(), [4])
        self.assertEqual(watchlist["id"].tolist(), [2])

    def test_equal_scores_keep_descending_id_order(self):
        jobs = pd.DataFrame(
            [
                {"id": 4, "status": "ready_for_review", "final_score": 88.0},
                {"id": 7, "status": "ready_for_review", "final_score": 88.0},
            ]
        )

        ready, _ = get_overview_queues(jobs)

        self.assertEqual(ready["id"].tolist(), [7, 4])

    def test_provider_label_reports_selected_backend_without_credentials(self):
        with patch.dict("os.environ", {"TURSO_DB_URL": "libsql://example", "TURSO_AUTH_TOKEN": "secret"}), \
             patch.object(db, "TURSO_URL", None), patch.object(db, "TURSO_TOKEN", None):
            self.assertEqual(db.get_connection_provider(), "Turso")

        with patch.dict("os.environ", {"TURSO_DB_URL": "", "TURSO_AUTH_TOKEN": ""}), \
             patch.object(db, "TURSO_URL", None), patch.object(db, "TURSO_TOKEN", None):
            self.assertEqual(db.get_connection_provider(), "SQLite")


    def test_metrics_respect_is_ready_recommendation_filtering(self):
        """Jobs with status 'ready_for_review' but is_ready_recommendation=False are NOT counted in ready_count."""
        jobs_with_gate = pd.DataFrame(
            [
                {"id": 1, "status": "ready_for_review", "final_score": 85.0, "is_ready_recommendation": True, "can_recommend": True},
                {"id": 2, "status": "ready_for_review", "final_score": 80.0, "is_ready_recommendation": False, "can_recommend": False},  # In review/blocked
                {"id": 3, "status": "low_priority", "final_score": 45.0, "is_ready_recommendation": False, "can_recommend": False},
            ]
        )
        metrics = get_overview_metrics(jobs_with_gate)
        # Only Job 1 is truly ready recommendation
        self.assertEqual(metrics["ready_count"], 1)
        self.assertEqual(metrics["watchlist_count"], 1)
        self.assertEqual(metrics["high_match_count"], 2)

    def test_queues_filter_unready_recommendations_from_ready_queue(self):
        """Jobs with status 'ready_for_review' but is_ready_recommendation=False are excluded from the review queue."""
        jobs_with_gate = pd.DataFrame(
            [
                {"id": 1, "status": "ready_for_review", "final_score": 85.0, "is_ready_recommendation": True, "can_recommend": True},
                {"id": 2, "status": "ready_for_review", "final_score": 90.0, "is_ready_recommendation": False, "can_recommend": False},  # Gate blocked
                {"id": 3, "status": "low_priority", "final_score": 70.0, "is_ready_recommendation": False, "can_recommend": False},
            ]
        )
        ready, watchlist = get_overview_queues(jobs_with_gate)
        self.assertEqual(ready["id"].tolist(), [1])
        self.assertNotIn(2, ready["id"].tolist())
        self.assertEqual(watchlist["id"].tolist(), [3])


if __name__ == "__main__":
    unittest.main()
