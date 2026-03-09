import unittest
from unittest.mock import AsyncMock

# Add repo root to path
from app.services.ytdlp.service import YtDlpService
from app.core.config import CONCURRENT_FRAGMENTS


class TestProfessionalRefinements(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = YtDlpService()

    async def test_live_stream_rejection(self):
        # Mock extract to return a live stream info
        self.service.extract = AsyncMock(
            return_value={"is_live": True, "title": "Live Video"}
        )

        with self.assertRaises(Exception) as cm:
            await self.service.list_formats("https://youtube.com/live/video")

        self.assertIn("прямая трансляция", str(cm.exception).lower())

    def test_concurrent_fragments_present(self):
        cmd = self.service.build_command("url", "best", 1080, "out")
        self.assertIn("--concurrent-fragments", cmd)
        self.assertIn(str(CONCURRENT_FRAGMENTS), cmd)
        self.assertIn("--no-playlist", cmd)


if __name__ == "__main__":
    unittest.main()
