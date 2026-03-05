import os
import tempfile
import time
import unittest
from unittest.mock import patch

from app.tasks import janitor

class TestJanitor(unittest.TestCase):
    def test_cleanup_temp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "ytdl_old.tmp")
            with open(p, "w", encoding="utf-8") as f:
                f.write("x")
            old = time.time() - 7200
            os.utime(p, (old, old))
            with patch("app.tasks.janitor.TEMP_DIR", d), patch("app.tasks.janitor.MAX_TEMP_AGE_SECONDS", 10):
                deleted, orphan = janitor.cleanup_temp_dir()
                self.assertEqual(deleted, 1)
                self.assertEqual(orphan, 1)
                self.assertFalse(os.path.exists(p))

    def test_skips_non_ytdl_files(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "other_process.tmp")
            with open(p, "w", encoding="utf-8") as f:
                f.write("x")
            old = time.time() - 7200
            os.utime(p, (old, old))
            with patch("app.tasks.janitor.TEMP_DIR", d), patch("app.tasks.janitor.MAX_TEMP_AGE_SECONDS", 10):
                deleted, orphan = janitor.cleanup_temp_dir()
                self.assertEqual(deleted, 0)
                self.assertTrue(os.path.exists(p))

if __name__ == "__main__":
    unittest.main()
