# -*- coding: utf-8 -*-
"""
Tests for Production Evaluation Instrumentation v1.
Validates structured user rejection reason recording in DB transition & event model.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.db import transition, REJECTION_REASONS, ALLOWED


class TestRejectionReasonInstrumentation(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_jobs.db"

        self.con = sqlite3.connect(self.db_path)
        self.con.execute("""
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY,
                title TEXT,
                status TEXT,
                updated_at TEXT
            )
        """)
        self.con.execute("""
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER,
                event_type TEXT,
                event_time TEXT,
                note TEXT
            )
        """)
        self.con.commit()

    def tearDown(self):
        self.con.close()
        self.tmp_dir.cleanup()

    def _mock_get_connection(self):
        return sqlite3.connect(self.db_path)

    def test_rejection_reasons_constant_contains_all_8_categories(self):
        """Verify the 8 standard rejection categories are defined."""
        expected = [
            "Lokasyon",
            "Dil",
            "Deneyim / Teknik Uyum",
            "Eğitim / Derece",
            "Çalışma İzni / Sponsorluk",
            "Maaş / Koşullar",
            "Şirket / Kişisel Tercih",
            "Diğer",
        ]
        for cat in expected:
            self.assertIn(cat, REJECTION_REASONS)

    def test_backward_compatibility_rejection_without_reason(self):
        """Eski reject davranışı: Reason verilmeden de başarıyla elenebilmeli."""
        self.con.execute("INSERT INTO jobs (id, title, status) VALUES (1, 'Bioinformatician', 'ready_for_review')")
        self.con.commit()

        with patch("src.db.get_connection", side_effect=self._mock_get_connection):
            transition(1, "rejected")

        # Job status must be rejected
        job = self.con.execute("SELECT status FROM jobs WHERE id=1").fetchone()
        self.assertEqual(job[0], "rejected")

        # Event must be recorded with standard note
        ev = self.con.execute("SELECT note FROM events WHERE job_id=1").fetchone()
        self.assertEqual(ev[0], "ready_for_review -> rejected")

    def test_rejection_with_valid_structured_reason(self):
        """Kullanıcı ret nedeni seçtiğinde event note içine ve yapısal biçimde kaydedilmeli."""
        self.con.execute("INSERT INTO jobs (id, title, status) VALUES (2, 'Lab Tech', 'ready_for_review')")
        self.con.commit()

        with patch("src.db.get_connection", side_effect=self._mock_get_connection):
            transition(2, "rejected", note="Dil")

        job = self.con.execute("SELECT status FROM jobs WHERE id=2").fetchone()
        self.assertEqual(job[0], "rejected")

        ev = self.con.execute("SELECT note FROM events WHERE job_id=2").fetchone()
        self.assertIn("Dil", ev[0])
        self.assertEqual(ev[0], "ready_for_review -> rejected: Dil")

    def test_rejection_with_all_valid_categories(self):
        """8 kategorinin her biri için geçerli geçiş yapılabilmeli."""
        for idx, cat in enumerate(REJECTION_REASONS, start=10):
            self.con.execute("INSERT INTO jobs (id, title, status) VALUES (?, 'Job', 'ready_for_review')", (idx,))
            self.con.commit()

            with patch("src.db.get_connection", side_effect=self._mock_get_connection):
                transition(idx, "rejected", note=cat)

            ev = self.con.execute("SELECT note FROM events WHERE job_id=?", (idx,)).fetchone()
            self.assertEqual(ev[0], f"ready_for_review -> rejected: {cat}")

    def test_rejection_with_custom_or_legacy_text_note_is_accepted(self):
        """Serbest metin notları da geriye dönük uyumluluk gereği kabul edilmeli."""
        self.con.execute("INSERT INTO jobs (id, title, status) VALUES (3, 'Job 3', 'ready_for_review')")
        self.con.commit()

        with patch("src.db.get_connection", side_effect=self._mock_get_connection):
            transition(3, "rejected", note="User marked as rejected via Telegram bot")

        ev = self.con.execute("SELECT note FROM events WHERE job_id=3").fetchone()
        self.assertIn("User marked as rejected via Telegram bot", ev[0])

    def test_telegram_bot_mark_job_status_with_rejection_reason(self):
        """Telegram bot mark_job_status note parameter integration test."""
        from src.telegram_bot import mark_job_status
        self.con.execute("INSERT INTO jobs (id, title, status) VALUES (4, 'Job 4', 'ready_for_review')")
        self.con.commit()

        with patch("src.db.get_connection", side_effect=self._mock_get_connection):
            mark_job_status(4, "rejected", note="Maaş / Koşullar")

        job = self.con.execute("SELECT status FROM jobs WHERE id=4").fetchone()
        self.assertEqual(job[0], "rejected")

        ev = self.con.execute("SELECT note FROM events WHERE job_id=4").fetchone()
        self.assertEqual(ev[0], "ready_for_review -> rejected: Maaş / Koşullar")


if __name__ == "__main__":
    unittest.main()
