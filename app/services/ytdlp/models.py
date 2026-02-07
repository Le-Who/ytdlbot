from dataclasses import dataclass
from typing import Optional

@dataclass
class FormatItem:
    """Представление формата видео"""
    format_id: str
    label: str
    ext: str
    height: Optional[int]
    filesize: Optional[int]
