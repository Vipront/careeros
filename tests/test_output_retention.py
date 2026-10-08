import unittest
import os
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timedelta

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

class TestOutputRetentionAndDeduplication(unittest.TestCase):
    def test_output_in_gitignore(self):
        """Ensure output/ is listed in .gitignore."""
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("output/", gitignore)

    def test_cleanup_old_outputs(self):
        """Ensure cleanup_old_outputs removes folders older than N days and preserves newer ones."""
        from run_daily import cleanup_old_outputs
        with tempfile.TemporaryDirectory(prefix="careeros-retention-test-") as temp_dir:
            test_dir = Path(temp_dir)
            now = datetime.now()
            old_folder = test_dir / (now - timedelta(days=45)).strftime("%Y-%m-%d")
            new_folder = test_dir / (now - timedelta(days=10)).strftime("%Y-%m-%d")
            non_date_folder = test_dir / "custom_archive"

            old_folder.mkdir()
            (old_folder / "dummy.txt").write_text("old")
            new_folder.mkdir()
            (new_folder / "dummy.txt").write_text("new")
            non_date_folder.mkdir()
            (non_date_folder / "dummy.txt").write_text("keep")

            removed = cleanup_old_outputs(days=30, output_dir=test_dir)
            self.assertEqual(removed, 1)
            self.assertFalse(old_folder.exists(), "Old folder should have been deleted")
            self.assertTrue(new_folder.exists(), "New folder should be kept")
            self.assertTrue(non_date_folder.exists(), "Non-date folder should be kept")

    def test_generator_duplicate_check_syntax(self):
        """Ensure generator.py checks existing packages across all date directories."""
        generator_code = (ROOT / "src" / "documents" / "generator.py").read_text(encoding="utf-8")
        self.assertIn('OUTPUT.glob(f"*/*_{job_id}")', generator_code)

if __name__ == "__main__":
    unittest.main()
