# -*- coding: utf-8 -*-
import unittest
import socket
import tempfile
import sqlite3
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ingestion.manual_ingestion import (
    validate_url,
    sanitize_input_text,
    fetch_job_from_url,
    process_manual_job,
    normalize_url,
    ManualIngestionResult
)

class TestManualJobIngestion(unittest.TestCase):
    def setUp(self):
        self.dns_patch = patch(
            "src.ops.safe_http.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 0))],
        )
        self.dns_patch.start()
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.tmp_dir) / "test_jobs.db"
        # Initialize schema
        con = sqlite3.connect(str(self.db_path))
        con.execute("""
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT UNIQUE,
                title TEXT NOT NULL,
                company TEXT DEFAULT '',
                location TEXT DEFAULT '',
                url TEXT DEFAULT '',
                source TEXT DEFAULT '',
                date_found TEXT,
                created_at TEXT,
                updated_at TEXT,
                description TEXT DEFAULT '',
                requirements_text TEXT DEFAULT '',
                education_requirements TEXT DEFAULT '',
                experience_requirements TEXT DEFAULT '',
                eligibility_text TEXT DEFAULT '',
                description_available INTEGER DEFAULT 0,
                status TEXT DEFAULT 'new',
                is_active INTEGER DEFAULT 1,
                profile_type TEXT,
                keyword_score REAL,
                semantic_score REAL,
                match_score REAL,
                final_score REAL,
                llm_score REAL,
                knockout_reason TEXT
            )
        """)
        con.execute("""
            CREATE TABLE events (
                job_id INTEGER,
                event_type TEXT,
                event_time TEXT,
                note TEXT
            )
        """)
        con.commit()
        con.close()

    def tearDown(self):
        self.dns_patch.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _get_con(self):
        return sqlite3.connect(str(self.db_path))

    # A. Valid URL validation
    def test_validate_url_valid(self):
        valid, err = validate_url("https://careers.embl.org/job/12345")
        self.assertTrue(valid)
        self.assertIsNone(err)

    # B. Invalid URL rejection
    def test_validate_url_invalid_scheme(self):
        valid, err = validate_url("ftp://example.com/job")
        self.assertFalse(valid)
        self.assertIn("HTTP/HTTPS", err)

        valid_malformed, err_m = validate_url("not_a_valid_url")
        self.assertFalse(valid_malformed)

    # G. SSRF & Private IP rejection
    def test_validate_url_ssrf_protection(self):
        ssrf_targets = [
            "http://localhost:8000/job",
            "http://127.0.0.1:8080/secret",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.1/admin",
            "http://192.168.1.1/router",
            "http://[::1]/private"
        ]
        for target in ssrf_targets:
            valid, err = validate_url(target)
            self.assertFalse(valid, f"SSRF target {target} should be rejected")
            self.assertIn("güvenlik", err.lower())

    def test_fetch_rejects_private_redirect_before_requesting_target(self):
        response = MagicMock()
        response.status_code = 302
        response.url = "https://jobs.example.org/redirect"
        response.headers = {"Location": "http://169.254.169.254/latest/meta-data/"}

        with patch("src.ops.safe_http._pinned_get", return_value=response) as requester:
            ok, _text, error = fetch_job_from_url("https://jobs.example.org/redirect")

        self.assertFalse(ok)
        self.assertIn("Yönlendirme", error)
        requester.assert_called_once()
        self.assertFalse(requester.call_args.kwargs["allow_redirects"])
        response.close.assert_called_once()

    # H. Empty / Oversized text validation
    def test_sanitize_input_text(self):
        valid, text, err = sanitize_input_text("")
        self.assertFalse(valid)
        self.assertIn("boş", err.lower())

        oversized = "A" * 70000
        valid_o, text_o, err_o = sanitize_input_text(oversized)
        self.assertFalse(valid_o)
        self.assertIn("azami uzunluğu", err_o)

        valid_ok, text_ok, _ = sanitize_input_text("   Bioinformatics Scientist position at Max Planck   ")
        self.assertTrue(valid_ok)
        self.assertEqual(text_ok, "Bioinformatics Scientist position at Max Planck")

    def test_process_manual_job_rejects_oversized_text_before_insert(self):
        with patch("src.db.get_connection", side_effect=self._get_con):
            result = process_manual_job(
                title="Bioinformatics Scientist",
                description="A" * 70000,
            )

        self.assertFalse(result.success)
        self.assertIn("azami uzunluğu", result.error.lower())
        con = self._get_con()
        self.assertEqual(con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        con.close()

    # D. Ingestion service with direct text & Manual metadata persistence
    def test_process_manual_job_direct_text(self):
        with patch("src.db.get_connection", side_effect=self._get_con):
            result = process_manual_job(
                url="https://jobs.example.org/bioinfo-1",
                title="Bioinformatics Research Associate",
                company="Max Planck Institute",
                location="Munich, Germany",
                description="Analyzing scRNA-seq and NGS datasets with R and Python in computational genomics."
            )

            self.assertTrue(result.success)
            self.assertIsNotNone(result.job_id)
            self.assertEqual(result.status, "evaluated")
            self.assertIn("pending semantic and LLM evaluation", result.reason)

            # Verify DB persistence
            con = self._get_con()
            row = con.execute("SELECT source, url, status, description_available FROM jobs WHERE id = ?", (result.job_id,)).fetchone()
            con.close()
            self.assertEqual(row[0], "manual")
            self.assertEqual(row[1], "https://jobs.example.org/bioinfo-1")
            self.assertEqual(row[2], result.status)
            self.assertEqual(row[3], 1)

    # C. Duplicate URL rejection
    def test_duplicate_url_rejection(self):
        with patch("src.db.get_connection", side_effect=self._get_con):
            # First insertion
            res1 = process_manual_job(
                url="https://jobs.example.org/duplicate-job",
                title="Computational Biologist",
                company="Novartis",
                location="Basel",
                description="Docking and molecular dynamics."
            )
            self.assertTrue(res1.success)

            # Second insertion with same canonical URL
            res2 = process_manual_job(
                url="https://jobs.example.org/duplicate-job?utm_source=linkedin",
                title="Computational Biologist",
                company="Novartis",
                location="Basel",
                description="Docking and molecular dynamics."
            )
            self.assertFalse(res2.success)
            self.assertTrue(res2.is_duplicate)
            self.assertEqual(res2.job_id, res1.job_id)

    # J. Hard barrier -> Rejected workflow
    def test_distinct_posting_urls_are_not_duplicates(self):
        pairs = [
            ("https://example.org/careers?jobId=101", "https://example.org/careers?jobId=102"),
            ("https://example.org/job/123", "https://example.org/job/12"),
            ("https://example.org/job/a_b", "https://example.org/job/a%b"),
        ]
        with patch("src.db.get_connection", side_effect=self._get_con):
            for index, (first, second) in enumerate(pairs):
                with self.subTest(first=first, second=second):
                    for suffix, url in enumerate((first, second)):
                        result = process_manual_job(
                            url=url, title=f"Computational Biology Intern {index} {suffix}",
                            description="Bioinformatics internship. Python programming. No prior experience required.",
                        )
                        self.assertTrue(result.success, result.error or result.reason)
                        self.assertFalse(result.is_duplicate)

    def test_tracking_variants_match_existing_stored_url(self):
        with patch("src.db.get_connection", side_effect=self._get_con):
            first = process_manual_job(
                url="https://example.org/jobs?jobId=101&utm_source=first&lang=en",
                title="Bioinformatics Intern", description="Python programming internship.",
            )
            second = process_manual_job(
                url="https://example.org/jobs?lang=en&jobId=101&utm_source=second#apply",
                title="Different title to isolate URL matching", description="Python programming internship.",
            )
        self.assertTrue(first.success)
        self.assertTrue(second.is_duplicate)
        self.assertEqual(first.job_id, second.job_id)

    def test_normalization_preserves_identity_and_blank_parameters(self):
        self.assertEqual(
            normalize_url("https://EXAMPLE.org/jobs/;position=12?jobId=101&flag=&utm_source=x#apply"),
            "https://example.org/jobs/;position=12?flag=&jobId=101",
        )

    def test_manual_job_hard_barrier_rejection(self):
        with patch("src.db.get_connection", side_effect=self._get_con):
            result = process_manual_job(
                url="https://jobs.example.org/lead-role",
                title="Lead Principal Director of Bioinformatics", # Triggers knockout Senior/Director rule
                company="BigPharma",
                location="Berlin",
                description="Managing 20 scientists."
            )
            self.assertTrue(result.success) # Ingestion succeeded
            self.assertEqual(result.status, "rejected")
            self.assertIn("Principal", str(result.reason))

if __name__ == "__main__":
    unittest.main()
