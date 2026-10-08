"""Destructive retention checks use disposable release trees only."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from scripts.cleanup_releases import cleanup_releases


class TestReleaseRetention(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.releases = self.root / "releases"
        self.releases.mkdir()
        for index in range(5):
            release = self.releases / f"release-{index}"
            (release / ".venv" / "bin").mkdir(parents=True)
            (release / ".venv" / "bin" / "python").write_text("test environment")
            marker = release / ".deployment-success"
            marker.touch()
            os.utime(marker, (100 + index, 100 + index))
            os.utime(release, (100 + index, 100 + index))
        try:
            (self.root / "current").symlink_to(self.releases / "release-4", target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")

    def test_keeps_active_and_two_latest_rollbacks(self):
        result = cleanup_releases(self.root)
        self.assertEqual(set(result["removed"]), {"release-0", "release-1"})
        self.assertTrue((self.releases / "release-4").is_dir())

    def test_previous_release_is_retained_even_if_older(self):
        result = cleanup_releases(self.root, self.releases / "release-0")
        self.assertEqual(set(result["removed"]), {"release-1", "release-2"})

    def test_shared_data_and_legacy_are_preserved(self):
        shared = self.root / "shared"
        shared.mkdir()
        (shared / "jobs.db").write_text("preserve database")
        (self.releases / "release-0" / "data").symlink_to(shared, target_is_directory=True)
        (self.releases / "legacy").mkdir()
        cleanup_releases(self.root)
        self.assertEqual((shared / "jobs.db").read_text(), "preserve database")
        self.assertTrue((self.releases / "legacy").is_dir())

    def test_real_runtime_data_blocks_entire_plan(self):
        (self.releases / "release-0" / "data").mkdir()
        with self.assertRaisesRegex(ValueError, "real runtime data"):
            cleanup_releases(self.root)
        self.assertTrue((self.releases / "release-1").is_dir())
        self.assertTrue((self.releases / "release-0").is_dir())

    def test_only_stale_staging_is_removed(self):
        stale = self.releases / ".staging-example-1"
        fresh = self.releases / ".staging-example-2"
        stale.mkdir()
        fresh.mkdir()
        os.utime(stale, (time.time() - 90000,) * 2)
        cleanup_releases(self.root)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())

    @unittest.skipUnless(Path("/proc").is_dir(), "Linux process inspection")
    def test_running_worker_preserves_old_release(self):
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                  cwd=self.releases / "release-0")
        try:
            cleanup_releases(self.root)
            self.assertTrue((self.releases / "release-0").is_dir())
        finally:
            worker.terminate()
            worker.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
