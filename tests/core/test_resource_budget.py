from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.resource_budget import (
    DiskBudget,
    InsufficientDiskSpace,
    is_active_media_lease,
)
from app.tasks import janitor


@pytest.mark.asyncio
async def test_disk_reservations_are_atomic_and_released(tmp_path: Path) -> None:
    budget = DiskBudget(tmp_path, capacity_bytes=100)
    first = await budget.reserve(70, owner="first")

    with pytest.raises(InsufficientDiskSpace):
        await budget.reserve(31, owner="second")

    await first.release()
    second = await budget.reserve(100, owner="second")
    assert budget.reserved_bytes == 100
    await second.release()
    assert budget.reserved_bytes == 0


@pytest.mark.asyncio
async def test_reservation_marks_every_bound_path_as_an_active_lease(
    tmp_path: Path,
) -> None:
    budget = DiskBudget(tmp_path, capacity_bytes=100)
    reservation = await budget.reserve(10, owner="request-1", lease_ttl=60)
    media = tmp_path / "media_item.mp4.part"
    media.write_bytes(b"data")
    reservation.bind(media)

    assert is_active_media_lease(media)
    marker = Path(f"{media}.lease")
    assert json.loads(marker.read_text(encoding="utf-8"))["owner"] == "request-1"

    await reservation.release()
    assert not marker.exists()


def test_janitor_skips_active_media_lease_and_reclaims_expired_partial(
    tmp_path: Path,
) -> None:
    active = tmp_path / "media_active.mp4.part"
    expired = tmp_path / "media_expired.mp4.part"
    active.write_bytes(b"active")
    expired.write_bytes(b"expired")
    old = time.time() - 100
    os.utime(active, (old, old))
    os.utime(expired, (old, old))
    Path(f"{active}.lease").write_text(
        json.dumps({"owner": "worker", "expires_at": time.time() + 60}),
        encoding="utf-8",
    )
    Path(f"{expired}.lease").write_text(
        json.dumps({"owner": "dead", "expires_at": time.time() - 1}),
        encoding="utf-8",
    )

    with (
        patch("app.tasks.janitor.TEMP_DIR", str(tmp_path)),
        patch("app.tasks.janitor.MAX_TEMP_AGE_SECONDS", 10),
    ):
        deleted, orphan = janitor.cleanup_temp_dir()

    assert active.exists()
    assert Path(f"{active}.lease").exists()
    assert not expired.exists()
    assert not Path(f"{expired}.lease").exists()
    assert (deleted, orphan) == (1, 1)


def test_aggressive_janitor_purge_still_preserves_active_lease(
    tmp_path: Path,
) -> None:
    active = tmp_path / "media_active.mp4"
    active.write_bytes(b"active")
    Path(f"{active}.lease").write_text(
        json.dumps({"owner": "sender", "expires_at": time.time() + 60}),
        encoding="utf-8",
    )

    with patch("app.tasks.janitor.TEMP_DIR", str(tmp_path)):
        deleted = janitor._aggressive_purge_temp()

    assert deleted == 0
    assert active.exists()
    assert Path(f"{active}.lease").exists()
