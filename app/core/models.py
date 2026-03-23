from dataclasses import dataclass
from typing import Optional


@dataclass
class DownloadContext:
    """Strongly typed context for video download sessions."""

    page_url: str
    format_id: Optional[str] = None
    height: Optional[int] = None
    title: Optional[str] = None
    info_json_path: Optional[str] = None
    user_tag: Optional[str] = None
    chat_id: Optional[int] = None
    original_msg_id: Optional[int] = None
    api_source: Optional[str] = None
    api_json: Optional[dict] = None
    youtube_fallback: bool = False
