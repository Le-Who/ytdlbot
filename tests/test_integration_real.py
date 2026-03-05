"""Integration tests — require real yt-dlp and ffmpeg binaries.

Skipped by default in local runs (pytest addopts: -m 'not integration').
Run explicitly with:  pytest -m integration --no-cov
"""
import os
import shutil
import tempfile
import asyncio
import unittest

import pytest


def _has_binary(name: str) -> bool:
    return shutil.which(name) is not None


SKIP_NO_YTDLP = pytest.mark.skipif(
    not _has_binary("yt-dlp"), reason="yt-dlp not on PATH"
)
SKIP_NO_FFMPEG = pytest.mark.skipif(
    not _has_binary("ffmpeg"), reason="ffmpeg not on PATH"
)

# Short, public-domain / CC video (Big Buck Bunny 10s clip)
TEST_VIDEO_URL = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"  # Big Buck Bunny 60s


@pytest.mark.integration
@SKIP_NO_YTDLP
class TestYtdlpIntegration(unittest.IsolatedAsyncioTestCase):
    """Test real yt-dlp extraction (requires yt-dlp binary)."""

    async def test_list_formats_returns_data(self):
        """yt-dlp can list formats for a public YouTube video."""
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp", "--dump-json", "--skip-download", TEST_VIDEO_URL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        self.assertEqual(proc.returncode, 0, f"yt-dlp failed: {stderr.decode()[:200]}")

        import json
        info = json.loads(stdout.decode())
        self.assertIn("formats", info)
        self.assertGreater(len(info["formats"]), 0)
        self.assertIn("title", info)

    async def test_download_audio_only(self):
        """yt-dlp can download audio from a public YouTube video."""
        tmpdir = tempfile.mkdtemp(prefix="ytdlbot_test_")
        try:
            out_path = os.path.join(tmpdir, "audio.%(ext)s")
            proc = await asyncio.create_subprocess_exec(
                "yt-dlp", "-f", "bestaudio[ext=m4a]/bestaudio",
                "--max-filesize", "5M",
                "-o", out_path,
                TEST_VIDEO_URL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            self.assertEqual(proc.returncode, 0, f"yt-dlp failed: {stderr.decode()[:200]}")

            files = os.listdir(tmpdir)
            self.assertGreater(len(files), 0, "No audio file downloaded")
            self.assertGreater(
                os.path.getsize(os.path.join(tmpdir, files[0])), 1000,
                "Audio file too small"
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.mark.integration
@SKIP_NO_FFMPEG
class TestFfmpegIntegration(unittest.IsolatedAsyncioTestCase):
    """Test real ffmpeg conversion (requires ffmpeg binary)."""

    async def test_mute_mp4_conversion(self):
        """ffmpeg can strip audio from an MP4 (mute GIF path)."""
        tmpdir = tempfile.mkdtemp(prefix="ytdlbot_test_")
        try:
            # Create a minimal valid MP4 using ffmpeg
            src = os.path.join(tmpdir, "src.mp4")
            dst = os.path.join(tmpdir, "muted.mp4")

            # Generate 1-second test video with color source
            gen_proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "color=c=red:s=64x64:d=1",
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                "-t", "1", "-shortest",
                "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac",
                src,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(gen_proc.communicate(), timeout=15)
            self.assertEqual(gen_proc.returncode, 0, "Failed to generate test video")
            self.assertTrue(os.path.exists(src))

            # Mute it (strip audio, copy video)
            mute_proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y",
                "-i", src,
                "-c:v", "copy", "-an",
                "-movflags", "frag_keyframe+empty_moov",
                "-f", "mp4",
                dst,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(mute_proc.communicate(), timeout=10)
            self.assertEqual(mute_proc.returncode, 0, "ffmpeg mute failed")
            self.assertTrue(os.path.exists(dst))
            self.assertGreater(os.path.getsize(dst), 100, "Muted file too small")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_ffmpeg_version(self):
        """ffmpeg binary reports its version."""
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        self.assertIn(b"ffmpeg version", stdout)


if __name__ == "__main__":
    unittest.main()
