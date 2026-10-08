# -*- coding: utf-8 -*-
"""
Tests for Dashboard Data Loading Functions.
Tests load_active_applications, load_analytics_metrics, and load_all_explorer_data
independently with mock SQLite database.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
import pandas as pd

ACTIVE_STATUSES = ('ready_for_review', 'low_priority', 'applied', 'interview', 'offer')

def load_active_applications_from_con(con):
    query = """
        SELECT
            id, title, company, location, url, status,
            review_priority, final_score, semantic_score, llm_score,
            requirements_text, description, knockout_reason, created_at, updated_at,
            is_easy_apply, workplace_type, employment_type,
            liveness_status, liveness_checked_at, liveness_http_code
        FROM jobs
        WHERE status IN ('ready_for_review', 'low_priority', 'applied', 'interview', 'offer')
        ORDER BY final_score DESC, id DESC
    """
    cursor = con.execute(query)
    columns = [col[0] for col in cursor.description]
    rows = cursor.fetchall()
    return pd.DataFrame(rows, columns=columns)

def load_analytics_metrics_from_con(con):
    total_scraped = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] or 0
    passed_semantic = con.execute("SELECT COUNT(*) FROM jobs WHERE semantic_score > 0").fetchone()[0] or 0
    shortlisted = con.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('ready_for_review', 'applied', 'interview', 'offer')").fetchone()[0] or 0
    applied = con.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('applied', 'interview', 'offer')").fetchone()[0] or 0
    interviews = con.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('interview', 'offer')").fetchone()[0] or 0
    return {
        "total_scraped": total_scraped,
        "passed_semantic": passed_semantic,
        "shortlisted": shortlisted,
        "applied": applied,
        "interviews": interviews
    }

def load_all_explorer_data_from_con(con):
    query = """
        SELECT
            id, title, company, location, url, status,
            review_priority, final_score, semantic_score, llm_score,
            requirements_text, description, knockout_reason, created_at, updated_at,
            is_easy_apply, workplace_type, employment_type,
            liveness_status, liveness_checked_at, liveness_http_code
        FROM jobs
        ORDER BY final_score DESC, id DESC
    """
    cursor = con.execute(query)
    columns = [col[0] for col in cursor.description]
    rows = cursor.fetchall()
    return pd.DataFrame(rows, columns=columns)


class TestDashboardDataLoading(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(self.tmp.name)
        self.tmp.close()

        self.con = sqlite3.connect(self.db_path)
        self.con.execute("""
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                company TEXT,
                location TEXT,
                url TEXT,
                status TEXT,
                review_priority TEXT,
                final_score REAL,
                semantic_score REAL,
                llm_score REAL,
                requirements_text TEXT,
                description TEXT,
                knockout_reason TEXT,
                created_at TEXT,
                updated_at TEXT,
                is_easy_apply INTEGER DEFAULT 0,
                workplace_type TEXT,
                employment_type TEXT,
                liveness_status TEXT,
                liveness_checked_at TEXT,
                liveness_http_code INTEGER
            )
        """)

        records = [
            ("Bioinformatician", "EMBL", "ready_for_review", 88.0, 0.82),
            ("Bioinformatician 2", "DKFZ", "ready_for_review", 84.0, 0.79),
            ("Applied Scientist", "Roche", "applied", 79.0, 0.75),
            ("Interview Candidate", "Novartis", "interview", 85.0, 0.80),
            ("Low Priority Tech", "TUM", "low_priority", 52.0, 0.45),
            ("Rejected Lead 1", "Pharma A", "rejected", 30.0, 0.10),
            ("Rejected Lead 2", "Pharma B", "rejected", 25.0, 0.05),
            ("Rejected Lead 3", "Pharma C", "rejected", 15.0, 0.00),
            ("Rejected Lead 4", "Pharma D", "rejected", 20.0, 0.20),
            ("Rejected Lead 5", "Pharma E", "rejected", 35.0, 0.15),
        ]
        self.con.executemany("""
            INSERT INTO jobs (title, company, status, final_score, semantic_score)
            VALUES (?, ?, ?, ?, ?)
        """, records)
        self.con.commit()

    def tearDown(self):
        self.con.close()
        self.db_path.unlink(missing_ok=True)

    def test_load_active_applications_filters_correctly(self):
        df_active = load_active_applications_from_con(self.con)
        self.assertEqual(len(df_active), 5)
        self.assertNotIn("rejected", df_active["status"].values)
        self.assertIn("ready_for_review", df_active["status"].values)
        self.assertIn("applied", df_active["status"].values)
        self.assertIn("interview", df_active["status"].values)
        self.assertIn("low_priority", df_active["status"].values)

    def test_load_analytics_metrics_returns_correct_aggregates(self):
        metrics = load_analytics_metrics_from_con(self.con)
        self.assertEqual(metrics["total_scraped"], 10)
        self.assertEqual(metrics["passed_semantic"], 9)
        self.assertEqual(metrics["shortlisted"], 4)
        self.assertEqual(metrics["applied"], 2)
        self.assertEqual(metrics["interviews"], 1)

    def test_load_all_explorer_data_contains_rejected(self):
        df_all = load_all_explorer_data_from_con(self.con)
        self.assertEqual(len(df_all), 10)
        self.assertIn("rejected", df_all["status"].values)


if __name__ == "__main__":
    unittest.main()
