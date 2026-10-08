# -*- coding: utf-8 -*-
"""
Tests for Telegram Bot module-level state and typing integrity.
Verifies _SCAN_IN_PROGRESS flag behavior without connecting to external Telegram API.
"""
import unittest
import src.telegram_bot as tb


class TestTelegramBotState(unittest.TestCase):

    def test_scan_in_progress_flag_exists_at_module_level(self):
        """Ensure _SCAN_IN_PROGRESS is defined at module scope as a boolean."""
        self.assertTrue(hasattr(tb, "_SCAN_IN_PROGRESS"))
        self.assertIsInstance(tb._SCAN_IN_PROGRESS, bool)

    def test_scan_in_progress_initial_state_is_false(self):
        """Initial state of _SCAN_IN_PROGRESS must be False."""
        self.assertFalse(tb._SCAN_IN_PROGRESS)

    def test_get_pending_review_jobs_row_conversion(self):
        """Ensure get_pending_review_jobs converts raw tuple/Row results into dicts."""
        from unittest.mock import patch, MagicMock
        from src.db import utc_now

        cols = [
            "id", "title", "company", "location", "url", "profile_type",
            "llm_score", "final_score", "status", "is_easy_apply",
            "workplace_type", "employment_type", "description",
            "liveness_status", "liveness_http_code", "liveness_detail", "liveness_checked_at"
        ]
        row_tuple = (
            101, "Bioinformatician", "EMBL", "Heidelberg", "https://embl.org/job/101",
            "Bioinformatics", 80, 88, "ready_for_review", 1,
            "Hybrid", "Full-time",
            "Bioinformatics analyst position in English. Python required. No experience required.",
            "ACTIVE", 200, "liveness-v2:positive-posting - Page accessible and application open", utc_now()
        )

        mock_con = MagicMock()

        def mock_execute(sql, *args, **kwargs):
            res = MagicMock()
            if "table_info" in sql:
                res.fetchall.return_value = [(i, col) for i, col in enumerate(cols)]
            else:
                res.fetchall.return_value = [row_tuple]
            return res

        mock_con.execute.side_effect = mock_execute

        with patch("src.telegram_bot.get_db", return_value=mock_con):
            jobs = tb.get_pending_review_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertIsInstance(jobs[0], dict)
            self.assertEqual(jobs[0]["id"], 101)
            self.assertEqual(jobs[0]["company"], "EMBL")
            self.assertEqual(jobs[0].get("is_easy_apply"), 1)
            self.assertEqual(jobs[0].get("workplace_type"), "Hybrid")

    def test_send_review_cards_with_dict_and_tuple_rows(self):
        """Ensure send_review_cards renders correctly without AttributeError."""
        from unittest.mock import patch

        fake_jobs = [
            {
                "id": 999,
                "title": "Bioinformatics Scientist",
                "company": "Roche",
                "location": "Basel",
                "url": "https://linkedin.com/jobs/view/999",
                "profile_type": "Bioinformatics",
                "llm_score": 85.0,
                "final_score": 85.0,
                "is_easy_apply": 1,
                "workplace_type": "On-site",
                "employment_type": "Full-time"
            }
        ]

        sent_messages = []

        def fake_send(method, payload):
            sent_messages.append((method, payload))
            return {"ok": True}

        with patch("src.telegram_bot.CHAT_ID", "123456"), \
             patch("src.telegram_bot.get_pending_review_jobs", return_value=fake_jobs), \
             patch("src.telegram_bot.send_telegram_request", side_effect=fake_send):
            tb.send_review_cards()

            self.assertEqual(len(sent_messages), 2)
            self.assertIn("Başvuru Bekleyen Aktif İlanlar", sent_messages[0][1]["text"])
            self.assertIn("Roche", sent_messages[1][1]["text"])
            self.assertIn("⚡ Kolay Başvuru", sent_messages[1][1]["text"])
            self.assertIn("reply_markup", sent_messages[1][1])


if __name__ == "__main__":
    unittest.main()
