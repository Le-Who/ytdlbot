"""Tests for app.services.converter — MediaConverter."""

import unittest


class TestConvertToGifFfmpeg(unittest.IsolatedAsyncioTestCase):
    """Test MediaConverter.convert_to_gif_ffmpeg."""

    async def test_none_input_returns_none(self):
        from app.services.converter import MediaConverter

        result = await MediaConverter.convert_to_gif_ffmpeg(None)
        self.assertIsNone(result)

    async def test_empty_string_returns_none(self):
        from app.services.converter import MediaConverter

        result = await MediaConverter.convert_to_gif_ffmpeg("")
        self.assertIsNone(result)

    async def test_nonexistent_file_returns_none(self):
        from app.services.converter import MediaConverter

        result = await MediaConverter.convert_to_gif_ffmpeg("/nonexistent/path.mp4")
        self.assertIsNone(result)


class TestImagesToVideo(unittest.IsolatedAsyncioTestCase):
    """Test MediaConverter.images_to_video."""

    async def test_empty_images_returns_none(self):
        from app.services.converter import MediaConverter

        result = await MediaConverter.images_to_video([])
        self.assertIsNone(result)

    async def test_none_images_returns_none(self):
        from app.services.converter import MediaConverter

        result = await MediaConverter.images_to_video(None)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
