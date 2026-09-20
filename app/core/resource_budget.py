"""Resource reservations and cross-process media leases."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path


class InsufficientDiskSpace(RuntimeError):
    """Raised before work starts when its disk requirement cannot be reserved."""


def media_lease_path(path: str | os.PathLike[str]) -> Path:
    return Path(f"{Path(path)}.lease")


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
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self.budget.root):
            raise ValueError("leased path must remain under the media root")
        self._paths.add(resolved)
        self._write_marker(resolved)

    def rebind(
        self, old_path: str | os.PathLike[str], new_path: str | os.PathLike[str]
    ) -> None:
        old = Path(old_path).resolve()
        new = Path(new_path).resolve()
        if old in self._paths:
            self._paths.remove(old)
            media_lease_path(old).unlink(missing_ok=True)
        self.bind(new)

    def renew(self) -> None:
        if self._released:
            raise RuntimeError("reservation already released")
        for path in self._paths:
            self._write_marker(path)

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        for path in self._paths:
            media_lease_path(path).unlink(missing_ok=True)
        self._paths.clear()
        await self.budget._release(self.size_bytes)

    def _write_marker(self, path: Path) -> None:
        marker = media_lease_path(path)
        temporary = marker.with_name(f"{marker.name}.{self.owner}.tmp")
        payload = {
            "owner": self.owner,
            "expires_at": time.time() + self.lease_ttl,
        }
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary, marker)
