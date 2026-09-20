from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core import resource_budget
from app.core.resource_budget import (
    DiskBudget,
    InsufficientDiskSpace,
    LeaseOwnershipError,
    is_active_media_lease,
    media_lease_lock,
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


@pytest.mark.asyncio
async def test_stale_reservation_cannot_renew_rebind_or_delete_new_owner_marker(
    tmp_path: Path,
) -> None:
    budget = DiskBudget(tmp_path, capacity_bytes=100)
    stale = await budget.reserve(10, owner="stale")
    media = tmp_path / "media_item.mp4.part"
    media.write_bytes(b"data")
    stale.bind(media)
    marker = Path(f"{media}.lease")
    marker.write_text(
        json.dumps({"owner": "new-owner", "expires_at": time.time() + 60}),
        encoding="utf-8",
    )

    with pytest.raises(LeaseOwnershipError):
        stale.renew()
    with pytest.raises(LeaseOwnershipError):
        stale.rebind(media, tmp_path / "media_final.mp4")
    await stale.release()

    assert json.loads(marker.read_text(encoding="utf-8"))["owner"] == "new-owner"


@pytest.mark.asyncio
async def test_release_drops_budget_when_lease_marker_is_concurrently_locked(
    tmp_path: Path,
) -> None:
    budget = DiskBudget(tmp_path, capacity_bytes=100)
    reservation = await budget.reserve(10, owner="request")
    media = tmp_path / "media_item.mp4.part"
    media.write_bytes(b"data")
    reservation.bind(media)
    lock = Path(f"{media}.lease.lock")
    lock.write_bytes(b"")

    await reservation.release()

    assert budget.reserved_bytes == 0
    assert lock.exists()


def test_configured_media_root_is_used_by_default_transport(
    tmp_path: Path,
) -> None:
    from unittest.mock import patch

    from app.services.media.transport import MediaTransport

    with patch("app.core.config.MEDIA_DIR", str(tmp_path)):
        media = MediaTransport()

    assert media.output_dir == tmp_path.resolve()


def test_janitor_scans_configured_media_root(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    media_root = tmp_path / "media"
    legacy.mkdir()
    media_root.mkdir()
    partial = media_root / "media_expired.mp4.part"
    partial.write_bytes(b"partial")
    old = time.time() - 100
    os.utime(partial, (old, old))

    with (
        patch("app.tasks.janitor.TEMP_DIR", str(legacy)),
        patch("app.tasks.janitor.MEDIA_DIR", str(media_root)),
        patch("app.tasks.janitor.MAX_TEMP_AGE_SECONDS", 10),
    ):
        deleted, orphan = janitor.cleanup_media_dirs()

    assert (deleted, orphan) == (1, 1)
    assert not partial.exists()


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


def test_aggressive_janitor_does_not_remove_in_progress_lease_lock(
    tmp_path: Path,
) -> None:
    target = tmp_path / "media_active.mp4.part"
    target.write_bytes(b"in-progress")
    lock = Path(f"{target}.lease.lock")
    lock.write_bytes(b"")

    with (
        patch("app.tasks.janitor.TEMP_DIR", str(tmp_path)),
        patch("app.tasks.janitor.MAX_TEMP_AGE_SECONDS", 10),
    ):
        deleted = janitor._aggressive_purge_temp()

    assert deleted == 0
    assert target.exists()
    assert lock.exists()


def test_aggressive_janitor_reclaims_target_with_expired_orphan_lock(
    tmp_path: Path,
) -> None:
    target = tmp_path / "media_expired.mp4.part"
    target.write_bytes(b"orphan")
    lock = Path(f"{target}.lease.lock")
    lock.write_bytes(b"")
    old = time.time() - 100
    os.utime(lock, (old, old))

    with (
        patch("app.tasks.janitor.TEMP_DIR", str(tmp_path)),
        patch("app.tasks.janitor.MAX_TEMP_AGE_SECONDS", 10),
    ):
        deleted = janitor._aggressive_purge_temp()

    assert deleted == 1
    assert not target.exists()
    assert not lock.exists()


def test_janitor_acquires_target_lock_after_enumeration_before_deleting(
    tmp_path: Path,
) -> None:
    target = tmp_path / "media_racing.mp4.part"
    target.write_bytes(b"in-progress")
    old = time.time() - 100
    os.utime(target, (old, old))
    lock_acquired = threading.Event()
    release_lock = threading.Event()
    holder: threading.Thread | None = None
    real_listdir = os.listdir

    def hold_binder_lock() -> None:
        with media_lease_lock(target):
            lock_acquired.set()
            release_lock.wait(timeout=1)

    def enumerate_then_bind(path: str | os.PathLike[str]) -> list[str]:
        nonlocal holder
        names = real_listdir(path)
        if holder is None:
            holder = threading.Thread(target=hold_binder_lock)
            holder.start()
            assert lock_acquired.wait(timeout=1)
        return names

    try:
        with (
            patch("app.tasks.janitor.TEMP_DIR", str(tmp_path)),
            patch("app.tasks.janitor.MAX_TEMP_AGE_SECONDS", 10),
            patch("app.tasks.janitor.os.listdir", side_effect=enumerate_then_bind),
        ):
            deleted, orphan = janitor.cleanup_temp_dir()
    finally:
        release_lock.set()
        if holder is not None:
            holder.join(timeout=1)

    assert (deleted, orphan) == (0, 0)
    assert target.exists()
    assert not Path(f"{target}.lease.lock").exists()


@pytest.mark.asyncio
async def test_promotion_marker_failure_preserves_source_lease_and_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget = DiskBudget(tmp_path, capacity_bytes=100)
    reservation = await budget.reserve(10, owner="request")
    partial = tmp_path / "media_item.mp4.part"
    final = tmp_path / "media_item.mp4"
    partial.write_bytes(b"data")
    reservation.bind(partial)
    real_replace = os.replace

    def fail_destination_marker(
        source: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> None:
        if Path(destination) == Path(f"{final}.lease"):
            raise OSError("marker write failed")
        real_replace(source, destination)

    monkeypatch.setattr(resource_budget.os, "replace", fail_destination_marker)

    with pytest.raises(OSError, match="marker write failed"):
        reservation.promote(partial, final)

    assert partial.exists()
    assert is_active_media_lease(partial)
    assert not final.exists()
    assert not Path(f"{final}.lease").exists()
    assert not list(tmp_path.glob("*.lease.*.tmp"))
    assert budget.reserved_bytes == 10
    await reservation.release()
    assert budget.reserved_bytes == 0


@pytest.mark.asyncio
async def test_promotion_rename_failure_removes_destination_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget = DiskBudget(tmp_path, capacity_bytes=100)
    reservation = await budget.reserve(10, owner="request")
    partial = tmp_path / "media_item.mp4.part"
    final = tmp_path / "media_item.mp4"
    partial.write_bytes(b"data")
    reservation.bind(partial)
    real_replace = os.replace

    def fail_media_rename(
        source: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> None:
        if Path(source) == partial and Path(destination) == final:
            raise OSError("rename failed")
        real_replace(source, destination)

    monkeypatch.setattr(resource_budget.os, "replace", fail_media_rename)

    with pytest.raises(OSError, match="rename failed"):
        reservation.promote(partial, final)

    assert partial.exists()
    assert is_active_media_lease(partial)
    assert not final.exists()
    assert not Path(f"{final}.lease").exists()
    assert not list(tmp_path.glob("*.lease.*.tmp"))
    assert budget.reserved_bytes == 10
    await reservation.release()
    assert budget.reserved_bytes == 0
