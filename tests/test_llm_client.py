"""Offline tests for the shared LLM transport helper."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, Mock

import requests

from src.llm.client import request_with_retry


class TestRequestWithRetry(unittest.TestCase):
    def response(self, status: int) -> MagicMock:
        response = MagicMock()
        response.status_code = status
        response.headers = {}
        return response

    def test_retries_provider_selected_status_and_preserves_response(self) -> None:
        first = self.response(503)
        second = self.response(200)
        post = Mock(side_effect=[first, second])
        sleep = Mock()
        on_retry = Mock()

        result = request_with_retry(
            "https://provider.invalid",
            post=post,
            sleep=sleep,
            request_kwargs={"json": {"prompt": "synthetic"}, "timeout": 7},
            max_attempts=2,
            base_backoff=0.5,
            retry_statuses={503},
            on_retry=on_retry,
            jitter=lambda: 0.0,
        )

        self.assertIs(result, second)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(0.5)
        on_retry.assert_called_once_with(1, 2, 503, None)
        first.close.assert_called_once()
        post.assert_called_with("https://provider.invalid", json={"prompt": "synthetic"}, timeout=7)

    def test_numeric_retry_after_is_bounded_and_has_injected_jitter(self) -> None:
        first = self.response(429)
        first.headers = {"Retry-After": "120"}
        post = Mock(side_effect=[first, self.response(200)])
        sleep = Mock()

        request_with_retry(
            "https://provider.invalid",
            post=post,
            sleep=sleep,
            request_kwargs={},
            max_attempts=2,
            base_backoff=1,
            retry_statuses={429},
            jitter=lambda: 1.0,
            jitter_seconds=0.5,
            max_delay=10,
            max_retry_after=20,
        )

        sleep.assert_called_once_with(10)
        first.close.assert_called_once()

    def test_http_date_retry_after_uses_injected_clock(self) -> None:
        first = self.response(503)
        first.headers = {"Retry-After": "Wed, 21 Oct 2015 07:28:10 GMT"}
        post = Mock(side_effect=[first, self.response(200)])
        sleep = Mock()

        def fixed_now() -> datetime:
            return datetime(2015, 10, 21, 7, 28, 0, tzinfo=timezone.utc)

        request_with_retry(
            "https://provider.invalid",
            post=post,
            sleep=sleep,
            request_kwargs={},
            max_attempts=2,
            base_backoff=1,
            retry_statuses={503},
            jitter=lambda: 0.0,
            now=fixed_now,
        )

        sleep.assert_called_once_with(10.0)

    def test_retries_network_error_and_raises_final_error(self) -> None:
        post = Mock(side_effect=[requests.ConnectionError("offline"), requests.Timeout("still offline")])
        sleep = Mock()

        with self.assertRaises(requests.Timeout):
            request_with_retry(
                "https://provider.invalid",
                post=post,
                sleep=sleep,
                request_kwargs={"timeout": 3},
                max_attempts=2,
                base_backoff=2,
                retry_statuses=set(),
                jitter=lambda: 0.0,
            )

        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_does_not_retry_unconfigured_http_status(self) -> None:
        response = self.response(400)
        post = Mock(return_value=response)

        result = request_with_retry(
            "https://provider.invalid",
            post=post,
            sleep=Mock(),
            request_kwargs={},
            max_attempts=4,
            base_backoff=1,
            retry_statuses={429, 503},
        )

        self.assertIs(result, response)
        post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
