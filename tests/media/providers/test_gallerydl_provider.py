from __future__ import annotations

from app.services.media.models import MediaKind, MediaRequest
from app.services.media.providers.gallerydl import GalleryDlProvider


async def test_gallery_dl_preserves_mixed_album_order_and_headers():
    async def extract(url: str):
        assert url == "https://www.instagram.com/p/album/"
        return [
            [2, {"title": "Album"}],
            [
                3,
                "https://cdn.example/first.jpg",
                {"id": "first", "extension": "jpg"},
            ],
            [
                3,
                "https://cdn.example/second.mp4",
                {
                    "id": "second",
                    "extension": "mp4",
                    "http_headers": {"Referer": "https://www.instagram.com/"},
                },
            ],
        ]

    request = MediaRequest(
        canonical_url="https://www.instagram.com/p/album/",
        platform="instagram",
        media_id="album",
        kind=MediaKind.AUTO,
    )

    candidates = await GalleryDlProvider(extract=extract).resolve(request)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.kind is MediaKind.ALBUM
    assert tuple(item.kind for item in candidate.items) == (
        MediaKind.PHOTO,
        MediaKind.VIDEO,
    )
    assert tuple(item.media_id for item in candidate.items) == ("first", "second")
    assert candidate.sources[1].http_headers == (
        ("referer", "https://www.instagram.com/"),
    )


def test_gallery_dl_is_public_and_platform_scoped():
    provider = GalleryDlProvider(extract=None)

    assert provider.supports(
        MediaRequest(
            canonical_url="https://www.pinterest.com/pin/1/",
            platform="pinterest",
            media_id="1",
        )
    )
    assert not provider.supports(
        MediaRequest(
            canonical_url="https://www.youtube.com/watch?v=abc123",
            platform="youtube",
            media_id="abc123",
        )
    )
    assert not provider.supports(
        MediaRequest(
            canonical_url="https://www.instagram.com/p/one/",
            platform="instagram",
            media_id="one",
            auth_scope="instagram:session",
        )
    )
