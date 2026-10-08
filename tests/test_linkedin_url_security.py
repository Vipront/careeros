from unittest.mock import patch

import pytest

from src.enrichment.dynamic_enricher import fetch_direct_linkedin_job


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/linkedin.com/jobs/view/123",
    "https://linkedin.com.evil.example/jobs/view/123",
    "https://evil.example/?next=linkedin.com/jobs/view/123",
    "https://linkedin.com@evil.example/jobs/view/123",
    "https://user:password@www.linkedin.com/jobs/view/123",
    "https://www.linkedin.com:8443/jobs/view/123",
    "https://www.linkedin.com:invalid/jobs/view/123",
    "https://www.linkedin.com/jobs/view-fake/123",
])
def test_linkedin_rejects_untrusted_targets_without_request(url):
    with patch("src.enrichment.dynamic_enricher.safe_get") as request:
        assert fetch_direct_linkedin_job(url) is None
        request.assert_not_called()


def test_linkedin_uses_redirect_safe_http_client():
    with patch("src.enrichment.dynamic_enricher.safe_get") as request:
        request.return_value.status_code = 404
        assert fetch_direct_linkedin_job("https://www.linkedin.com/jobs/view/123") is None
        request.assert_called_once()
