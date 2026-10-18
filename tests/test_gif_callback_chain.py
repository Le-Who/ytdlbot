"""The animation-to-native-GIF button chain retains its confirmed upload source."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from telegram import Animation, Chat, Message
from telegram.error import BadRequest, NetworkError
from test_callback_resource_regressions import gif_callback
from test_gif_local_source_binding import SentBot, export, send_source, sparse_source
from test_gif_local_source_binding import binding_env as binding_env  # noqa: PLC0414

from app.bot import callbacks
from app.core import config, state
from app.core.cache import FileTTLCache
from app.core.process import ProcessResult
from app.core.utils import safe_remove
from app.services import converter
from app.services.converter import MediaConverter


class ChainBot(SentBot):
    def __init__(self):
        super().__init__()
        self.animation_message = None
        self.animation_error = None
        self.native_button = None
        self.animation_messages = []

    async def send_animation(self, **kwargs):
        if self.animation_error is not None:
            raise self.animation_error
        self.native_button = kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        identity = len(self.animation_messages) + 1
        self.animation_message = Message(
            message_id=99,
            date=datetime(2026, 10, 18, tzinfo=UTC),
            chat=Chat(42, "private"),
            animation=Animation(
                f"animation-file-{identity}",
                f"animation-unique-{identity}",
                1,
                1,
                1,
                file_size=25_000_000,
            ),
        )
        self.animation_messages.append(self.animation_message)
        return self.animation_message


@pytest.fixture
def chain_env(binding_env, tmp_path, monkeypatch):
    env = binding_env
    env.bot = ChainBot()
    env.generated = tmp_path / "animation.mp4"
    env.original = tmp_path / "original.mp4"
    env.sentinel = tmp_path / "wrong-token.mp4"
    sparse_source(env.original)
    env.sentinel.write_bytes(b"unrelated old token source")
    env.clock = [0.0]
    cache = FileTTLCache(
        maxsize=2, ttl=60, timer=lambda: env.clock[0], on_eviction=safe_remove
    )
    cache["shared"] = str(env.sentinel)
    monkeypatch.setattr(state, "file_cache", cache)
    monkeypatch.setattr(config, "MEDIA_DIR", str(tmp_path))

    async def convert_animation(path):
        assert Path(path) == env.original
        sparse_source(env.generated, header=b"mute")
        return str(env.generated)

    async def convert_native(path):
        path = Path(path)
        env.conversions.append((path, path.stat().st_size))
        native = tmp_path / "native.gif"
        native.write_bytes(b"GIF89a")
        return str(native)

    monkeypatch.setattr(
        callbacks.MediaSender, "convert_to_gif_ffmpeg", convert_animation
    )
    monkeypatch.setattr(
        "app.services.converter.MediaConverter.convert_to_native_gif", convert_native
    )
    return env


@pytest.mark.parametrize("local_mode", [False, True], ids=["cloud", "local-api"])
async def test_confirmed_animation_source_supports_next_native_gif_button(
    chain_env, monkeypatch, local_mode
):
    env = chain_env
    monkeypatch.setattr(
        config, "TELEGRAM_LOCAL_ENDPOINT", "http://tg-api:8081" if local_mode else ""
    )
    assert (await send_source(env, env.original, local_mode=local_mode)).success
    await export(env, "animation")
    assert env.bot.native_button == "m2|giffile|shared"

    update, context = gif_callback("shared", env.bot)
    update.callback_query.message = env.bot.animation_message
    await callbacks.on_save_as_gif_file(update, context)

    assert env.conversions == [(env.generated, 25_000_000)]
    assert env.bot.outputs == ["document"]
    assert env.bot.get_attempts == []
    assert env.generated.exists()
    assert env.original.stat().st_size == 25_000_000
    assert env.sentinel.read_bytes() == b"unrelated old token source"
    assert state.file_cache["shared"] == str(env.sentinel)


@pytest.mark.parametrize("remove_by", ["expiry", "eviction"])
async def test_retained_animation_obeys_file_cache_owner_lifetime(chain_env, remove_by):
    env = chain_env
    assert (await send_source(env, env.original)).success
    await export(env, "animation")
    assert env.generated.exists()
    assert state.local_sent_media_source(env.bot.animation_message.animation) == str(
        env.generated
    )

    if remove_by == "expiry":
        env.clock[0] = 61
        state.file_cache.expire()
    else:
        state.file_cache["other-1"] = ""
        state.file_cache["other-2"] = ""

    assert not env.generated.exists()
    assert state.local_sent_media_source(env.bot.animation_message.animation) is None
    assert env.original.stat().st_size == 25_000_000


@pytest.mark.parametrize("error", [BadRequest("rejected"), NetworkError("uncertain")])
async def test_unsuccessful_animation_does_not_transfer_generated_file(
    chain_env, error
):
    env = chain_env
    assert (await send_source(env, env.original)).success
    env.bot.animation_error = error

    await export(env, "animation")

    assert not env.generated.exists()
    assert list(state.file_cache.items()) == [("shared", str(env.sentinel))]
    assert env.original.stat().st_size == 25_000_000


async def test_repeated_confirmed_animations_have_independent_cache_owners(
    chain_env, monkeypatch
):
    env = chain_env
    generated = []

    async def transform(command, **kwargs):
        output = Path(command[-1])
        generated.append(output)
        output.write_bytes(f"animation-{len(generated)}".encode())
        return ProcessResult(0, b"", b"")

    monkeypatch.setattr(converter, "_probe_video_codec", AsyncMock(return_value="h264"))
    monkeypatch.setattr(converter, "_run_transform_process", transform)
    monkeypatch.setattr(
        callbacks.MediaSender,
        "convert_to_gif_ffmpeg",
        MediaConverter.convert_to_gif_ffmpeg,
    )
    assert (await send_source(env, env.original)).success
    await export(env, "animation")
    await export(env, "animation")

    first, second = generated
    assert first != second
    assert first.read_bytes() == b"animation-1"
    assert second.read_bytes() == b"animation-2"
    first_owner = next(
        key for key, path in state.file_cache.items() if path == str(first)
    )
    state.file_cache.pop(first_owner)
    assert not first.exists()
    assert second.read_bytes() == b"animation-2"

    update, context = gif_callback("shared", env.bot)
    update.callback_query.message = env.bot.animation_messages[1]
    await callbacks.on_save_as_gif_file(update, context)

    assert env.conversions == [(second, len(b"animation-2"))]
    assert env.bot.get_attempts == []
    assert second.exists()
