import os
import unittest
import contextlib
import io
import requests
from unittest.mock import patch, MagicMock
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.llm_judge import call_gemini as judge_gemini, call_claude as judge_dispatcher
from src.documents.generator import call_gemini as gen_gemini, call_claude as gen_dispatcher


class TestGeminiIntegration(unittest.TestCase):
    def setUp(self):
        self.orig_env = dict(os.environ)
        os.environ["GEMINI_API_KEY"] = "fake-gemini-test-key-12345"
        os.environ["LLM_MODEL"] = "gemini-2.5-flash"
        os.environ["LLM_PROVIDER"] = "gemini"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.orig_env)

    @patch("requests.post")
    def test_llm_judge_gemini_success(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": '{"is_match": true, "profile_type": "Wet Lab", "match_score": 92, "missing_critical_skills": [], "relocation_supported": true, "confidence": 0.95, "reasoning": "Strong match."}'
                            }
                        ]
                    }
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 150,
                "candidatesTokenCount": 60,
                "totalTokenCount": 210,
            },
        }
        mock_post.return_value = mock_response

        res, model, usage = judge_gemini("test prompt")
        self.assertEqual(model, "gemini-2.5-flash")
        self.assertTrue(res["is_match"])
        self.assertEqual(res["match_score"], 92)
        self.assertEqual(usage["input_tokens"], 150)
        self.assertEqual(usage["output_tokens"], 60)

        # Check call arguments
        args, kwargs = mock_post.call_args
        self.assertIn("models/gemini-2.5-flash:generateContent", args[0])
        self.assertNotIn("fake-gemini-test-key-12345", args[0])
        self.assertEqual(kwargs["headers"]["x-goog-api-key"], "fake-gemini-test-key-12345")
        self.assertEqual(kwargs["json"]["generationConfig"]["responseMimeType"], "application/json")

    @patch("requests.post")
    def test_generator_gemini_success(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "Dear Hiring Manager,\nI am writing to express my interest..."}
                        ]
                    }
                }
            ]
        }
        mock_post.return_value = mock_response

        text = gen_gemini("write cover letter")
        self.assertIn("Dear Hiring Manager", text)
        args, kwargs = mock_post.call_args
        self.assertNotIn("fake-gemini-test-key-12345", args[0])
        self.assertEqual(kwargs["headers"]["x-goog-api-key"], "fake-gemini-test-key-12345")

    @patch("requests.post")
    def test_unified_dispatcher_prefers_gemini(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": '{"is_match": true, "profile_type": "Bioinformatics", "match_score": 85, "missing_critical_skills": [], "relocation_supported": false, "confidence": 0.9, "reasoning": "Good fit."}'
                            }
                        ]
                    }
                }
            ],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 50},
        }
        mock_post.return_value = mock_response

        res, model, usage = judge_dispatcher("evaluate job")
        self.assertEqual(model, "gemini-2.5-flash")
        self.assertEqual(res["profile_type"], "Bioinformatics")

    def test_judge_gemini_redacts_retry_and_propagated_network_errors(self):
        token = os.environ["GEMINI_API_KEY"]
        output = io.StringIO()

        def fail_request(url, **kwargs):
            self.assertNotIn(token, url)
            self.assertEqual(kwargs["request_kwargs"]["headers"]["x-goog-api-key"], token)
            kwargs["on_retry"](1, 2, None, requests.ConnectionError(f"GET {url}?key={token}"))
            raise requests.ConnectionError(f"GET {url}?key={token}")

        with patch("src.llm_judge.request_with_retry", side_effect=fail_request), contextlib.redirect_stdout(output):
            with self.assertRaises(requests.ConnectionError) as raised:
                judge_gemini("test prompt")

        self.assertNotIn(token, output.getvalue())
        self.assertNotIn(token, str(raised.exception))

    def test_generator_gemini_redacts_http_errors_in_logs_and_caller_exception(self):
        token = os.environ["GEMINI_API_KEY"]
        response = MagicMock()
        response.raise_for_status.side_effect = requests.HTTPError(
            f"403 for https://generativelanguage.googleapis.com/?key={token}"
        )
        output = io.StringIO()

        with patch("src.documents.generator.request_with_retry", return_value=response), contextlib.redirect_stdout(output):
            with self.assertRaises(requests.HTTPError) as raised:
                gen_gemini("test prompt")

        self.assertNotIn(token, output.getvalue())
        self.assertNotIn(token, str(raised.exception))
