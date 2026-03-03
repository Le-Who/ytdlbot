class YtDlpError(Exception):
    """Base exception for yt-dlp service errors."""

    pass


class VideoNotFoundError(YtDlpError):
    """Raised when the video cannot be found (404)."""

    pass


class AccessDeniedError(YtDlpError):
    """Raised when access is denied (403/Forbidden)."""

    pass


class LiveStreamError(YtDlpError):
    """Raised when the video is a live stream."""

    pass


class ExtractionError(YtDlpError):
    """Raised for generic extraction errors."""

    pass


class DirectDownloadReady(YtDlpError):
    """Raised when content was downloaded directly (e.g. via gallery-dl fallback).

    Carries the video file path so the caller can send it immediately
    without going through format selection.
    """

    def __init__(self, video_path: str, title: str = "TikTok Video"):
        self.video_path = video_path
        self.title = title
        super().__init__(f"Direct download ready: {video_path}")
