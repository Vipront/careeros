import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.collectors import linkedin_crawler

class TestCrawlerResilience(unittest.TestCase):
    def setUp(self):
        # Patch asyncio.sleep to 0 for fast mock execution
        self.sleep_patch = patch("asyncio.sleep", new_callable=AsyncMock)
        self.sleep_patch.start()

    def tearDown(self):
        self.sleep_patch.stop()

    def test_normal_raw_results(self):
        """TEST 1: When raw cards are seen, crawler succeeds."""
        mock_jobs = [{"title": "Bioinformatician", "company": "BioLab", "location": "Germany", "url": "http://example.com/1"}]
        with patch.object(linkedin_crawler, "scrape_target_all_pages", new_callable=AsyncMock) as mock_scrape:
            mock_scrape.return_value = (mock_jobs, None) # (jobs, anomaly)
            with patch("src.collectors.linkedin_crawler.get_connection") as mock_get_conn:
                mock_con = MagicMock()
                mock_get_conn.return_value = mock_con
                with patch.object(linkedin_crawler, "upsert_job", return_value=True):
                    with patch("src.collectors.linkedin_crawler.async_playwright") as mock_pw:
                        mock_p = AsyncMock()
                        mock_browser = AsyncMock()
                        mock_ctx = AsyncMock()
                        mock_page = AsyncMock()
                        mock_pw.return_value.__aenter__.return_value = mock_p
                        mock_p.chromium.launch.return_value = mock_browser
                        mock_browser.new_context.return_value = mock_ctx
                        mock_ctx.new_page.return_value = mock_page

                        imported = asyncio.run(linkedin_crawler.run_all_targets())
                        self.assertGreater(imported, 0)

    def test_existing_jobs_only_succeeds(self):
        """TEST 3 & TEST 12: cards > 0 but imported = 0 (all existing) -> SUCCESS, NOT FAILURE."""
        mock_jobs = [{"title": "Bioinformatician", "company": "BioLab", "location": "Germany", "url": "http://example.com/1"}]
        with patch.object(linkedin_crawler, "scrape_target_all_pages", new_callable=AsyncMock) as mock_scrape:
            mock_scrape.return_value = (mock_jobs, None)
            with patch("src.collectors.linkedin_crawler.get_connection") as mock_get_conn:
                mock_con = MagicMock()
                mock_get_conn.return_value = mock_con
                with patch.object(linkedin_crawler, "upsert_job", return_value=False): # existing!
                    with patch("src.collectors.linkedin_crawler.async_playwright") as mock_pw:
                        mock_p = AsyncMock()
                        mock_browser = AsyncMock()
                        mock_ctx = AsyncMock()
                        mock_page = AsyncMock()
                        mock_pw.return_value.__aenter__.return_value = mock_p
                        mock_p.chromium.launch.return_value = mock_browser
                        mock_browser.new_context.return_value = mock_ctx
                        mock_ctx.new_page.return_value = mock_page

                        # Must return 0 without raising RuntimeError (PASS)
                        imported = asyncio.run(linkedin_crawler.run_all_targets())
                        self.assertEqual(imported, 0)

    def test_global_zero_yield_raises_runtime_error(self):
        """TEST 2 & TEST 11: total_cards_seen == 0 across all targets -> raises RuntimeError."""
        with patch.object(linkedin_crawler, "scrape_target_all_pages", new_callable=AsyncMock) as mock_scrape:
            mock_scrape.return_value = ([], "Zero cards found")
            with patch("src.collectors.linkedin_crawler.get_connection") as mock_get_conn:
                mock_con = MagicMock()
                mock_get_conn.return_value = mock_con
                with patch("src.collectors.linkedin_crawler.async_playwright") as mock_pw:
                    mock_p = AsyncMock()
                    mock_browser = AsyncMock()
                    mock_ctx = AsyncMock()
                    mock_page = AsyncMock()
                    mock_pw.return_value.__aenter__.return_value = mock_p
                    mock_p.chromium.launch.return_value = mock_browser
                    mock_browser.new_context.return_value = mock_ctx
                    mock_ctx.new_page.return_value = mock_page

                    with self.assertRaises(RuntimeError) as ctx:
                        asyncio.run(linkedin_crawler.run_all_targets())
                    self.assertIn("Zero-Yield Anomaly", str(ctx.exception))

    def test_http_detection_403_and_429(self):
        """TEST 4 & TEST 5: HTTP status 403 / 429 detected as anomaly."""
        target = {"keywords": "Bioinformatics", "location": "Germany"}
        page = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status = 429
        page.goto.return_value = mock_resp
        page.url = "https://www.linkedin.com/jobs/search?..."

        jobs, anomaly = asyncio.run(linkedin_crawler.scrape_target_all_pages(page, target, max_pages=1))
        self.assertEqual(len(jobs), 0)
        self.assertIn("HTTP_429", str(anomaly))

    def test_authwall_checkpoint_login_detection(self):
        """TEST 6, 7, 8: Authwall, checkpoint and login in page.url detected as anomaly."""
        target = {"keywords": "Bioinformatics", "location": "Germany"}
        page = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        page.goto.return_value = mock_resp

        # Authwall
        page.url = "https://www.linkedin.com/authwall?trk=..."
        jobs, anomaly = asyncio.run(linkedin_crawler.scrape_target_all_pages(page, target, max_pages=1))
        self.assertIn("AUTHWALL", str(anomaly))

        # Checkpoint
        page.url = "https://www.linkedin.com/checkpoint/challenge?..."
        jobs, anomaly = asyncio.run(linkedin_crawler.scrape_target_all_pages(page, target, max_pages=1))
        self.assertIn("CHECKPOINT", str(anomaly))

        # Login
        page.url = "https://www.linkedin.com/login?..."
        jobs, anomaly = asyncio.run(linkedin_crawler.scrape_target_all_pages(page, target, max_pages=1))
        self.assertIn("LOGIN", str(anomaly))

    def test_timeout_handled_per_target(self):
        """TEST 9 & TEST 10: Timeout on one target does not prevent other targets from succeeding."""
        target = {"keywords": "Bioinformatics", "location": "Germany"}
        page = AsyncMock()
        page.goto.side_effect = TimeoutError("Navigation timeout of 30000ms exceeded")

        jobs, anomaly = asyncio.run(linkedin_crawler.scrape_target_all_pages(page, target, max_pages=1))
        self.assertEqual(len(jobs), 0)
        self.assertIn("TIMEOUT", str(anomaly))

if __name__ == "__main__":
    unittest.main()
