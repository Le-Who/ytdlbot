"""Disk pressure changes admission state and emits bounded admin alerts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.core import config, state
from app.tasks import janitor


async def test_warning_critical_and_recovery_change_admission_without_alert_spam(
    monkeypatch,
):
    monkeypatch.setattr(config, "DISK_WARNING_PCT", 10)
    monkeypatch.setattr(config, "DISK_CRITICAL_PCT", 5)
    monkeypatch.setattr(state, "disk_critical", False)
    monkeypatch.setattr(janitor, "_last_alert_level", 0)
    usage = Mock(
        side_effect=[
            SimpleNamespace(total=100, free=value) for value in (9, 8, 4, 3, 20, 9)
        ]
    )
    purge = Mock(return_value=2)
    notify = AsyncMock()
    monkeypatch.setattr(janitor.shutil, "disk_usage", usage)
    monkeypatch.setattr(janitor, "_aggressive_purge_media_dirs", purge)
    monkeypatch.setattr(janitor, "_notify_admin", notify)
    maintenance = []

    for _ in range(6):
        await janitor.check_disk_space()
        maintenance.append(state.disk_critical)

    assert maintenance == [False, False, True, True, False, False]
    assert purge.call_count == 2
    assert notify.await_count == 4
    assert "Disk Warning" in notify.await_args_list[0].args[0]
    assert "DISK CRITICAL" in notify.await_args_list[1].args[0]
    assert "Disk Recovered" in notify.await_args_list[2].args[0]
    assert "Disk Warning" in notify.await_args_list[3].args[0]


async def test_disk_probe_failure_preserves_maintenance_and_sends_no_false_recovery(
    monkeypatch,
):
    monkeypatch.setattr(state, "disk_critical", True)
    monkeypatch.setattr(
        janitor.shutil, "disk_usage", Mock(side_effect=OSError("disk unavailable"))
    )
    notify = AsyncMock()
    monkeypatch.setattr(janitor, "_notify_admin", notify)

    await janitor.check_disk_space()

    assert state.disk_critical is True
    notify.assert_not_awaited()
