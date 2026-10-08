# -*- coding: utf-8 -*-
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from src.observability import langfuse_client as langfuse


class TestLangfuseObservability(unittest.TestCase):
    def setUp(self):
        client_patch = patch.object(langfuse, "_client", None)
        attempted_patch = patch.object(langfuse, "_init_attempted", False)
        client_patch.start()
        attempted_patch.start()
        self.addCleanup(attempted_patch.stop)
        self.addCleanup(client_patch.stop)

    def test_feature_flag_disabled(self):
        """Disabled tracing returns sanitized data without constructing the SDK client."""
        sdk_module = types.ModuleType("langfuse")
        sdk_module.Langfuse = MagicMock(side_effect=AssertionError("SDK must not initialize"))
        with patch.dict(os.environ, {"LANGFUSE_ENABLED": "false"}), patch.dict(sys.modules, {"langfuse": sdk_module}):
            self.assertFalse(langfuse.is_langfuse_enabled())
            result = langfuse.trace_llm_judge(
                101, "claude-haiku-4-5-20251001", 120.5, 1000, 200, 0.0005, True,
                run_id="run-101", match_score=85.0, is_match=True, profile_type="Wet Lab",
            )
        self.assertEqual(result["job_id"], 101)
        self.assertEqual(result["run_id"], "run-101")
        self.assertEqual(result["usage"]["total_tokens"], 1200)
        self.assertIsNone(result["error_category"])
        sdk_module.Langfuse.assert_not_called()

    def test_valid_configuration_initialization_uses_fake_sdk(self):
        """Valid credentials instantiate the SDK shim without any network activity."""
        fake_client = object()
        constructor = MagicMock(return_value=fake_client)
        sdk_module = types.ModuleType("langfuse")
        sdk_module.Langfuse = constructor
        with (
            patch.dict(os.environ, {
                "LANGFUSE_ENABLED": "true",
                "LANGFUSE_PUBLIC_KEY": "pk-lf-test",
                "LANGFUSE_SECRET_KEY": "sk-lf-test",
                "LANGFUSE_HOST": "https://unit-test.invalid",
            }),
            patch.dict(sys.modules, {"langfuse": sdk_module}),
        ):
            self.assertTrue(langfuse.is_langfuse_enabled())
            client = langfuse.get_langfuse_client()
        self.assertIs(client, fake_client)
        constructor.assert_called_once_with(
            public_key="pk-lf-test", secret_key="sk-lf-test", host="https://unit-test.invalid", debug=False
        )

    def test_sdk_initialization_error_is_isolated(self):
        sdk_module = types.ModuleType("langfuse")
        sdk_module.Langfuse = MagicMock(side_effect=ConnectionError("simulated SDK failure"))
        with (
            patch.dict(os.environ, {
                "LANGFUSE_ENABLED": "true",
                "LANGFUSE_PUBLIC_KEY": "pk-lf-test",
                "LANGFUSE_SECRET_KEY": "sk-lf-test",
            }),
            patch.dict(sys.modules, {"langfuse": sdk_module}),
        ):
            self.assertIsNone(langfuse.get_langfuse_client())

    def test_failure_isolation_network_error(self):
        """A simulated transport failure during emission must not escape the tracing helper."""
        fake_client = MagicMock()
        fake_client.start_observation.side_effect = ConnectionError("simulated offline endpoint")
        with (
            patch.dict(os.environ, {"LANGFUSE_ENABLED": "true"}),
            patch.object(langfuse, "get_langfuse_client", return_value=fake_client),
        ):
            result = langfuse.trace_llm_judge(
                202, "claude-haiku-4-5-20251001", 300.0, 500, 100, 0.0002, False,
                error="HTTP 429 Too Many Requests",
            )
        self.assertEqual(result["job_id"], 202)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_category"], "RATE_LIMIT")
        fake_client.start_observation.assert_called_once()

    def test_privacy_data_minimization(self):
        result = langfuse.trace_llm_judge(
            303, "claude-haiku-4-5-20251001", 450.0, 2000, 400, 0.001, True,
            run_id="run-303", match_score=90.0, is_match=True, profile_type="Bioinformatics",
        )
        serialized = str(result).lower()
        for private_value in ("ugur", "yildirim", "email", "phone", "address", "master_cv"):
            self.assertNotIn(private_value, serialized)
        for key in ("latency_ms", "cost_usd", "usage", "output_summary"):
            self.assertIn(key, result)
        self.assertEqual(result["run_id"], "run-303")

    def test_cost_token_latency_propagation(self):
        from src.observability.telemetry import ModelPricingRegistry

        cost = ModelPricingRegistry.calculate_cost("claude-3-haiku", 1_000_000, 1_000_000)
        self.assertEqual(cost, 1.50)
        result = langfuse.trace_llm_judge(404, "claude-haiku-4-5-20251001", 620.0, 10_000, 2_000, 0.005, True)
        self.assertEqual(result["usage"]["input_tokens"], 10_000)
        self.assertEqual(result["usage"]["output_tokens"], 2_000)
        self.assertEqual(result["usage"]["total_tokens"], 12_000)
        self.assertEqual(result["latency_ms"], 620.0)


if __name__ == "__main__":
    unittest.main()
