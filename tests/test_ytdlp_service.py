import json
import subprocess
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.constants import GIF_FORMAT_ID
from app.services.ytdlp.builders import YtDlpCLIBuilder
from app.services.ytdlp.exceptions import AccessDeniedError
from app.services.ytdlp.service import YtDlpService


@pytest.mark.parametrize("height", [720, 1080])
def test_every_video_fallback_preserves_quality_and_sound(height):
    cmd = YtDlpCLIBuilder().build_download_cmd(
        "https://youtu.be/example", "137", "out.mp4", height=height
    )
    fmt = cmd[cmd.index("--format") + 1]
    for branch in fmt.split("/"):
        assert f"height<={height}" in branch or f"width<={height}" in branch
        assert "+bestaudio" in branch or "[acodec!=none]" in branch


@pytest.mark.parametrize("info_path", [None, "info.json"])
def test_clip_options_precede_source_arguments(info_path):
    cmd = YtDlpCLIBuilder().build_download_cmd(
        "https://youtu.be/example",
        "137",
        "out.mp4",
        height=1080,
        section="*10-20",
        info_json_path=info_path,
    )
    source_index = cmd.index("--load-info-json") if info_path else cmd.index("--")
    assert cmd.index("--download-sections") < source_index


def test_strict_mp3_command_selects_language_and_transcodes():
    cmd = YtDlpCLIBuilder().build_download_cmd(
        "https://youtu.be/example", "audio", "out.mp3", audio_language="uk"
    )
    assert "--extract-audio" in cmd
    assert cmd[cmd.index("--audio-format") + 1] == "mp3"
    assert all(
        "[language=uk]" in branch
        for branch in cmd[cmd.index("--format") + 1].split("/")
    )


async def test_youtube_403_list_formats_propagates_without_retry():
    service = YtDlpService()
    with patch.object(
        service, "extract", AsyncMock(side_effect=AccessDeniedError("403"))
    ) as extract:
        with pytest.raises(AccessDeniedError):
            await service.list_formats("https://youtu.be/example")
        assert extract.await_count == 1


