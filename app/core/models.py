from dataclasses import dataclass


@dataclass
class DownloadContext:
    """Strongly typed context for video download sessions."""

    page_url: str
    format_id: str | None = None
    height: int | None = None
    title: str | None = None
    info_json_path: str | None = None
    user_tag: str | None = None
    chat_id: int | None = None
    original_msg_id: int | None = None
    api_source: str | None = None
    api_json: dict | None = None
    youtube_fallback: bool = False
    section: str | None = None
