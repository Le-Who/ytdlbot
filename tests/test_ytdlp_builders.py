import unittest
from app.services.ytdlp.builders import build_command
from app.constants import GIF_FORMAT_ID


class TestYtDlpBuilders(unittest.TestCase):
    def test_build_command_video_with_height(self):
        """Test build_command for video with specified height."""
        page_url = "http://example.com/video"
        format_id = "best"
        height = 720
        output = "output.mp4"

        cmd = build_command(
            page_url=page_url,
            format_id=format_id,
            height=height,
            output=output
        )

        # Verify height-specific selectors
        self.assertIn("bestvideo[height=720]+(bestaudio[format_note*=original]/bestaudio[language^=en]/bestaudio[language^=orig]/bestaudio/bestaudio[ext=m4a]/bestaudio)/best[height=720]/bestvideo+bestaudio/best", cmd)
        self.assertIn("--output", cmd)
        self.assertIn(output, cmd)
        self.assertIn(page_url, cmd)

    def test_build_command_specific_format_id(self):
        """Test build_command for specific format_id without height."""
        page_url = "http://example.com/video"
        format_id = "137"
        height = None
        output = "output.mp4"

        cmd = build_command(
            page_url=page_url,
            format_id=format_id,
            height=height,
            output=output
        )

        # Verify format_id usage
        # Find format argument
        try:
            format_idx = cmd.index("--format")
            format_val = cmd[format_idx + 1]
        except ValueError:
            self.fail("--format flag not found in command")

        self.assertIn(format_id, format_val)
        self.assertIn(page_url, cmd)

    def test_build_command_gif_format(self):
        """Test build_command for GIF format."""
        page_url = "http://example.com/gif"
        format_id = GIF_FORMAT_ID
        height = None
        output = "output.mp4"

        cmd = build_command(
            page_url=page_url,
            format_id=format_id,
            height=height,
            output=output
        )

        # Verify GIF specific command parts
        self.assertIn("bestvideo[ext=mp4]/bestvideo/best[ext=mp4]/best", cmd)
        # Should not have audio selectors
        self.assertNotIn("bestaudio", cmd)
        self.assertIn(page_url, cmd)

    def test_build_command_audio_raw_format(self):
        """Test build_command for audio/raw format."""
        page_url = "http://example.com/audio"
        format_id = "bestaudio"
        height = None
        output = "output.mp3"

        cmd = build_command(
            page_url=page_url,
            format_id=format_id,
            height=height,
            output=output
        )

        # Verify audio format usage
        # Find format argument
        try:
            format_idx = cmd.index("--format")
            format_val = cmd[format_idx + 1]
        except ValueError:
            self.fail("--format flag not found in command")

        self.assertIn(format_id, format_val)
        self.assertIn(page_url, cmd)

    def test_build_command_with_cookies_and_max_filesize(self):
        """Test build_command with cookies and max_filesize."""
        page_url = "http://example.com/video"
        format_id = "best"
        height = 720
        output = "output.mp4"
        cookies_path = "/path/to/cookies.txt"
        max_filesize = 50

        cmd = build_command(
            page_url=page_url,
            format_id=format_id,
            height=height,
            output=output,
            cookies_path=cookies_path,
            max_filesize=max_filesize
        )

        self.assertIn("--cookies", cmd)
        self.assertIn(cookies_path, cmd)
        self.assertIn("--max-filesize", cmd)
        self.assertIn(f"{max_filesize}M", cmd)

    def test_build_command_aria2_usage(self):
        """Test build_command with aria2 enabled and installed."""
        page_url = "http://example.com/video"
        format_id = "best"
        height = 720
        output = "output.mp4"
        use_aria2 = True
        has_aria2_installed = True

        cmd = build_command(
            page_url=page_url,
            format_id=format_id,
            height=height,
            output=output,
            use_aria2=use_aria2,
            has_aria2_installed=has_aria2_installed
        )

        self.assertIn("--external-downloader", cmd)
        self.assertIn("aria2c", cmd)

    def test_build_command_aria2_disabled_or_not_installed(self):
        """Test build_command with aria2 disabled or not installed."""
        page_url = "http://example.com/video"
        format_id = "best"
        height = 720
        output = "output.mp4"

        # Case 1: use_aria2=True, has_aria2_installed=False
        cmd1 = build_command(
            page_url=page_url,
            format_id=format_id,
            height=height,
            output=output,
            use_aria2=True,
            has_aria2_installed=False
        )
        self.assertNotIn("--external-downloader", cmd1)
        self.assertNotIn("aria2c", cmd1)

        # Case 2: use_aria2=False, has_aria2_installed=True
        cmd2 = build_command(
            page_url=page_url,
            format_id=format_id,
            height=height,
            output=output,
            use_aria2=False,
            has_aria2_installed=True
        )
        self.assertNotIn("--external-downloader", cmd2)
        self.assertNotIn("aria2c", cmd2)

    def test_build_command_pipe_output(self):
        """Test build_command with output to pipe ('-')."""
        page_url = "http://example.com/video"
        format_id = "best"
        height = 720
        output = "-"
        use_aria2 = True
        has_aria2_installed = True

        cmd = build_command(
            page_url=page_url,
            format_id=format_id,
            height=height,
            output=output,
            use_aria2=use_aria2,
            has_aria2_installed=has_aria2_installed
        )

        # Should not use aria2 or progress bar when output is pipe
        self.assertNotIn("--external-downloader", cmd)
        self.assertNotIn("aria2c", cmd)
        self.assertNotIn("--progress", cmd)
        self.assertNotIn("--newline", cmd)
        self.assertIn(output, cmd)

if __name__ == "__main__":
    unittest.main()
