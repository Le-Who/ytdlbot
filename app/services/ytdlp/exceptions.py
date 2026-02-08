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

class FormatError(YtDlpError):
    """Raised when formats are not available."""
    pass

class ExtractionError(YtDlpError):
    """Raised for generic extraction errors."""
    pass
