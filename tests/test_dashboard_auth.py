import unittest
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_PATH = ROOT / "src" / "dashboard.py"

class TestDashboardAuth(unittest.TestCase):
    def setUp(self):
        self.code = DASHBOARD_PATH.read_text(encoding="utf-8")

    def test_no_hardcoded_fallback_password_in_env_get(self):
        """Ensure DASHBOARD_PASSWORD does not have a default hardcoded password string in getenv."""
        match = re.search(r'os\.getenv\(["\']DASHBOARD_PASSWORD["\']\s*,\s*["\']([^"\']+)["\']\)', self.code)
        if match:
            self.fail(f"Found hardcoded fallback password in os.getenv: {match.group(1)}")

    def test_no_hardcoded_backdoor_passwords(self):
        """Ensure hardcoded passwords like hunter2026 and ugur2026 are not accepted."""
        self.assertNotIn('"hunter2026"', self.code)
        self.assertNotIn("'hunter2026'", self.code)
        self.assertNotIn('"ugur2026"', self.code)
        self.assertNotIn("'ugur2026'", self.code)

    def test_missing_password_safely_stops(self):
        """Ensure that if DASHBOARD_PASSWORD is not set or empty, access is blocked."""
        self.assertTrue(
            "not DASHBOARD_PASSWORD" in self.code or "if not DASHBOARD_PASSWORD:" in self.code,
            "Missing safety check for empty/unset DASHBOARD_PASSWORD"
        )

if __name__ == "__main__":
    unittest.main()
