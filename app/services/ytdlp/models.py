from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True)
class FormatItem:
    """Представление формата видео"""

    format_id: str
    label: str
    ext: str
    height: Optional[int]
    filesize: Optional[int]


@dataclass(slots=True)
class FormatMetadata:
    """Промежуточное представление формата видео без лейбла"""

    format_id: str
    ext: str
    height: Optional[int]
    filesize: Optional[int]
    protocol: str
