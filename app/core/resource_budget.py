"""Resource reservations and cross-process media leases."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path


class InsufficientDiskSpace(RuntimeError):
    """Raised before work starts when its disk requirement cannot be reserved."""


class LeaseOwnershipError(RuntimeError):
    """A stale reservation attempted to mutate another owner's lease."""


def media_lease_path(path: str | os.PathLike[str]) -> Path:
    return Path(f"{Path(path)}.lease")


@contextmanager
def media_lease_lock(
    path: str | os.PathLike[str],
    *,
    stale_after: float | None = None,
    wall_clock: Callable[[], float] = time.time,
) -> Iterator[None]:
    """Acquire the atomic lock used for every mutation of a target's lease."""
    with _marker_lock(
        media_lease_path(path),
        stale_after=stale_after,
        wall_clock=wall_clock,
    ):
        yield


def is_active_media_lease(
    path: str | os.PathLike[str], *, wall_clock: Callable[[], float] = time.time
) -> bool:
    """Return whether a valid, owned sidecar lease currently protects *path*."""
    marker = media_lease_path(path)
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        owner = payload.get("owner")
        expires_at = float(payload.get("expires_at", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    return bool(owner) and expires_at > wall_clock()


class DiskBudget:
    """Atomically account for in-process reservations against real free space."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        capacity_bytes: int | None = None,
        disk_usage: Callable[[str | os.PathLike[str]], shutil._ntuple_diskusage] = (
            shutil.disk_usage
        ),
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._capacity_bytes = capacity_bytes
        self._disk_usage = disk_usage
        self._reserved_bytes = 0
        self._lock = asyncio.Lock()

    @property
    def reserved_bytes(self) -> int:
        return self._reserved_bytes

    async def reserve(
        self,
        size_bytes: int,
        *,
        owner: str | None = None,
        lease_ttl: float = 3600,
    ) -> DiskReservation:
        if size_bytes < 0:
            raise ValueError("reservation size must not be negative")
        if lease_ttl <= 0:
            raise ValueError("lease TTL must be positive")
        async with self._lock:
            available = self._available_bytes()
            if size_bytes > available:
                raise InsufficientDiskSpace("insufficient disk space for media")
            self._reserved_bytes += size_bytes
        return DiskReservation(
            budget=self,
            size_bytes=size_bytes,
            owner=owner or uuid.uuid4().hex,
            lease_ttl=lease_ttl,
        )

    async def _grow(self, reservation: DiskReservation, new_size: int) -> None:
        async with self._lock:
            delta = new_size - reservation.size_bytes
            if delta <= 0:
                return
            if delta > self._available_bytes():
                raise InsufficientDiskSpace("insufficient disk space for media")
            self._reserved_bytes += delta
            reservation.size_bytes = new_size

    async def _release(self, size_bytes: int) -> None:
        async with self._lock:
            self._reserved_bytes = max(0, self._reserved_bytes - size_bytes)

    def _available_bytes(self) -> int:
        real_free = int(self._disk_usage(self.root).free)
        if self._capacity_bytes is not None:
            real_free = min(real_free, self._capacity_bytes)
        return max(0, real_free - self._reserved_bytes)


class DiskReservation:
    """A reservation that protects bound files with renewable lease sidecars."""

    def __init__(
        self,
        *,
        budget: DiskBudget,
        size_bytes: int,
        owner: str,
        lease_ttl: float,
    ) -> None:
        self.budget = budget
        self.size_bytes = size_bytes
        self.owner = owner
        self.lease_ttl = lease_ttl
        self._paths: set[Path] = set()
        self._released = False

    async def ensure(self, size_bytes: int) -> None:
        if self._released:
            raise RuntimeError("reservation already released")
        await self.budget._grow(self, size_bytes)

    def bind(self, path: str | os.PathLike[str]) -> None:
        if self._released:
            raise RuntimeError("reservation already released")
        resolved = self._resolve_path(path)
        self._write_marker(resolved)
        self._paths.add(resolved)

    def rebind(
        self, old_path: str | os.PathLike[str], new_path: str | os.PathLike[str]
    ) -> None:
        if self._released:
            raise RuntimeError("reservation already released")
        old = self._resolve_path(old_path)
        new = self._resolve_path(new_path)
        with _marker_locks(old, new):
            self._rebind_locked(old, new)

    def promote(
        self, old_path: str | os.PathLike[str], new_path: str | os.PathLike[str]
    ) -> None:
        """Atomically promote a bound partial while its destination is locked."""
        if self._released:
            raise RuntimeError("reservation already released")
        old = self._resolve_path(old_path)
        new = self._resolve_path(new_path)
        if old == new:
            raise ValueError("promotion paths must differ")
        with _marker_locks(old, new):
            if old not in self._paths:
                raise LeaseOwnershipError("promotion source is not bound")
            if new in self._paths:
                raise LeaseOwnershipError("promotion destination is already bound")
            self._assert_marker_owner_locked(old)
            renamed = False
            try:
                self._write_marker_locked(new)
                self._paths.add(new)
                os.replace(old, new)
                renamed = True
                self._remove_marker_if_owned_locked(old)
                self._paths.remove(old)
            except Exception:
                if not renamed and not old.exists() and new.exists():
                    renamed = True
                if renamed:
                    try:
                        os.replace(new, old)
                    except OSError:
                        # The destination still has our lease and remains tracked;
                        # request cleanup can safely remove it after this failure.
                        pass
                    else:
                        renamed = False
                        try:
                            self._write_marker_locked(old)
                            self._paths.add(old)
                        except OSError:
                            old.unlink(missing_ok=True)
                            self._remove_marker_if_owned_locked(old)
                            self._paths.discard(old)
                if not renamed:
                    self._remove_marker_if_owned_locked(new)
                    self._paths.discard(new)
                raise

    def renew(self) -> None:
        if self._released:
            raise RuntimeError("reservation already released")
        for path in self._paths:
            self._write_marker(path, require_owner=True)

    def unbind(self, path: str | os.PathLike[str]) -> None:
        resolved = Path(path).resolve()
        if resolved not in self._paths:
            return
        self._remove_marker_if_owned(resolved)
        self._paths.remove(resolved)

    async def release(self) -> None:
        if self._released:
            return
        try:
            for path in self._paths:
                try:
                    self._remove_marker_if_owned(path)
                except LeaseOwnershipError:
                    # Another owner/process is changing this marker. Leaving the
                    # sidecar for expiry is safer than deleting uncertain state.
                    continue
        finally:
            self._paths.clear()
            await self.budget._release(self.size_bytes)
            self._released = True

    def _write_marker(self, path: Path, *, require_owner: bool = False) -> None:
        marker = media_lease_path(path)
        with _marker_lock(marker):
            self._write_marker_locked(path, require_owner=require_owner)

    def _assert_marker_owner(self, path: Path) -> None:
        marker = media_lease_path(path)
        with _marker_lock(marker):
            self._assert_marker_owner_locked(path)

    def _remove_marker_if_owned(self, path: Path) -> None:
        marker = media_lease_path(path)
        with _marker_lock(marker):
            self._remove_marker_if_owned_locked(path)

    def _resolve_path(self, path: str | os.PathLike[str]) -> Path:
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self.budget.root):
            raise ValueError("leased path must remain under the media root")
        return resolved

    def _rebind_locked(self, old: Path, new: Path) -> None:
        if old not in self._paths:
            self._write_marker_locked(new)
            self._paths.add(new)
            return
        self._assert_marker_owner_locked(old)
        self._write_marker_locked(new)
        self._paths.add(new)
        try:
            self._remove_marker_if_owned_locked(old)
        except Exception:
            self._remove_marker_if_owned_locked(new)
            self._paths.discard(new)
            raise
        self._paths.remove(old)

    def _write_marker_locked(self, path: Path, *, require_owner: bool = False) -> None:
        marker = media_lease_path(path)
        current_owner, current_expiry = _marker_identity(marker)
        if (
            current_owner is not None
            and current_owner != self.owner
            and (require_owner or current_expiry > time.time())
        ):
            raise LeaseOwnershipError("media lease belongs to another owner")
        temporary = marker.with_name(f"{marker.name}.{self.owner}.tmp")
        payload = {
            "owner": self.owner,
            "expires_at": time.time() + self.lease_ttl,
        }
        try:
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(temporary, marker)
        finally:
            temporary.unlink(missing_ok=True)

    def _assert_marker_owner_locked(self, path: Path) -> None:
        owner, _ = _marker_identity(media_lease_path(path))
        if owner != self.owner:
            raise LeaseOwnershipError("media lease belongs to another owner")

    def _remove_marker_if_owned_locked(self, path: Path) -> None:
        marker = media_lease_path(path)
        owner, _ = _marker_identity(marker)
        if owner == self.owner:
            marker.unlink(missing_ok=True)


@contextmanager
def _marker_locks(*paths: Path) -> Iterator[None]:
    markers = sorted({media_lease_path(path) for path in paths}, key=os.fspath)
    with ExitStack() as stack:
        for marker in markers:
            stack.enter_context(_marker_lock(marker))
        yield


def _marker_identity(marker: Path) -> tuple[str | None, float]:
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        owner = payload.get("owner")
        expiry = float(payload.get("expires_at", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None, 0
    return (str(owner) if owner else None), expiry


@contextmanager
def _marker_lock(
    marker: Path,
    *,
    stale_after: float | None = None,
    wall_clock: Callable[[], float] = time.time,
) -> Iterator[None]:
    lock_path = Path(f"{marker}.lock")
    token = uuid.uuid4().hex
    descriptor: int | None = None
    for attempt in range(2):
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            if stale_after is None or attempt:
                raise LeaseOwnershipError("media lease is being updated") from error
            try:
                age = wall_clock() - lock_path.stat().st_mtime
            except FileNotFoundError:
                continue
            except OSError as stat_error:
                raise LeaseOwnershipError(
                    "media lease lock is unreadable"
                ) from stat_error
            if age <= stale_after:
                raise LeaseOwnershipError("media lease is being updated") from error
            try:
                lock_path.unlink()
            except FileNotFoundError:
                continue
            except OSError as unlink_error:
                raise LeaseOwnershipError(
                    "media lease lock is unavailable"
                ) from unlink_error
            continue
        try:
            os.write(descriptor, token.encode("ascii"))
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            descriptor = None
            lock_path.unlink(missing_ok=True)
            raise
        break
    if descriptor is None:
        raise LeaseOwnershipError("media lease is being updated")
    try:
        yield
    finally:
        os.close(descriptor)
        try:
            owned = lock_path.read_text(encoding="ascii") == token
        except OSError:
            owned = False
        if owned:
            lock_path.unlink(missing_ok=True)
