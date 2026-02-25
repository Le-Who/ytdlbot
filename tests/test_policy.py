import unittest
from unittest.mock import patch, MagicMock
import os
import sys
import importlib

class TestPolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Mock dotenv and set env vars BEFORE importing/reloading app.core.policy
        # to handle import-time logic in app.core.config
        cls.env_patcher = patch.dict(os.environ, {"BOT_TOKEN": "test_token"})
        cls.env_patcher.start()

        cls.dotenv_patcher = patch.dict(sys.modules, {"dotenv": MagicMock()})
        cls.dotenv_patcher.start()

        # Import (and ensure reload to pick up mocked env/modules) the module under test
        import app.core.policy
        importlib.reload(app.core.policy)
        cls.policy_module = app.core.policy

    @classmethod
    def tearDownClass(cls):
        cls.dotenv_patcher.stop()
        cls.env_patcher.stop()

    def test_size_allowed_none(self):
        """Test that size_allowed returns True when filesize_bytes is None."""
        self.assertTrue(self.policy_module.size_allowed(None))

    @patch("app.core.policy.MAX_TG_UPLOAD_MB", 50)
    def test_size_allowed_telegram(self):
        """Test size_allowed with target='telegram' using mocked MAX_TG_UPLOAD_MB."""
        limit_bytes = 50 * 1024 * 1024

        # Below limit
        self.assertTrue(self.policy_module.size_allowed(limit_bytes - 1, target="telegram"))
        # At limit
        self.assertTrue(self.policy_module.size_allowed(limit_bytes, target="telegram"))
        # Above limit
        self.assertFalse(self.policy_module.size_allowed(limit_bytes + 1, target="telegram"))

    @patch("app.core.policy.MAX_DL_MB", 100)
    def test_size_allowed_other(self):
        """Test size_allowed with target!='telegram' using mocked MAX_DL_MB."""
        limit_bytes = 100 * 1024 * 1024

        # Below limit
        self.assertTrue(self.policy_module.size_allowed(limit_bytes - 1, target="http"))
        # At limit
        self.assertTrue(self.policy_module.size_allowed(limit_bytes, target="http"))
        # Above limit
        self.assertFalse(self.policy_module.size_allowed(limit_bytes + 1, target="http"))

if __name__ == "__main__":
    unittest.main()
