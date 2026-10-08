import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import src.telegram_bot as telegram_bot
import src.telegram_notify as telegram_notify


class TestTelegramReviewFilter(unittest.TestCase):
    def test_review_cards_select_only_ready_for_review(self):
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = []

        with patch("src.telegram_bot.get_db", return_value=connection):
            self.assertEqual(telegram_bot.get_pending_review_jobs(), [])

        query = connection.execute.call_args.args[0]
        self.assertIn("WHERE status = 'ready_for_review'", query)
        self.assertNotIn("final_score >= 60", query)

    def test_daily_report_counts_only_ready_for_review(self):
        connection = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.side_effect = [(4071,), (0,), (3,), (0,), (2,)]
        connection.execute.return_value = cursor

        with tempfile.TemporaryDirectory() as temp_dir, \
             patch("src.telegram_notify.get_connection", return_value=connection), \
             patch("src.telegram_notify.ROOT", Path(temp_dir)):
            message = telegram_notify.build_message()

        query = next(
            call.args[0]
            for call in connection.execute.call_args_list
            if "ready_for_review" in call.args[0]
        )
        self.assertIn("WHERE status = 'ready_for_review'", query)
        self.assertNotIn("final_score >= 60", query)
        self.assertIn("İnceleme Bekleyen Aktif İlan: <b>0 İlan</b>", message)

    def test_daily_report_filters_closed_and_ineligible(self):
        import sqlite3
        from datetime import datetime, timezone
        now_ts = datetime.now(timezone.utc).isoformat()

        con = sqlite3.connect(":memory:")
        con.execute("""
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY,
                date_found TEXT,
                llm_judge_status TEXT,
                status TEXT,
                title TEXT,
                description TEXT,
                requirements_text TEXT,
                liveness_status TEXT,
                liveness_checked_at TEXT,
                liveness_http_code INTEGER,
                liveness_detail TEXT,
                eligibility_status TEXT
            )
        """)
        # Insert 1 fully eligible ready job
        con.execute("""
            INSERT INTO jobs VALUES (
                1, '2026-10-08', 'success', 'ready_for_review',
                'Bioinformatics Intern', 'Computational biology internship in English. Requires Python and biology knowledge.',
                'Enrolled student or graduate with 0-1 years experience.',
                'ACTIVE', ?, 200, 'liveness-v2:positive-posting - Page accessible and application open', 'eligible'
            )
        """, (now_ts,))
        # Insert 1 closed ready job
        con.execute("""
            INSERT INTO jobs VALUES (
                2, '2026-10-08', 'success', 'ready_for_review',
                'Bioinformatics Intern', 'Role text in English.', '',
                'CLOSED', ?, 410, 'HTTP 410 Gone', 'eligible'
            )
        """, (now_ts,))
        # Insert 1 ineligible ready job (requires 10+ years experience)
        con.execute("""
            INSERT INTO jobs VALUES (
                3, '2026-10-08', 'success', 'ready_for_review',
                'Director of Bioinformatics', 'Requires 10+ years of industry experience.', '10+ years industry experience required',
                'ACTIVE', ?, 200, 'Page accessible and application open', 'ineligible'
            )
        """, (now_ts,))
        # Insert 1 NOT_FOUND ready job
        con.execute("""
            INSERT INTO jobs VALUES (
                4, '2026-10-08', 'success', 'ready_for_review',
                'Bioinformatics Intern', 'Role text in English.', '',
                'NOT_FOUND', ?, 404, 'HTTP 404', 'eligible'
            )
        """, (now_ts,))
        # Insert 1 unverified review ready job (liveness unverified / unknown)
        con.execute("""
            INSERT INTO jobs VALUES (
                5, '2026-10-08', 'success', 'ready_for_review',
                'Bioinformatics Intern', 'Role text in English.', '',
                NULL, NULL, NULL, NULL, 'review'
            )
        """)
        con.commit()

        msg = telegram_notify.build_message(con=con)
        con.close()

        # Jobs 1 and 5 pass; missing source proof is no longer a review barrier.
        # Known closed/missing jobs and the experience mismatch remain excluded.
        self.assertIn("İnceleme Bekleyen Aktif İlan: <b>2 İlan</b>", msg)

    def test_get_pending_review_jobs_evaluates_gate_and_filters_only_can_notify(self):
        import sqlite3
        from datetime import datetime, timezone
        now_ts = datetime.now(timezone.utc).isoformat()

        con = sqlite3.connect(":memory:")
        con.execute("""
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY,
                status TEXT,
                title TEXT,
                company TEXT,
                location TEXT,
                url TEXT,
                profile_type TEXT,
                keyword_score REAL,
                semantic_score REAL,
                llm_score REAL,
                final_score REAL,
                review_priority TEXT,
                is_easy_apply INTEGER,
                workplace_type TEXT,
                employment_type TEXT,
                description TEXT,
                requirements_text TEXT,
                liveness_status TEXT,
                liveness_checked_at TEXT,
                liveness_http_code INTEGER,
                liveness_detail TEXT,
                eligibility_status TEXT
            )
        """)
        # 1 eligible job
        con.execute("""
            INSERT INTO jobs VALUES (
                101, 'ready_for_review', 'Computational Biology Intern', 'Lab A', 'Remote',
                'https://lab.org/101', 'Bioinformatics', 80, 0.8, 85, 85, 'high',
                1, 'remote', 'internship',
                'Internship in computational biology and bioinformatics. Working language is English. Python required. 0 years required.',
                '', 'ACTIVE', ?, 200, 'liveness-v2:positive-posting - Page accessible and application open', 'eligible'
            )
        """, (now_ts,))
        # 1 closed job
        con.execute("""
            INSERT INTO jobs VALUES (
                102, 'ready_for_review', 'Closed Intern', 'Lab B', 'Remote',
                'https://lab.org/102', 'Bioinformatics', 80, 0.8, 85, 85, 'high',
                1, 'remote', 'internship',
                'Internship in bioinformatics.', '',
                'CLOSED', ?, 410, 'HTTP 410 Gone', 'eligible'
            )
        """, (now_ts,))

        with patch("src.telegram_bot.get_db", return_value=con):
            pending = telegram_bot.get_pending_review_jobs(limit=10)

        con.close()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], 101)


if __name__ == "__main__":
    unittest.main()
