"""Tests for app.tasks.janitor — cleanup_temp_dir and janitor_loop."""

import os
import time
import asyncio
import unittest
from unittest.mock import patch
from app.tasks.janitor import cleanup_temp_dir, janitor_loop


class TestCleanupTempDir(unittest.TestCase):
    """Test cleanup_temp_dir with a real temp directory."""

    def setUp(self):
        """Create a temp directory with test files."""
        import tempfile

        self.temp_dir = tempfile.mkdtemp()
        self._patches = []

        p1 = patch("app.tasks.janitor.TEMP_DIR", self.temp_dir)
        p2 = patch("app.tasks.janitor.MAX_TEMP_AGE_SECONDS", 10)
        self._patches.extend([p1, p2])
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_empty_dir_returns_zeros(self):
        deleted, orphan = cleanup_temp_dir()
        self.assertEqual(deleted, 0)
        self.assertEqual(orphan, 0)

    def test_deletes_old_ytdl_file(self):
        """Old ytdl_ file gets deleted."""
        path = os.path.join(self.temp_dir, "ytdl_test.mp4")
        with open(path, "w") as f:
            f.write("x")
        # Backdate the file
        old_time = time.time() - 100
        os.utime(path, (old_time, old_time))

        deleted, orphan = cleanup_temp_dir()
        self.assertEqual(deleted, 1)
        self.assertEqual(orphan, 1)
        self.assertFalse(os.path.exists(path))

    def test_keeps_recent_ytdl_file(self):
        """Recent ytdl_ file is kept."""
        path = os.path.join(self.temp_dir, "ytdl_new.mp4")
        with open(path, "w") as f:
            f.write("x")

        deleted, orphan = cleanup_temp_dir()
        self.assertEqual(deleted, 0)
        self.assertTrue(os.path.exists(path))

    def test_deletes_old_concat_file(self):
        """Old concat_ file gets deleted."""
        path = os.path.join(self.temp_dir, "concat_test.txt")
        with open(path, "w") as f:
            f.write("x")
        old_time = time.time() - 100
        os.utime(path, (old_time, old_time))

        deleted, orphan = cleanup_temp_dir()
        self.assertEqual(deleted, 1)

    def test_ignores_non_matching_files(self):
        """Files not matching ytdl_/concat_ prefix are ignored."""
        path = os.path.join(self.temp_dir, "random_file.txt")
        with open(path, "w") as f:
            f.write("x")
        old_time = time.time() - 100
        os.utime(path, (old_time, old_time))

        deleted, orphan = cleanup_temp_dir()
        self.assertEqual(deleted, 0)
        self.assertTrue(os.path.exists(path))

    def test_deletes_old_slideshow_dir(self):
        """Old slideshow_ directory gets deleted."""
        dir_path = os.path.join(self.temp_dir, "slideshow_test")
        os.makedirs(dir_path)
        # Put a file inside
        with open(os.path.join(dir_path, "img.jpg"), "w") as f:
            f.write("x")
        old_time = time.time() - 100
        os.utime(dir_path, (old_time, old_time))

        deleted, orphan = cleanup_temp_dir()
        self.assertEqual(deleted, 1)
        self.assertFalse(os.path.exists(dir_path))

    def test_nonexistent_temp_dir(self):
        """Nonexistent TEMP_DIR returns zeros without error."""
        with patch("app.tasks.janitor.TEMP_DIR", "/nonexistent/dir"):
            deleted, orphan = cleanup_temp_dir()
            self.assertEqual(deleted, 0)


class TestJanitorLoop(unittest.IsolatedAsyncioTestCase):
    """Test janitor_loop stops on event."""

    async def test_loop_stops_on_event(self):
        """janitor_loop exits when stop_event is set."""
        stop = asyncio.Event()
        stop.set()  # Immediately stop

        with patch("app.tasks.janitor.cleanup_temp_dir", return_value=(0, 0)):
            with patch("app.tasks.janitor.JANITOR_INTERVAL_SECONDS", 0.01):
                await janitor_loop(stop)  # Should return immediately


if __name__ == "__main__":
    unittest.main()
