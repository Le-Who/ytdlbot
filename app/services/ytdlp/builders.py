from typing import List, Optional
from app.constants import GIF_FORMAT_ID
from app.core.config import CONCURRENT_FRAGMENTS


class YtDlpCLIBuilder:
    """Consolidated builder for yt-dlp CLI arguments.
    Handles generic extraction, fallback extraction, and direct downloading."""

    def __init__(self):
        self._base_args = [
            "yt-dlp",
            "--force-ipv4",
            "--geo-bypass",
            "--ignore-config",
            "--no-warnings",
        ]

    def build_extraction_cmd(
        self,
        url: str,
        cookies_path: Optional[str] = None,
        proxy: Optional[str] = None,
        user_agent: Optional[str] = None,
        timeout: int = 120,
        fallback_clients: bool = False,
    ) -> List[str]:
        """Build args for dumping JSON info"""
        cmd = self._base_args.copy()
        cmd.extend(
            [
                "--dump-json",
                "--no-download",
                "--no-playlist",
                "--socket-timeout",
                str(timeout),
            ]
        )

        # TikTok specific edge case applied generically for strict JSON extraction
        cmd.extend(["--extractor-args", "tiktok:app_info="])

        if fallback_clients and ("youtube.com" in url or "youtu.be" in url):
            cmd.extend(["--extractor-args", "youtube:player_client=ios,android"])

        self._append_network_opts(cmd, cookies_path, proxy, user_agent)
        cmd.extend(["--", url])
        return cmd

    def build_download_cmd(
        self,
        url: str,
        format_id: str,
        output_path: str,
        height: Optional[int] = None,
        cookies_path: Optional[str] = None,
        proxy: Optional[str] = None,
        max_filesize_mb: Optional[int] = None,
        use_aria2: bool = False,
        info_json_path: Optional[str] = None,
        fallback_clients: bool = False,
        pipe_mode: bool = False,
    ) -> List[str]:
        """Build args for downloading media"""
        cmd = self._base_args.copy()

        # Format resolution logic
        is_gif_format = format_id == GIF_FORMAT_ID
        if is_gif_format:
            final_fmt = "bestvideo[ext=mp4]/bestvideo/best[ext=mp4]/best"
        elif format_id in ("bestaudio/best", "best", "audio"):
            final_fmt = "bestaudio/best" if format_id == "audio" else format_id
        else:
            height_cap = height or 1080
            # Prefer H.264 (avc1) for Telegram compatibility; fallback to any
            fallback = (
                f"bestvideo[height<={height_cap}][vcodec^=avc]+bestaudio[acodec^=mp4a]/"
                f"bestvideo[height<={height_cap}]+bestaudio/bestvideo+bestaudio/best"
            )
            final_fmt = f"{format_id}/{fallback}"

        if fallback_clients and ("youtube.com" in url or "youtu.be" in url):
            cmd.extend(["--extractor-args", "youtube:player_client=ios,android"])

        cmd.extend(
            [
                "--format",
                final_fmt,
                "--output",
                output_path,
                "--merge-output-format",
                "mp4",
                "--quiet",
                "--no-playlist",
                "--no-mtime",
                "--retries",
                "3",
                "--fragment-retries",
                "5",
                "--retry-sleep",
                "linear=1::2",
                "--concurrent-fragments",
                str(CONCURRENT_FRAGMENTS),
            ]
        )

        # Fragmented MP4 for pipe mode (streaming), faststart for file downloads
        if pipe_mode:
            cmd.extend(
                [
                    "--postprocessor-args",
                    "Merger+ffmpeg:-movflags frag_keyframe+empty_moov",
                ]
            )
        else:
            cmd.extend(
                [
                    "--postprocessor-args",
                    "Merger+ffmpeg:-movflags +faststart",
                ]
            )

        # Write thumbnail alongside download for zero-cost Telegram preview injection.
        # Skipped for: pipe mode (no file path), audio (irrelevant), GIF (no preview needed).
        if not pipe_mode and not is_gif_format and format_id not in ("bestaudio/best", "audio"):
            cmd.extend(["--write-thumbnail", "--convert-thumbnails", "jpg"])

        if output_path != "-" and use_aria2:
            cmd.extend(
                [
                    "--downloader",
                    "http:aria2c",
                    "--downloader-args",
                    "aria2c:-x 16 -s 16 -k 1M",
                ]
            )

        if max_filesize_mb:
            cmd.extend(["--max-filesize", f"{max_filesize_mb}M"])

        self._append_network_opts(cmd, cookies_path, proxy, None)

        if info_json_path:
            cmd.extend(["--load-info-json", info_json_path])
        else:
            cmd.extend(["--", url])

        return cmd

    def _append_network_opts(
        self,
        cmd: List[str],
        cookies_path: Optional[str],
        proxy: Optional[str],
        user_agent: Optional[str],
    ) -> None:
        if cookies_path:
            cmd.extend(["--cookies", cookies_path])
        if proxy:
            cmd.extend(["--proxy", proxy])
        if user_agent:
            cmd.extend(["--user-agent", user_agent])
