class AppError(Exception):
    """Base exception for application errors."""
    pass

class DownloadError(AppError):
    """Base exception for download errors."""
    pass

class FileTooLargeError(DownloadError):
    """Raised when file size exceeds limits."""
    pass

class FormatNotAvailableError(DownloadError):
    """Raised when the requested format is not available."""
    pass

class AuthRequiredError(DownloadError):
    """Raised when the content requires authentication."""
    pass
