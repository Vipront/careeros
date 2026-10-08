# -*- coding: utf-8 -*-
"""
Tests for Job Link Liveness & Expiration Detection (src/ops/liveness.py).
Follows TDD (RED phase) before implementing src/ops/liveness.py.
"""
import unittest
import socket
from pathlib import Path
import sys
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ops.liveness import (
    check_liveness,
    run_liveness_checks,
    LivenessStatus,
    LivenessResult,
    CACHE_DURATION_HOURS
)


class TestJobLinkLiveness(unittest.TestCase):

    def setUp(self):
        self.dns_patch = patch(
            "src.ops.safe_http.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 0))],
        )
        self.dns_patch.start()

    def tearDown(self):
        self.dns_patch.stop()

    def test_liveness_http_200_active(self):
        """A. HTTP 200 with substantive job details and application control -> ACTIVE."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://careers.embl.org/job/123"
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.iter_content.return_value = [b"<html><body><h1>Bioinformatician</h1><h2>Job Description</h2><p>Requirements: Python, R</p><p>Apply now</p></body></html>"]

        with patch("src.ops.safe_http._pinned_get", return_value=mock_resp):
            res = check_liveness("https://careers.embl.org/job/123")
            self.assertEqual(res.status, LivenessStatus.ACTIVE)
            self.assertEqual(res.http_status, 200)
            self.assertIn("liveness-v2:positive-posting", str(res.detail))

    def test_liveness_http_200_closed_content_en(self):
        """B1. HTTP 200 with English 'job is no longer available' -> CLOSED."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://company.workday.com/job/456"
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.iter_content.return_value = [b"<div>This job is no longer available. Thank you.</div>"]

        with patch("src.ops.safe_http._pinned_get", return_value=mock_resp):
            res = check_liveness("https://company.workday.com/job/456")
            self.assertEqual(res.status, LivenessStatus.CLOSED)
            self.assertIn("no longer available", res.detail.lower())

    def test_liveness_http_200_closed_content_tr(self):
        """B2. HTTP 200 with Turkish 'bu ilan yay\u0131ndan kald\u0131r\u0131ld\u0131' -> CLOSED."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://kariyer.net/ilan/789"
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.iter_content.return_value = ["<div>Bu ilan yay\u0131ndan kald\u0131r\u0131lm\u0131\u015ft\u0131r.</div>".encode("utf-8")]

        with patch("src.ops.safe_http._pinned_get", return_value=mock_resp):
            res = check_liveness("https://kariyer.net/ilan/789")
            self.assertEqual(res.status, LivenessStatus.CLOSED)

    def test_liveness_http_200_closed_content_de(self):
        """B3. HTTP 200 with German 'nicht mehr verf\u00fcgbar' -> CLOSED."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://jobs.charite.de/job/101"
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.iter_content.return_value = ["<div>Dieses Stellenangebot ist leider nicht mehr verf\u00fcgbar.</div>".encode("utf-8")]

        with patch("src.ops.safe_http._pinned_get", return_value=mock_resp):
            res = check_liveness("https://jobs.charite.de/job/101")
            self.assertEqual(res.status, LivenessStatus.CLOSED)

    def test_liveness_http_404_not_found(self):
        """C. HTTP 404 is NOT_FOUND, NOT closed."""
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.url = "https://example.org/jobs/expired"
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.iter_content.return_value = [b"Not Found"]

        with patch("src.ops.safe_http._pinned_get", return_value=mock_resp):
            res = check_liveness("https://example.org/jobs/expired")
            self.assertEqual(res.status, LivenessStatus.NOT_FOUND)
            self.assertEqual(res.http_status, 404)

    def test_liveness_http_410_gone_closed(self):
        """D. HTTP 410 Gone is definitively CLOSED."""
        mock_resp = MagicMock()
        mock_resp.status_code = 410
        mock_resp.url = "https://example.org/jobs/410"
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.iter_content.return_value = [b"Gone"]

        with patch("src.ops.safe_http._pinned_get", return_value=mock_resp):
            res = check_liveness("https://example.org/jobs/410")
            self.assertEqual(res.status, LivenessStatus.CLOSED)
            self.assertEqual(res.http_status, 410)

    def test_liveness_http_429_rate_limited(self):
        """E. HTTP 429 is RATE_LIMITED, NOT closed."""
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.url = "https://linkedin.com/jobs/view/123"
        mock_resp.headers = {}
        mock_resp.iter_content.return_value = [b"Too Many Requests"]

        with patch("src.ops.safe_http._pinned_get", return_value=mock_resp):
            res = check_liveness("https://linkedin.com/jobs/view/123")
            self.assertEqual(res.status, LivenessStatus.RATE_LIMITED)
            self.assertEqual(res.http_status, 429)

    def test_liveness_http_503_temporary_error(self):
        """F. HTTP 503 is TEMPORARY_ERROR, NOT closed."""
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.url = "https://example.org/jobs/servererror"
        mock_resp.headers = {}
        mock_resp.iter_content.return_value = [b"Service Unavailable"]

        with patch("src.ops.safe_http._pinned_get", return_value=mock_resp):
            res = check_liveness("https://example.org/jobs/servererror")
            self.assertEqual(res.status, LivenessStatus.TEMPORARY_ERROR)
            self.assertEqual(res.http_status, 503)

    def test_liveness_timeout(self):
        """G. Timeout or network error -> TEMPORARY_ERROR."""
        import requests
        with patch("src.ops.safe_http._pinned_get", side_effect=requests.Timeout("Connection timed out")):
            res = check_liveness("https://example.org/jobs/timeout")
            self.assertEqual(res.status, LivenessStatus.TEMPORARY_ERROR)
            self.assertIn("timeout", res.detail.lower())

    def test_liveness_auth_redirect(self):
        """H. Redirected to /login or /signin endpoint -> AUTH_REQUIRED."""
        redirect = MagicMock()
        redirect.status_code = 302
        redirect.url = "https://company.myworkdayjobs.com/job/1"
        redirect.headers = {"Location": "/login?redirect=%2Fjob%2F1"}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://company.myworkdayjobs.com/login?redirect=%2Fjob%2F1"
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.iter_content.return_value = [b"Please Sign In to View"]

        with patch("src.ops.safe_http._pinned_get", side_effect=[redirect, mock_resp]):
            res = check_liveness("https://company.myworkdayjobs.com/job/1")
            self.assertEqual(res.status, LivenessStatus.AUTH_REQUIRED)
            self.assertIn("login", res.final_url.lower())

    def test_liveness_career_home_redirect(self):
        """I. Redirected from specific job page to generic homepage / careers -> REDIRECTED."""
        redirect = MagicMock()
        redirect.status_code = 301
        redirect.url = "https://www.roche.com/careers/jobs/job-12345"
        redirect.headers = {"Location": "/careers"}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://www.roche.com/careers"
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.iter_content.return_value = [b"Explore our jobs"]

        with patch("src.ops.safe_http._pinned_get", side_effect=[redirect, mock_resp]):
            res = check_liveness("https://www.roche.com/careers/jobs/job-12345")
            self.assertEqual(res.status, LivenessStatus.REDIRECTED)

    def test_liveness_same_job_canonical_redirect(self):
        """J. Redirected to same job (e.g. canonical URL) -> ACTIVE."""
        redirect = MagicMock()
        redirect.status_code = 301
        redirect.url = "https://www.linkedin.com/jobs/view/999999?refId=xyz"
        redirect.headers = {"Location": "/jobs/view/999999/"}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://www.linkedin.com/jobs/view/999999/"
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.iter_content.return_value = [b"<html><h1>Bioinformatician</h1><h2>Job Description</h2><p>Requirements: Python</p><p>Apply now</p></html>"]

        with patch("src.ops.safe_http._pinned_get", side_effect=[redirect, mock_resp]):
            res = check_liveness("https://www.linkedin.com/jobs/view/999999?refId=xyz")
            self.assertEqual(res.status, LivenessStatus.ACTIVE)

    def test_liveness_linkedin_authwall(self):
        """K. LinkedIn authwall / checkpoint / sign-in requirement -> AUTH_REQUIRED (never closed)."""
        redirect = MagicMock()
        redirect.status_code = 302
        redirect.url = "https://www.linkedin.com/jobs/view/123456/"
        redirect.headers = {"Location": "/authwall?trk=bf"}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://www.linkedin.com/authwall?trk=bf"
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.iter_content.return_value = [b"Sign in to see full profile"]

        with patch("src.ops.safe_http._pinned_get", side_effect=[redirect, mock_resp]):
            res = check_liveness("https://www.linkedin.com/jobs/view/123456/")
            self.assertEqual(res.status, LivenessStatus.AUTH_REQUIRED)

    def test_liveness_ssrf_initial_url(self):
        """L. SSRF target in initial URL is immediately blocked -> INVALID_URL."""
        res = check_liveness("http://127.0.0.1:8000/admin")
        self.assertEqual(res.status, LivenessStatus.INVALID_URL)
        self.assertIn("G\u00fcvenlik riski", res.detail)

    def test_liveness_ssrf_redirect_target(self):
        """M. SSRF target reached via redirect is blocked -> INVALID_URL."""
        redirect = MagicMock()
        redirect.status_code = 302
        redirect.url = "https://legit-looking-service.org/go?url=meta"
        redirect.headers = {"Location": "http://169.254.169.254/latest/meta-data/"}

        with patch("src.ops.safe_http._pinned_get", return_value=redirect) as requester:
            res = check_liveness("https://legit-looking-service.org/go?url=meta")
            self.assertEqual(res.status, LivenessStatus.INVALID_URL)
            self.assertIn("Yönlendirme güvenlik sınırını aştı", res.detail)
            requester.assert_called_once()
            redirect.close.assert_called_once()

    def test_liveness_oversized_response(self):
        """N. Streaming safely terminates if response exceeds 2MB limit."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://careers.embl.org/job/huge"
        mock_resp.headers = {"content-type": "text/html"}
        chunk_1mb = b"A" * (1024 * 1024)
        mock_resp.iter_content.return_value = [chunk_1mb, chunk_1mb, chunk_1mb]

        with patch("src.ops.safe_http._pinned_get", return_value=mock_resp):
            res = check_liveness("https://careers.embl.org/job/huge")
            self.assertIn(res.status, [LivenessStatus.ACTIVE, LivenessStatus.UNKNOWN])

    def test_liveness_unsupported_scheme(self):
        """O. Unsupported schemes like ftp:// or file:// are blocked."""
        res = check_liveness("file:///etc/passwd")
        self.assertEqual(res.status, LivenessStatus.INVALID_URL)

    def test_12_hour_cache_behavior_and_ready_for_review_selection(self):
        """P, Q, R, S: Worker checks only ready_for_review jobs, respects 12h cache, does NOT change FSM status."""
        import sqlite3
        import tempfile
        from datetime import datetime, timezone, timedelta
        from src.db import ensure_v2_schema

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(tmp.name)
        tmp.close()

        con = sqlite3.connect(db_path)
        con.execute("""
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY,
                title TEXT,
                company TEXT,
                location TEXT,
                url TEXT,
                status TEXT,
                final_score REAL,
                metrics_json TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        ensure_v2_schema(con)

        now_utc = datetime.now(timezone.utc)
        recent_check = (now_utc - timedelta(hours=2)).isoformat()
        old_check = (now_utc - timedelta(hours=15)).isoformat()

        # Job 1: ready_for_review, old check -> Should be checked
        con.execute("""
            INSERT INTO jobs (id, title, company, url, status, final_score, liveness_status, liveness_checked_at)
            VALUES (1, 'Bioinformatician', 'EMBL', 'https://embl.de/job/1', 'ready_for_review', 80.0, 'ACTIVE', ?)
        """, (old_check,))

        # Job 2: ready_for_review, recent check (2h ago) -> Should be skipped (cached)
        con.execute("""
            INSERT INTO jobs (id, title, company, url, status, final_score, liveness_status, liveness_checked_at)
            VALUES (2, 'Scientist', 'Roche', 'https://roche.com/job/2', 'ready_for_review', 75.0, 'ACTIVE', ?)
        """, (recent_check,))

        # Job 3: rejected job -> Should NOT be checked
        con.execute("""
            INSERT INTO jobs (id, title, company, url, status, final_score, liveness_status)
            VALUES (3, 'Director', 'Novartis', 'https://novartis.com/job/3', 'rejected', 30.0, NULL)
        """,)

        # Job 4: ready_for_review, never checked -> Should be checked and fail gracefully without stopping others
        con.execute("""
            INSERT INTO jobs (id, title, company, url, status, final_score, liveness_status)
            VALUES (4, 'Postdoc', 'DKFZ', 'https://dkfz.de/job/4', 'ready_for_review', 85.0, NULL)
        """,)
        con.commit()

        def fake_check(url):
            if "job/1" in url:
                return LivenessResult(status=LivenessStatus.CLOSED, http_status=410, detail="Gone")
            elif "job/4" in url:
                raise RuntimeError("Network glitch on job 4")
            return LivenessResult(status=LivenessStatus.ACTIVE, http_status=200)

        with patch("src.ops.liveness.check_liveness", side_effect=fake_check):
            summary = run_liveness_checks(con=con, force=False)

        self.assertEqual(summary["total_eligible"], 2)
        self.assertEqual(summary["checked_count"], 2)
        self.assertEqual(summary["skipped_cached"], 1)

        r1 = con.execute("SELECT status, liveness_status, liveness_http_code FROM jobs WHERE id=1").fetchone()
        self.assertEqual(r1[0], "ready_for_review")
        self.assertEqual(r1[1], "CLOSED")
        self.assertEqual(r1[2], 410)

        r3 = con.execute("SELECT liveness_status FROM jobs WHERE id=3").fetchone()
        self.assertIsNone(r3[0])

        con.close()
        db_path.unlink(missing_ok=True)

    def test_liveness_200_without_positive_evidence_returns_unknown(self):
        """HTTP 200 on empty page or without job posting evidence must NOT return ACTIVE."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://company.com/empty"
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.iter_content.return_value = [b"<html><body><div>Hello world</div></body></html>"]

        with patch("src.ops.safe_http._pinned_get", return_value=mock_resp):
            res = check_liveness("https://company.com/empty")
            self.assertEqual(res.status, LivenessStatus.UNKNOWN)
            self.assertIn("positive job posting", res.detail.lower())

    def test_is_liveness_stale_policy(self):
        from src.ops.liveness import is_liveness_stale
        from datetime import datetime, timezone, timedelta

        now_utc = datetime.now(timezone.utc)
        fresh_ts = (now_utc - timedelta(hours=5)).isoformat()
        stale_ts = (now_utc - timedelta(hours=25)).isoformat()
        future_ts = (now_utc + timedelta(hours=2)).isoformat()

        self.assertFalse(is_liveness_stale(fresh_ts, ttl_hours=24))
        self.assertTrue(is_liveness_stale(stale_ts, ttl_hours=24))
        self.assertTrue(is_liveness_stale(future_ts, ttl_hours=24))
        self.assertTrue(is_liveness_stale(None, ttl_hours=24))

    def test_liveness_generic_words_alone_do_not_assert_active(self):
        """Generic words like 'details', 'role', 'profil' on error/generic page must return UNKNOWN."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://company.com/generic"
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.iter_content.return_value = [b"<html><body><div>Click here for details about this role or view your profil.</div></body></html>"]

        with patch("src.ops.safe_http._pinned_get", return_value=mock_resp):
            res = check_liveness("https://company.com/generic")
            self.assertEqual(res.status, LivenessStatus.UNKNOWN)

    def test_liveness_captcha_challenge_returns_auth_required(self):
        """Cloudflare or captcha security check page must return AUTH_REQUIRED, never ACTIVE."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://company.com/careers/job1"
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.iter_content.return_value = [b"<html><body><h1>Attention Required! | Cloudflare</h1><p>Please verify you are a human to continue.</p></body></html>"]

        with patch("src.ops.safe_http._pinned_get", return_value=mock_resp):
            res = check_liveness("https://company.com/careers/job1")
            self.assertEqual(res.status, LivenessStatus.AUTH_REQUIRED)
            self.assertIn("captcha", res.detail.lower())

    def test_liveness_http_400_client_error_returns_unknown(self):
        """HTTP 400 or other 4xx client errors must return UNKNOWN and not proceed to body check."""
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.url = "https://company.com/job/bad"
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.iter_content.return_value = [b"Bad Request"]

        with patch("src.ops.safe_http._pinned_get", return_value=mock_resp):
            res = check_liveness("https://company.com/job/bad")
            self.assertEqual(res.status, LivenessStatus.UNKNOWN)
            self.assertIn("client error", res.detail.lower())

    def test_generic_careers_page_lacks_substantive_details_or_controls_returns_unknown(self):
        """Generic careers portal landing page without specific job requirements/controls returns UNKNOWN."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://company.com/careers"
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.iter_content.return_value = [b"<html><body><h1>Explore Our Careers</h1><p>Join our team and discover exciting global opportunities at our company.</p></body></html>"]

        with patch("src.ops.safe_http._pinned_get", return_value=mock_resp):
            res = check_liveness("https://company.com/careers")
            self.assertEqual(res.status, LivenessStatus.UNKNOWN)
            self.assertIn("substantive job details", res.detail.lower())

    def test_job_posting_schema_structure_with_application_control_returns_active(self):
        """Structured JobPosting JSON-LD combined with application control satisfies positive evidence."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://careers.embl.org/job/789"
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.iter_content.return_value = [
            b'<html><head><script type="application/ld+json">{"@type": "JobPosting", "title": "Bioinformatician"}</script></head><body><button>Easy Apply</button></body></html>'
        ]

        with patch("src.ops.safe_http._pinned_get", return_value=mock_resp):
            res = check_liveness("https://careers.embl.org/job/789")
            self.assertEqual(res.status, LivenessStatus.ACTIVE)
            self.assertEqual(res.http_status, 200)
            self.assertIn("liveness-v2:positive-posting", str(res.detail))


if __name__ == "__main__":
    unittest.main()
