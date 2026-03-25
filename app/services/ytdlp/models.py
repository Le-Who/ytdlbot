from dataclasses import dataclass
from typing import Optional, List


@dataclass(slots=True)
class FormatItem:
    """Представление формата видео"""

    format_id: str
    ext: str
    height: Optional[int]
    filesize: Optional[int]
    is_tiktok: bool = False
    protocol: str = ""
    format_note: str = ""


@dataclass
class ExtractionResult:
    title: str
    formats: List[FormatItem]
    special_format: Optional[FormatItem]
    duration_str: str
    is_slideshow: bool
    info_json_path: Optional[str]
    thumbnail_url: Optional[str]
    tiktok_auth_error: bool = False
    youtube_fallback: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "ExtractionResult":
        formats = [
            FormatItem(**f) if isinstance(f, dict) else f
            for f in data.get("formats", [])
        ]
        sf = data.get("special_format")
        if sf and isinstance(sf, dict):
            sf = FormatItem(**sf)

        d = dict(data)
        d["formats"] = formats
        d["special_format"] = sf
        return cls(**d)


@dataclass(slots=True)
class FormatMetadata:
    """Промежуточное представление формата видео без лейбла"""

    format_id: str
    ext: str
    height: Optional[int]
    filesize: Optional[int]
    protocol: str
    vcodec: str = "none"
    acodec: str = "none"
