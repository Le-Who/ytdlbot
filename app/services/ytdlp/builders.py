import re

from app.constants import GIF_FORMAT_ID
from app.core.config import CONCURRENT_FRAGMENTS, POT_PROVIDER_URL

# Shared browser User-Agent for all yt-dlp requests.
# Must match across extraction and download to satisfy VK's anti-bot fingerprinting.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"
)


def _is_youtube_url(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url


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
        cookies_path: str | None = None,
        proxy: str | None = None,
        user_agent: str | None = None,
        timeout: int = 120,
        fallback_clients: bool = False,
    ) -> list[str]:
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

        # Optional configured PO-token service; yt-dlp chooses its supported clients.
        self._append_youtube_bypasses(cmd, url)

        self._append_network_opts(
            cmd,
            cookies_path,
            proxy,
            user_agent if user_agent is not None else _BROWSER_UA,
        )
        cmd.extend(["--", url])
        return cmd

    def build_download_cmd(
        self,
        url: str,
        format_id: str,
        output_path: str,
        height: int | None = None,
        cookies_path: str | None = None,
        proxy: str | None = None,
        user_agent: str | None = None,
        max_filesize_mb: int | None = None,
        use_aria2: bool = False,
        info_json_path: str | None = None,
        fallback_clients: bool = False,
        pipe_mode: bool = False,
        section: str | None = None,
        audio_language: str | None = None,
        audio_format: str | None = None,
    ) -> list[str]:
        """Build args for downloading media"""
        cmd = self._base_args.copy()

        # Format resolution logic
        is_gif_format = format_id == GIF_FORMAT_ID
        is_audio = format_id in ("bestaudio/best", "audio") or audio_format is not None
        if audio_language and not re.fullmatch(r"[\w-]+", audio_language):
            raise ValueError("invalid audio language")
        language = f"[language={audio_language}]" if audio_language else ""
        if is_gif_format:
            final_fmt = "bestvideo[ext=mp4]/bestvideo/best[ext=mp4]/best"
        elif is_audio:
            final_fmt = f"bestaudio{language}/best{language}"
            cmd.extend(["--extract-audio", "--audio-format", audio_format or "mp3"])
        else:
            # Short display edge, including portrait Shorts. Every alternative
            # enforces the same exact requested quality and requires audio.
            cap = height or 1080
            floor = f"[width>={cap}][height>={cap}]" if height else ""
            caps = (f"[height<={cap}]{floor}", f"[width<={cap}]{floor}")
            branches = []
            if format_id != "best":
                video_id, _, audio_id = format_id.partition("+")
                for limit in caps:
                    branches.append(
                        f"{video_id}{limit}[acodec=none]+{audio_id or 'bestaudio'}{language}"
                    )
                    if not audio_id:
                        branches.append(f"{video_id}{limit}[acodec!=none]{language}")
            for limit in caps:
                branches.extend(
                    [
                        f"bestvideo{limit}[vcodec^=avc]+bestaudio[acodec^=mp4a]{language}",
                        f"bestvideo{limit}+bestaudio{language}",
                        f"best{limit}[acodec!=none]{language}",
                    ]
                )
            final_fmt = "/".join(branches)

        self._append_youtube_bypasses(cmd, url)

        cmd.extend(
            [
                "--format",
                final_fmt,
                "--output",
                output_path,
                "--merge-output-format",
                "mp4",
                "--no-warnings",
                "--newline",
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
        if not pipe_mode and not is_gif_format and not is_audio:
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

        self._append_network_opts(
            cmd,
            cookies_path,
            proxy,
            user_agent if user_agent is not None else _BROWSER_UA,
        )

        # We use info_json cache for all platforms to avoid redundant extraction.
        # This is especially critical for VK because extracting the same URL twice
        # in 10 seconds triggers their anti-bot protection (badbrowser.php redirect).
        if section:
            cmd.extend(["--download-sections", section])

        if info_json_path:
            cmd.extend(["--load-info-json", info_json_path])
        else:
            cmd.extend(["--", url])

        return cmd

    def _append_network_opts(
        self,
        cmd: list[str],
        cookies_path: str | None,
        proxy: str | None,
        user_agent: str | None,
    ) -> None:
        if cookies_path:
            cmd.extend(["--cookies", cookies_path])
        if proxy:
            cmd.extend(["--proxy", proxy])
        if user_agent:
            cmd.extend(["--user-agent", user_agent])

    @staticmethod
    def _append_youtube_bypasses(cmd: list[str], url: str) -> None:
        """Inject the optional configured PO-token HTTP service for YouTube."""
        if not _is_youtube_url(url):
            return

        if POT_PROVIDER_URL:
            cmd.extend(
                [
                    "--extractor-args",
                    f"youtubepot-bgutilhttp:base_url={POT_PROVIDER_URL}",
                ]
            )
