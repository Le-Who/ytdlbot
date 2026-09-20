from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_compose() -> dict[str, object]:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_compose_mounts_shared_media_and_separate_durable_state() -> None:
    compose = load_compose()
    services = compose["services"]

    assert "media:/srv/ytdlbot/media" in services["bot"]["volumes"]
    assert "state:/srv/ytdlbot/state" in services["bot"]["volumes"]
    assert "media:/srv/ytdlbot/media:ro" in services["tg-api"]["volumes"]
    assert "tg-api-data:/var/lib/telegram-bot-api" in services["tg-api"]["volumes"]
    assert "redis-data:/data" in services["redis"]["volumes"]
    assert {"media", "state", "tg-api-data", "redis-data", "bot_cache"} <= set(
        compose["volumes"]
    )


def test_compose_uses_versioned_images_and_readiness_healthcheck() -> None:
    compose = load_compose()
    services = compose["services"]

    for service in services.values():
        image = service.get("image")
        if image is not None:
            assert ":latest" not in image
    assert services["bot"]["image"] == "ghcr.io/${GHCR_REPO}:${APP_RELEASE}"
    assert services["bgutil-pot"]["image"].endswith(":2.0.0")
    assert services["tg-api"]["image"].endswith(":10.3")
    assert services["redis"]["image"].endswith(":7.4.11-alpine")
    assert services["bot"]["healthcheck"]["test"] == [
        "CMD",
        "curl",
        "--fail",
        "--silent",
        "http://localhost:8000/health/ready",
    ]
    assert "service_started" == services["bot"]["depends_on"]["tg-api"]["condition"]


def test_runtime_dependencies_are_exactly_pinned_and_image_does_not_self_update(
) -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    dependency_lines = [
        line.strip()
        for line in requirements.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert dependency_lines
    assert all("==" in line for line in dependency_lines)

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.12-slim" not in dockerfile
    assert "FROM python:3.12.14-slim-bookworm" in dockerfile
    assert "yt-dlp -U" not in dockerfile
    assert "yt_dlp -U" not in dockerfile
    assert "mkdir -p /srv/ytdlbot/media /srv/ytdlbot/state" in dockerfile
    assert "chown -R botuser:botuser /srv/ytdlbot" in dockerfile


def test_example_environment_selects_exact_decimal_2000_mb_local_profile() -> None:
    values = {}
    for raw_line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value

    assert values["APP_RELEASE"]
    assert values["MAX_MEDIA_FILE_MB"] == "2000"
    assert values["TELEGRAM_LOCAL_REQUIRED"] == "1"
    assert values["MEDIA_DIR"] == "/srv/ytdlbot/media"
    assert values["YTDLBOT_JOB_DB"] == "/srv/ytdlbot/state/jobs.sqlite3"
