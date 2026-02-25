import os
import unittest

# Set environment variables BEFORE importing app.main to avoid runtime errors during import
os.environ["BOT_TOKEN"] = "test_token"
os.environ["WEBHOOK_URL"] = "https://example.com"

from fastapi.testclient import TestClient
from app.main import api


class TestHealthEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api)

    def test_health_check(self):
        """Test the /health endpoint returns 200 and OK status."""
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})


if __name__ == "__main__":
    unittest.main()
