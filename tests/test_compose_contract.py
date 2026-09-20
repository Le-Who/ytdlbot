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
    init = services["volume-init"]
    assert init["user"] == "0:0"
    assert "media:/srv/ytdlbot/media" in init["volumes"]
    assert "state:/srv/ytdlbot/state" in init["volumes"]
    assert "bot_cache:/home/botuser/.cache/yt-dlp" in init["volumes"]
    assert init["command"] == [
        "chown -R 10001:10001 /srv/ytdlbot/media /srv/ytdlbot/state "
        "/home/botuser/.cache/yt-dlp"
    ]
    assert services["bot"]["depends_on"]["volume-init"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["tg-api"]["depends_on"]["volume-init"]["condition"] == (
        "service_completed_successfully"
    )


def test_compose_uses_versioned_images_and_readiness_healthcheck() -> None:
    compose = load_compose()
    services = compose["services"]

    for service in services.values():
        image = service.get("image")
        if image is not None:
            assert ":latest" not in image
    immutable_bot = "${BOT_IMAGE:?Set BOT_IMAGE to an immutable image@sha256:digest}"
    assert services["bot"]["image"] == immutable_bot
    assert services["volume-init"]["image"] == immutable_bot
    assert services["bgutil-pot"]["image"] == (
        "brainicism/bgutil-ytdlp-pot-provider:2.0.0@sha256:"
        "ed86b6fdd5e430ddd7c8ce1adb55e1ab54db7c7dbc1bcbf3a82454a85b971164"
    )
    assert services["tg-api"]["image"] == (
        "aiogram/telegram-bot-api:10.3@sha256:"
        "a299091598537528755c1245f54696598352f0d8ea00b1067087f522524029c2"
    )
    assert services["redis"]["image"] == (
        "redis:7.4.11-alpine@sha256:"
        "520775a41a63e77e06c73e35d2fd9cc15921a609516818796b4ecbb813078bc7"
    )
    assert services["bot"]["healthcheck"]["test"] == [
        "CMD",
        "curl",
        "--fail",
        "--silent",
        "http://localhost:8000/health/ready",
    ]
    assert "service_started" == services["bot"]["depends_on"]["tg-api"]["condition"]


def test_compose_stop_budget_exceeds_durable_drain_deadline() -> None:
    bot = load_compose()["services"]["bot"]
    environment = dict(item.split("=", 1) for item in bot["environment"])

    assert environment["DRAIN_TIMEOUT_SECONDS"] == "30"
    assert bot["stop_grace_period"] == "45s"


def test_runtime_dependencies_are_exactly_pinned_and_image_does_not_self_update() -> (
    None
):
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
    pinned_python = (
        "python:3.12.14-slim-bookworm@sha256:"
        "392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e"
    )
    assert dockerfile.count(f"FROM {pinned_python}") == 2
    assert "yt-dlp -U" not in dockerfile
    assert "yt_dlp -U" not in dockerfile
    assert "mkdir -p /srv/ytdlbot/media /srv/ytdlbot/state" in dockerfile
    assert "chown -R botuser:botuser /srv/ytdlbot" in dockerfile
    assert "groupadd --gid 10001 botuser" in dockerfile
    assert "useradd --uid 10001 --gid 10001" in dockerfile


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
