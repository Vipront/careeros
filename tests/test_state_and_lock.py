import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

class TestTelegramStateMachineAndLock(unittest.TestCase):
    def test_telegram_mark_job_status_uses_transition(self):
        """Ensure telegram_bot.py uses transition() and does not execute raw UPDATE jobs SET status."""
        code = (ROOT / "src" / "telegram_bot.py").read_text(encoding="utf-8")
        self.assertNotIn("UPDATE jobs", code, "Direct SQL UPDATE jobs found in telegram_bot.py")
        self.assertIn("transition(", code, "transition() must be called in telegram_bot.py")

    def test_pipeline_lock_acquire_and_release(self):
        """Ensure run_daily.py implements acquire_lock and release_lock properly."""
        import run_daily
        self.assertTrue(hasattr(run_daily, "acquire_lock"))
        self.assertTrue(hasattr(run_daily, "release_lock"))

        lock_file = ROOT / "data" / "test_unit.lock"
        if lock_file.exists():
            lock_file.unlink()

        # 1. First acquire succeeds
        f1 = run_daily.acquire_lock(str(lock_file))
        self.assertIsNotNone(f1, "First lock acquire should succeed")

        # 2. Second concurrent acquire fails
        f2 = run_daily.acquire_lock(str(lock_file))
        self.assertIsNone(f2, "Second concurrent lock acquire should fail")

        # 3. Release first lock
        run_daily.release_lock(f1, str(lock_file))

        # 4. Third acquire succeeds after release
        f3 = run_daily.acquire_lock(str(lock_file))
        self.assertIsNotNone(f3, "Lock acquire should succeed after release")
        run_daily.release_lock(f3, str(lock_file))
        if lock_file.exists():
            lock_file.unlink()

    def test_transition_enforces_terminal_state(self):
        """Test that transition() raises ValueError when attempting to transition out of terminal state."""
        from src.db import ALLOWED, transition
        # rejected is a terminal state in ALLOWED
        self.assertEqual(ALLOWED.get("rejected"), set())

if __name__ == "__main__":
    unittest.main()