class TestYtDlpService(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = YtDlpService()
        # Mock PlatformCookiesManager to return a deterministic cookies path
        self.service.cookies_manager.get_cookies_path = MagicMock(
            return_value="/tmp/cookies.txt"
        )
        self.service.cookies_manager._global_cookies_path = "/tmp/cookies.txt"

    def test_build_command_video(self):
        cmd = self.service.build_command(
            page_url="https://www.tiktok.com/@user/video/123",
            format_id="137+140",
            height=1080,
            output="/tmp/out.mp4",
        )
        self.assertTrue(any("yt-dlp" in arg or "yt_dlp" in arg for arg in cmd))
        # Check format string construction
        # We check substring
        fmt = cmd[cmd.index("--format") + 1]
        self.assertIn("137[height<=1080]", fmt)
        self.assertIn("+140", fmt)
        self.assertIn("/tmp/out.mp4", cmd)
        self.assertIn("--", cmd)
        self.assertIn("https://www.tiktok.com/@user/video/123", cmd)
        self.assertIn("--cookies", cmd)
        self.assertIn("/tmp/cookies.txt", cmd)

    def test_build_command_gif(self):
        cmd = self.service.build_command(
            page_url="http://pinterest.com/pin/123",
            format_id=GIF_FORMAT_ID,
            height=None,
            output="/tmp/out.mp4",
        )
        # Should select video only for GIF conversion
        expected_fmt = "bestvideo[ext=mp4]/bestvideo/best[ext=mp4]/best"
        self.assertIn(expected_fmt, cmd)

    def test_build_command_audio(self):
        cmd = self.service.build_command(
            page_url="https://www.tiktok.com/@user/video/456",
            format_id="bestaudio/best",
            height=None,
            output="/tmp/out.mp3",
        )
        self.assertIn("bestaudio/best", cmd)
        self.assertIn("--cookies", cmd)

    def test_build_command_no_aria2(self):
        # Verify aria2c is never in the command args
        cmd = self.service.build_command(
            page_url="http://example.com/video",
            format_id="best",
            height=720,
            output="/tmp/file.mp4",
        )
        self.assertFalse(any("aria2c" in arg for arg in cmd))

    async def test_list_formats_bypasses_extractor(self):
        with patch.object(
            self.service, "extract", new_callable=AsyncMock
        ) as mock_extract:
            # Test TikTok
            url_tiktok = "https://tiktok.com/@user/video/123"
            res_tiktok = await self.service.list_formats(url_tiktok)
            mock_extract.assert_not_called()
            self.assertEqual(len(res_tiktok.formats), 1)
            self.assertEqual(res_tiktok.formats[0].format_id, "tikwm_fallback")

            # Test Pinterest
            url_pin = "https://pinterest.com/pin/123"
            res_pin = await self.service.list_formats(url_pin)
            mock_extract.assert_not_called()
            self.assertEqual(len(res_pin.formats), 1)
            self.assertEqual(res_pin.formats[0].format_id, "pinterest_native")


if __name__ == "__main__":
    unittest.main()


@pytest.mark.parametrize(
    "width,height,edge,has_audio,expected",
    [
        (1920, 1080, 1080, True, "v+a"),
        (1280, 720, 720, True, "v+a"),
        (1080, 1920, 1080, True, "v+a"),
        (640, 360, 1080, True, None),
        (1920, 1080, 1080, False, None),
    ],
)
def test_selector_runs_in_real_ytdlp_without_downgrade_or_silent_video(
    width, height, edge, has_audio, expected
):
    cmd = YtDlpCLIBuilder().build_download_cmd(
        "https://youtu.be/example", "absent", "out.mp4", height=edge
    )
    formats = []
    if has_audio:
        formats.append(
            {
                "format_id": "a",
                "url": "https://cdn.example/a",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
            }
        )
    formats.append(
        {
            "format_id": "v",
            "url": "https://cdn.example/v",
            "ext": "mp4",
            "width": width,
            "height": height,
            "vcodec": "avc1.640028",
            "acodec": "none",
        }
    )
    script = """
import json, sys, yt_dlp
data = json.load(sys.stdin)
with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
    selected = list(ydl.build_format_selector(data['selector'])({'formats': data['formats'], 'incomplete_formats': False}))
print(json.dumps(selected[0]['format_id'] if selected else None))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(
            {"selector": cmd[cmd.index("--format") + 1], "formats": formats}
        ),
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == expected


def test_muxed_selected_id_cannot_override_requested_audio_language():
    cmd = YtDlpCLIBuilder().build_download_cmd(
        "https://youtu.be/example", "muxed", "out.mp4", height=1080, audio_language="uk"
    )
    formats = [
        {
            "format_id": "uk",
            "url": "https://cdn.example/uk",
            "ext": "m4a",
            "vcodec": "none",
            "acodec": "mp4a",
            "language": "uk",
        },
        {
            "format_id": "video",
            "url": "https://cdn.example/video",
            "ext": "mp4",
            "vcodec": "avc1",
            "acodec": "none",
            "width": 1920,
            "height": 1080,
        },
        {
            "format_id": "muxed",
            "url": "https://cdn.example/en",
            "ext": "mp4",
            "vcodec": "avc1",
            "acodec": "mp4a",
            "language": "en",
            "width": 1920,
            "height": 1080,
        },
    ]
    script = """
import json, sys, yt_dlp
data = json.load(sys.stdin)
with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
    selected = list(ydl.build_format_selector(data['selector'])({'formats': data['formats'], 'incomplete_formats': False}))
print(json.dumps(selected[0]['format_id'] if selected else None))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(
            {"selector": cmd[cmd.index("--format") + 1], "formats": formats}
        ),
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == "video+uk"
