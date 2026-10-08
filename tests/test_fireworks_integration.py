import json
import os
import unittest
from unittest.mock import MagicMock, patch

import requests

from src.documents import generator
from src import llm_judge
from src.observability.telemetry import (
    DEFAULT_FIREWORKS_MODEL,
    FIREWORKS_INPUT_RATE_PER_MILLION,
    FIREWORKS_OUTPUT_RATE_PER_MILLION,
    ModelPricingRegistry,
    calculate_cost,
    get_telemetry_records,
    reset_telemetry,
)


class FireworksIntegrationTests(unittest.TestCase):
    def setUp(self):
        reset_telemetry()
        self.judge_result = {
            "is_match": True,
            "profile_type": "Bioinformatics",
            "match_score": 86,
            "missing_critical_skills": [],
            "relocation_supported": False,
            "confidence": 0.9,
            "reasoning": "Strong domain match.",
        }

    def response(self, content, usage=None, status=200):
        response = MagicMock()
        response.status_code = status
        response.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            **({"usage": usage} if usage is not None else {}),
        }
        if status >= 400:
            response.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}")
        else:
            response.raise_for_status.return_value = None
        return response

    def test_standard_rate_and_model_pricing(self):
        self.assertEqual(FIREWORKS_INPUT_RATE_PER_MILLION, 0.22)
        self.assertEqual(FIREWORKS_OUTPUT_RATE_PER_MILLION, 0.66)
        cost = calculate_cost(1_000_000, 1_000_000)
        self.assertAlmostEqual(cost["total_cost"], 0.88)
        self.assertAlmostEqual(
            ModelPricingRegistry.calculate_cost(DEFAULT_FIREWORKS_MODEL, 1_000_000, 1_000_000),
            0.88,
        )

    def test_judge_request_parsing_and_usage(self):
        usage = {"prompt_tokens": 1200, "completion_tokens": 400, "total_tokens": 1600}
        mock_response = self.response(json.dumps(self.judge_result), usage)
        with patch.dict(os.environ, {"FIREWORKS_API_KEY": "fw_test_key", "LLM_MODEL": DEFAULT_FIREWORKS_MODEL}):
            with patch("src.llm_judge.requests.post", return_value=mock_response) as post:
                parsed, model, tokens = llm_judge.call_fireworks("evaluate", return_usage=True)
        self.assertEqual(parsed["match_score"], 86)
        self.assertEqual(model, DEFAULT_FIREWORKS_MODEL)
        self.assertEqual(tokens, {"input_tokens": 1200, "output_tokens": 400})
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.fireworks.ai/inference/v1/chat/completions")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer fw_test_key")
        self.assertEqual(kwargs["json"]["model"], DEFAULT_FIREWORKS_MODEL)
        self.assertEqual(get_telemetry_records()[0].prompt_tokens, 1200)

    def test_missing_judge_usage_is_not_invented(self):
        mock_response = self.response(json.dumps(self.judge_result))
        with patch.dict(os.environ, {"FIREWORKS_API_KEY": "fw_test_key"}):
            with patch("src.llm_judge.requests.post", return_value=mock_response):
                parsed, _, usage = llm_judge.call_fireworks("evaluate", return_usage=True)
        self.assertTrue(parsed["is_match"])
        self.assertEqual(usage, {})
        self.assertEqual(get_telemetry_records(), [])

    def test_missing_key_fails_without_request(self):
        with patch.dict(os.environ, {"FIREWORKS_API_KEY": ""}):
            with patch("src.llm_judge.requests.post") as post:
                with self.assertRaisesRegex(RuntimeError, "FIREWORKS_API_KEY is not set"):
                    llm_judge.call_fireworks("evaluate")
        post.assert_not_called()

    def test_judge_retries_transient_status_but_not_bad_request(self):
        ok = self.response(json.dumps(self.judge_result), {"prompt_tokens": 4, "completion_tokens": 3})
        with patch.dict(os.environ, {"FIREWORKS_API_KEY": "fw_test_key"}):
            with patch("src.llm_judge.requests.post", side_effect=[self.response("", status=503), ok]) as post:
                with patch("src.llm_judge.time.sleep"):
                    parsed, _ = llm_judge.call_fireworks("evaluate", max_retries=2)
            self.assertTrue(parsed["is_match"])
            self.assertEqual(post.call_count, 2)
            with patch("src.llm_judge.requests.post", return_value=self.response("", status=400)) as post:
                with self.assertRaises(requests.HTTPError):
                    llm_judge.call_fireworks("evaluate", max_retries=3)
            post.assert_called_once()

    def test_generator_text_and_token_tracking(self):
        raw = '{"summary":"short"}'
        usage = {"prompt_tokens": 200, "completion_tokens": 40, "total_tokens": 240}
        mock_response = self.response(raw, usage)
        with patch.dict(os.environ, {"FIREWORKS_API_KEY": "fw_test_key", "LLM_MODEL": DEFAULT_FIREWORKS_MODEL}):
            with patch("src.documents.generator.requests.post", return_value=mock_response) as post:
                text = generator.call_fireworks("generate")
        self.assertEqual(text, raw)
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer fw_test_key")
        self.assertEqual(get_telemetry_records()[0].completion_tokens, 40)

    def test_generator_missing_usage_is_not_counted(self):
        raw = '{"summary":"short"}'
        mock_response = self.response(raw)
        with patch.dict(os.environ, {"FIREWORKS_API_KEY": "fw_test_key"}):
            with patch("src.documents.generator.requests.post", return_value=mock_response):
                self.assertEqual(generator.call_fireworks("generate"), raw)
        self.assertEqual(get_telemetry_records(), [])

    def test_explicit_dispatch_selects_fireworks(self):
        with patch("src.llm_judge.call_fireworks", return_value=(self.judge_result, DEFAULT_FIREWORKS_MODEL, {})) as judge_call:
            result = llm_judge.call_claude("evaluate", provider="fireworks")
        judge_call.assert_called_once()
        self.assertEqual(result[0], self.judge_result)

        with patch("src.documents.generator.call_fireworks", return_value="generated") as generator_call:
            result = generator.call_claude("generate", provider="fireworks")
        generator_call.assert_called_once()
        self.assertEqual(result, "generated")

    def test_unsupported_provider_fails_instead_of_falling_back(self):
        with self.assertRaisesRegex(ValueError, "Unsupported LLM provider"):
            llm_judge.call_claude("evaluate", provider="unknown")
        with self.assertRaisesRegex(ValueError, "Unsupported LLM provider"):
            generator.call_claude("generate", provider="unknown")


if __name__ == "__main__":
    unittest.main()
