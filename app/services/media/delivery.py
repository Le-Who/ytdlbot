"""Telegram delivery with explicit outcomes and Local Bot API path handling."""

from __future__ import annotations

import io
import logging
import os
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, TypeAlias

from telegram import (
    Bot,
    InputFile,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)
from telegram.error import BadRequest, NetworkError, RetryAfter

from app.core.job_store import (
    DeliveryOutcome,
    begin_current_delivery,
    current_delivery_outcome,
    record_current_delivery,
)
from app.core.policy import DECIMAL_MB

from .models import (
    DeliveredItem,
    DeliveryReceipt,
    DeliveryStatus,
    DeliveryTarget,
    MediaItem,
    MediaKind,
    MediaRequest,
)
from .transport import MaterializedItem

if TYPE_CHECKING:
    from app.core.media_cache import CachedDelivery, MediaCache

MAX_TELEGRAM_ALBUM_SIZE = 10
logger = logging.getLogger("app.services.media.delivery")

MediaSource: TypeAlias = str | os.PathLike[str] | BinaryIO | io.BytesIO


@dataclass(frozen=True, slots=True)
class DeliveryAsset:
    """One ordered Telegram output and its optional upload source."""

    item: MediaItem
    source: MediaSource | None
    item_index: int | None = None
    size_bytes: int | None = None

    def __post_init__(self) -> None:
        if self.item_index is not None and self.item_index < 0:
            raise ValueError("delivery item index must not be negative")
        if self.size_bytes is not None and self.size_bytes < 0:
            raise ValueError("delivery size must not be negative")


@dataclass(frozen=True, slots=True)
class _PreparedAsset:
    asset: DeliveryAsset
    source: MediaSource
    cached: CachedDelivery | None = None


class TelegramDelivery:
    """Deliver ordered media once and return enough evidence for safe recovery."""

    def __init__(
        self,
        bot: Bot,
        *,
        media_dir: str | os.PathLike[str] | None = None,
        local_mode: bool | None = None,
        media_cache: MediaCache | None = None,
        bot_id: str | None = None,
        max_media_bytes: int | None = None,
        cloud_max_bytes: int | None = None,
        media_write_timeout: float | None = None,
        read_timeout: float | None = None,
        write_timeout: float | None = None,
        connect_timeout: float | None = None,
        pool_timeout: float | None = None,
    ) -> None:
        from app.core import config

        self.bot = bot
        self.media_dir = Path(media_dir or config.MEDIA_DIR).resolve()
        self.local_mode = bool(
            config.TELEGRAM_LOCAL_ENDPOINT if local_mode is None else local_mode
        )
        self.media_cache = media_cache
        self.bot_id = bot_id
        self.max_media_bytes = (
            config.MAX_MEDIA_FILE_MB * DECIMAL_MB
            if max_media_bytes is None
            else max_media_bytes
        )
        self.cloud_max_bytes = (
            config.TELEGRAM_CLOUD_MAX_FILE_MB * DECIMAL_MB
            if cloud_max_bytes is None
            else cloud_max_bytes
        )
        self.media_write_timeout = (
            config.TELEGRAM_MEDIA_WRITE_TIMEOUT
            if media_write_timeout is None
            else media_write_timeout
        )
        self.read_timeout = (
            config.TELEGRAM_READ_TIMEOUT if read_timeout is None else read_timeout
        )
        self.write_timeout = (
            config.TELEGRAM_WRITE_TIMEOUT if write_timeout is None else write_timeout
        )
        self.connect_timeout = (
            config.TELEGRAM_CONNECT_TIMEOUT
            if connect_timeout is None
            else connect_timeout
        )
        self.pool_timeout = (
            config.TELEGRAM_POOL_TIMEOUT if pool_timeout is None else pool_timeout
        )
        if self.max_media_bytes <= 0 or self.cloud_max_bytes <= 0:
            raise ValueError("delivery byte limits must be positive")

    async def send(
        self,
        media: MaterializedItem | DeliveryAsset | Sequence[DeliveryAsset],
        target: DeliveryTarget,
        **kwargs: Any,
    ) -> DeliveryReceipt:
        """Compatibility spelling for callers that used ``send`` in prototypes."""
        return await self.deliver(media, target, **kwargs)

    async def deliver(
        self,
        media: MaterializedItem | DeliveryAsset | Sequence[DeliveryAsset],
        target: DeliveryTarget,
        *,
        request: MediaRequest | None = None,
        caption: str = "",
        parse_mode: str | None = None,
        reply_markup: Any = None,
        reply_parameters: Any = None,
        duration: int | None = None,
        width: int | None = None,
        height: int | None = None,
        has_spoiler: bool = False,
        thumbnail: MediaSource | None = None,
        file_name: str | None = None,
    ) -> DeliveryReceipt:
        assets = self._normalize_assets(media)
        if not assets:
            return DeliveryReceipt(
                target=target, items=(), status=DeliveryStatus.FAILED
            )

        options = {
            "caption": caption,
            "parse_mode": parse_mode,
            "reply_markup": reply_markup,
            "reply_parameters": reply_parameters,
            "duration": duration,
            "width": width,
            "height": height,
            "has_spoiler": has_spoiler,
            "thumbnail": thumbnail,
            "file_name": file_name,
        }
        results: dict[str, DeliveredItem] = {}
        pending_assets: list[DeliveryAsset] = []
        for asset in assets:
            item_key = _durable_item_key(target, asset)
            prior = await current_delivery_outcome(item_key)
            if prior is DeliveryOutcome.SUCCESS:
                results[item_key] = DeliveredItem(
                    item=asset.item,
                    status=DeliveryStatus.DELIVERED,
                    telegram_type=asset.item.kind,
                    item_index=asset.item_index or 0,
                )
            elif prior is DeliveryOutcome.UNCERTAIN:
                results[item_key] = DeliveredItem(
                    item=asset.item,
                    status=DeliveryStatus.UNCERTAIN,
                    telegram_type=asset.item.kind,
                    item_index=asset.item_index or 0,
                    error="Telegram send outcome was uncertain before restart",
                    error_category="recovered_unknown_outcome",
                )
            else:
                pending_assets.append(asset)

        units = _delivery_units(tuple(pending_assets))
        for unit_index, unit in enumerate(units):
            await begin_current_delivery(
                tuple(_durable_item_key(target, asset) for asset in unit)
            )
            group_options = dict(options)
            if unit_index or results:
                group_options["caption"] = ""
                group_options["parse_mode"] = None
                group_options["reply_parameters"] = None
            delivered: tuple[DeliveredItem, ...]
            if len(unit) == 1:
                delivered = (
                    await self._deliver_one(
                        unit[0], target, request=request, options=group_options
                    ),
                )
            else:
                delivered = await self._deliver_group(
                    unit,
                    target,
                    request=request,
                    options=group_options,
                )
            for asset, item in zip(unit, delivered, strict=True):
                item_key = _durable_item_key(target, asset)
                await record_current_delivery(
                    item_key,
                    _durable_outcome(item.status),
                    delivery_id=item.delivery_id,
                )
                results[item_key] = item
        return self._receipt(
            target,
            tuple(results[_durable_item_key(target, asset)] for asset in assets),
        )

    async def retry_failed(
        self,
        receipt: DeliveryReceipt,
        media: MaterializedItem | DeliveryAsset | Sequence[DeliveryAsset],
        target: DeliveryTarget,
        **kwargs: Any,
    ) -> DeliveryReceipt:
        """Retry only items whose prior outcome proves that nothing was sent."""
        failed_indexes = {item.item_index for item in receipt.retryable_items}
        retry_assets = [
            asset
            for asset in self._normalize_assets(media)
            if asset.item_index in failed_indexes
        ]
        return await self.deliver(retry_assets, target, **kwargs)

    async def _deliver_one(
        self,
        asset: DeliveryAsset,
        target: DeliveryTarget,
        *,
        request: MediaRequest | None,
        options: dict[str, Any],
    ) -> DeliveredItem:
        prepared = await self._prepare(asset, request=request)
        if isinstance(prepared, DeliveredItem):
            return prepared
        try:
            message = await self._send_single(prepared, target, options)
        except RetryAfter as error:
            return self._failure(asset, DeliveryStatus.FAILED, error, "rate_limited")
        except BadRequest as error:
            if prepared.cached is not None and _is_invalid_file_id(error):
                await self._evict(request, asset)
                upload = await self._prepare(asset, request=request, force_upload=True)
                if isinstance(upload, DeliveredItem):
                    return upload
                try:
                    message = await self._send_single(upload, target, options)
                except RetryAfter as retry_error:
                    return self._failure(
                        asset, DeliveryStatus.FAILED, retry_error, "rate_limited"
                    )
                except BadRequest as retry_error:
                    return self._failure(
                        asset, DeliveryStatus.FAILED, retry_error, "telegram_rejected"
                    )
                except NetworkError as retry_error:
                    return self._failure(
                        asset,
                        DeliveryStatus.UNCERTAIN,
                        retry_error,
                        "unknown_outcome",
                    )
                except Exception as retry_error:  # noqa: BLE001 - receipt boundary
                    return self._failure(
                        asset, DeliveryStatus.FAILED, retry_error, "send_failed"
                    )
            else:
                return self._failure(
                    asset, DeliveryStatus.FAILED, error, "telegram_rejected"
                )
        except NetworkError as error:
            return self._failure(
                asset, DeliveryStatus.UNCERTAIN, error, "unknown_outcome"
            )
        except Exception as error:  # noqa: BLE001 - receipt boundary
            return self._failure(asset, DeliveryStatus.FAILED, error, "send_failed")
        delivered = self._success(asset, message)
        await self._store(request, delivered)
        return delivered

    async def _deliver_group(
        self,
        assets: Sequence[DeliveryAsset],
        target: DeliveryTarget,
        *,
        request: MediaRequest | None,
        options: dict[str, Any],
        force_upload: bool = False,
    ) -> tuple[DeliveredItem, ...]:
        prepared: list[_PreparedAsset] = []
        failures: dict[int, DeliveredItem] = {}
        for asset in assets:
            result = await self._prepare(
                asset, request=request, force_upload=force_upload
            )
            if isinstance(result, DeliveredItem):
                failures[asset.item_index or 0] = result
            else:
                prepared.append(result)
        if failures:
            return tuple(
                failures.get(asset.item_index or 0)
                or self._failure(
                    asset,
                    DeliveryStatus.FAILED,
                    RuntimeError("album contains an unavailable item"),
                    "album_incomplete",
                )
                for asset in assets
            )

        try:
            messages = await self._send_group(prepared, target, options)
        except RetryAfter as error:
            return tuple(
                self._failure(asset, DeliveryStatus.FAILED, error, "rate_limited")
                for asset in assets
            )
        except BadRequest as error:
            if (
                not force_upload
                and any(item.cached is not None for item in prepared)
                and _is_invalid_file_id(error)
            ):
                for item in prepared:
                    if item.cached is not None:
                        await self._evict(request, item.asset)
                return await self._deliver_group(
                    assets,
                    target,
                    request=request,
                    options=options,
                    force_upload=True,
                )
            return tuple(
                self._failure(asset, DeliveryStatus.FAILED, error, "telegram_rejected")
                for asset in assets
            )
        except NetworkError as error:
            return tuple(
                self._failure(asset, DeliveryStatus.UNCERTAIN, error, "unknown_outcome")
                for asset in assets
            )
        except Exception as error:  # noqa: BLE001 - receipt boundary
            return tuple(
                self._failure(asset, DeliveryStatus.FAILED, error, "send_failed")
                for asset in assets
            )

        results: list[DeliveredItem] = []
        for position, asset in enumerate(assets):
            if position >= len(messages):
                results.append(
                    self._failure(
                        asset,
                        DeliveryStatus.UNCERTAIN,
                        RuntimeError("Telegram returned an incomplete album receipt"),
                        "unknown_outcome",
                    )
                )
                continue
            delivered = self._success(asset, messages[position])
            await self._store(request, delivered)
            results.append(delivered)
        return tuple(results)

    async def _prepare(
        self,
        asset: DeliveryAsset,
        *,
        request: MediaRequest | None,
        force_upload: bool = False,
    ) -> _PreparedAsset | DeliveredItem:
        cache = self.media_cache
        bot_id = (
            self._cache_bot_id() if cache is not None and request is not None else None
        )
        if not force_upload and cache is not None and request is not None and bot_id:
            try:
                cached = await cache.get_delivery(
                    request, bot_id=bot_id, item_index=asset.item_index or 0
                )
            except Exception:  # noqa: BLE001 - optional cache boundary
                self._log_cache_failure("get", asset)
                cached = None
            if cached is not None:
                try:
                    if str(cached.telegram_type) == asset.item.kind.value:
                        return _PreparedAsset(asset, cached.file_id, cached)
                except Exception:  # noqa: BLE001 - malformed optional cache record
                    self._log_cache_failure("decode", asset)
                await self._evict(request, asset)

        if asset.source is None:
            return self._failure(
                asset,
                DeliveryStatus.FAILED,
                RuntimeError("no upload source is available"),
                "missing_source",
            )
        try:
            size = self._asset_size(asset)
        except OSError as error:
            return self._failure(asset, DeliveryStatus.FAILED, error, "missing_source")
        limit = self.max_media_bytes
        if not self.local_mode:
            limit = min(limit, self.cloud_max_bytes)
        if size is not None and size > limit:
            return self._failure(
                asset,
                DeliveryStatus.FAILED,
                ValueError(f"media exceeds the {limit}-byte delivery limit"),
                "file_too_large",
            )
        return _PreparedAsset(asset, asset.source)

    async def _send_single(
        self,
        prepared: _PreparedAsset,
        target: DeliveryTarget,
        options: dict[str, Any],
    ) -> Any:
        kind = prepared.asset.item.kind
        method_name, argument_name = _send_method(kind)
        method = getattr(self.bot, method_name)
        with self._open_source(prepared) as media:
            kwargs: dict[str, Any] = {
                "chat_id": _chat_id(target.destination),
                argument_name: media,
                "caption": options["caption"],
                "parse_mode": options["parse_mode"],
                "reply_markup": options["reply_markup"],
                "reply_parameters": options["reply_parameters"],
                "read_timeout": self.read_timeout,
                "write_timeout": self.media_write_timeout,
                "connect_timeout": self.connect_timeout,
                "pool_timeout": self.pool_timeout,
            }
            if kind in {MediaKind.VIDEO, MediaKind.ANIMATION}:
                kwargs.update(
                    duration=options["duration"],
                    width=options["width"],
                    height=options["height"],
                    has_spoiler=options["has_spoiler"],
                    thumbnail=options["thumbnail"],
                    filename=options["file_name"],
                )
            elif kind is MediaKind.AUDIO:
                kwargs.update(
                    duration=options["duration"],
                    thumbnail=options["thumbnail"],
                    filename=options["file_name"],
                )
            elif kind is MediaKind.PHOTO:
                kwargs["has_spoiler"] = options["has_spoiler"]
            elif kind is MediaKind.DOCUMENT:
                kwargs.update(
                    thumbnail=options["thumbnail"], filename=options["file_name"]
                )
            if kind is MediaKind.VIDEO:
                kwargs["supports_streaming"] = True
            return await method(**kwargs)

    async def _send_group(
        self,
        prepared: Sequence[_PreparedAsset],
        target: DeliveryTarget,
        options: dict[str, Any],
    ) -> Sequence[Any]:
        with ExitStack() as stack:
            media = []
            for position, item in enumerate(prepared):
                source = stack.enter_context(self._open_source(item))
                media.append(
                    _input_media(
                        item.asset.item.kind,
                        source,
                        caption=options["caption"] if position == 0 else None,
                        parse_mode=options["parse_mode"] if position == 0 else None,
                    )
                )
            result = await self.bot.send_media_group(
                chat_id=_chat_id(target.destination),
                media=media,
                reply_parameters=options["reply_parameters"],
                read_timeout=self.read_timeout,
                write_timeout=self.media_write_timeout,
                connect_timeout=self.connect_timeout,
                pool_timeout=self.pool_timeout,
            )
        return tuple(result)

    @contextmanager
    def _open_source(self, prepared: _PreparedAsset) -> Iterator[Any]:
        source = prepared.source
        if prepared.cached is not None:
            yield source
            return
        if isinstance(source, os.PathLike) or (
            isinstance(source, str) and not source.startswith(("http://", "https://"))
        ):
            path = Path(source)
        else:
            if hasattr(source, "read"):
                yield InputFile(source, read_file_handle=False)
            else:
                yield source
            return
        resolved = path.resolve(strict=True)
        if self.local_mode and _is_relative_to(resolved, self.media_dir):
            yield f"file://{resolved}"
            return
        with resolved.open("rb") as handle:
            yield InputFile(
                handle,
                filename=resolved.name,
                read_file_handle=False,
            )

    def _asset_size(self, asset: DeliveryAsset) -> int | None:
        if asset.size_bytes is not None:
            return asset.size_bytes
        source = asset.source
        if isinstance(source, os.PathLike):
            return Path(source).stat().st_size
        if isinstance(source, str) and not source.startswith(("http://", "https://")):
            return Path(source).stat().st_size
        if isinstance(source, io.BytesIO):
            return source.getbuffer().nbytes
        return None

    def _normalize_assets(
        self, media: MaterializedItem | DeliveryAsset | Sequence[DeliveryAsset]
    ) -> tuple[DeliveryAsset, ...]:
        raw_assets: tuple[DeliveryAsset, ...]
        if isinstance(media, DeliveryAsset):
            raw_assets = (media,)
        elif isinstance(media, MaterializedItem):
            raw_assets = self._from_materialized(media)
        else:
            raw_assets = tuple(media)
        return tuple(
            asset
            if asset.item_index is not None
            else DeliveryAsset(
                item=asset.item,
                source=asset.source,
                item_index=index,
                size_bytes=asset.size_bytes,
            )
            for index, asset in enumerate(raw_assets)
        )

    def _from_materialized(self, media: MaterializedItem) -> tuple[DeliveryAsset, ...]:
        candidate_items = media.candidate.items
        assets: list[DeliveryAsset] = []
        for index, path in enumerate(media.paths):
            if len(candidate_items) == len(media.paths):
                item = candidate_items[index]
            else:
                item = MediaItem(
                    media_id=(media.candidate.media_id or media.candidate.candidate_id)
                    + (f":{index}" if len(media.paths) > 1 else ""),
                    kind=media.candidate.kind or MediaKind.VIDEO,
                    url=media.candidate.url,
                    width=media.candidate.width,
                    height=media.candidate.height,
                    container=media.candidate.container,
                    filesize_bytes=(
                        media.size_bytes
                        if len(media.paths) == 1
                        else path.stat().st_size
                    ),
                )
            assets.append(
                DeliveryAsset(
                    item=item,
                    source=path,
                    item_index=index,
                    size_bytes=(
                        media.size_bytes
                        if len(media.paths) == 1
                        else path.stat().st_size
                    ),
                )
            )
        return tuple(assets)

    def _success(self, asset: DeliveryAsset, message: Any) -> DeliveredItem:
        media = _message_media(message, asset.item.kind)
        message_id = getattr(message, "message_id", None)
        file_id = getattr(media, "file_id", None)
        file_unique_id = getattr(media, "file_unique_id", None)
        return DeliveredItem(
            item=asset.item,
            status=DeliveryStatus.SUCCESS,
            telegram_type=asset.item.kind,
            item_index=asset.item_index or 0,
            message_id=message_id if isinstance(message_id, int) else None,
            file_id=file_id if isinstance(file_id, str) else None,
            file_unique_id=(
                file_unique_id if isinstance(file_unique_id, str) else None
            ),
            delivery_id=str(message_id) if isinstance(message_id, int) else None,
        )

    def _failure(
        self,
        asset: DeliveryAsset,
        status: DeliveryStatus,
        error: BaseException,
        category: str,
    ) -> DeliveredItem:
        return DeliveredItem(
            item=asset.item,
            status=status,
            telegram_type=asset.item.kind,
            item_index=asset.item_index or 0,
            error=str(error),
            error_category=category,
        )

    async def _store(
        self, request: MediaRequest | None, delivered: DeliveredItem
    ) -> None:
        from app.core.media_cache import CachedDelivery

        if self.media_cache is None or request is None or not delivered.file_id:
            return
        bot_id = self._cache_bot_id()
        if not bot_id:
            return
        try:
            await self.media_cache.put_delivery(
                request,
                bot_id=bot_id,
                delivery=CachedDelivery(
                    file_id=delivered.file_id,
                    telegram_type=delivered.telegram_type,
                    item_index=delivered.item_index,
                    file_unique_id=delivered.file_unique_id,
                ),
            )
        except Exception:  # noqa: BLE001 - optional cache boundary
            self._log_cache_failure("put", delivered)

    async def _evict(self, request: MediaRequest | None, asset: DeliveryAsset) -> None:
        if self.media_cache is not None and request is not None:
            bot_id = self._cache_bot_id()
            if not bot_id:
                return
            try:
                await self.media_cache.evict_delivery(
                    request, bot_id=bot_id, item_index=asset.item_index or 0
                )
            except Exception:  # noqa: BLE001 - optional cache boundary
                self._log_cache_failure("evict", asset)

    def _cache_bot_id(self) -> str | None:
        if self.bot_id:
            return self.bot_id
        try:
            value = getattr(self.bot, "id", None)
        except Exception:  # noqa: BLE001 - optional cache identity
            logger.warning(
                "Optional media cache operation failed",
                extra={"operation": "identity", "category": "backend_error"},
            )
            return None
        return str(value) if isinstance(value, int | str) else None

    @staticmethod
    def _log_cache_failure(operation: str, item: DeliveryAsset | DeliveredItem) -> None:
        logger.warning(
            "Optional media cache operation failed",
            extra={
                "operation": operation,
                "category": "backend_error",
                "item_index": item.item_index or 0,
            },
        )

    @staticmethod
    def _receipt(
        target: DeliveryTarget, items: tuple[DeliveredItem, ...]
    ) -> DeliveryReceipt:
        return DeliveryReceipt(
            target=target, items=items, status=_aggregate_status(items)
        )


def _send_method(kind: MediaKind) -> tuple[str, str]:
    methods = {
        MediaKind.VIDEO: ("send_video", "video"),
        MediaKind.AUDIO: ("send_audio", "audio"),
        MediaKind.PHOTO: ("send_photo", "photo"),
        MediaKind.ANIMATION: ("send_animation", "animation"),
        MediaKind.DOCUMENT: ("send_document", "document"),
    }
    try:
        return methods[kind]
    except KeyError as error:
        raise ValueError(f"unsupported Telegram media kind: {kind}") from error


def _durable_item_key(target: DeliveryTarget, asset: DeliveryAsset) -> str:
    return f"{target.destination}:{asset.item_index or 0}:{asset.item.media_id}"


def _durable_outcome(status: DeliveryStatus) -> DeliveryOutcome:
    if status in {DeliveryStatus.SUCCESS, DeliveryStatus.DELIVERED}:
        return DeliveryOutcome.SUCCESS
    if status is DeliveryStatus.UNCERTAIN:
        return DeliveryOutcome.UNCERTAIN
    return DeliveryOutcome.FAILED


def _input_media(
    kind: MediaKind,
    source: Any,
    *,
    caption: str | None,
    parse_mode: str | None,
) -> Any:
    classes = {
        MediaKind.VIDEO: InputMediaVideo,
        MediaKind.AUDIO: InputMediaAudio,
        MediaKind.PHOTO: InputMediaPhoto,
        MediaKind.DOCUMENT: InputMediaDocument,
    }
    try:
        media_class = classes[kind]
    except KeyError as error:
        raise ValueError(f"unsupported Telegram album kind: {kind}") from error
    return media_class(media=source, caption=caption, parse_mode=parse_mode)


def _message_media(message: Any, kind: MediaKind) -> Any:
    value = getattr(message, kind.value, None)
    if kind is MediaKind.PHOTO and isinstance(value, Sequence) and value:
        return value[-1]
    return value


def _aggregate_status(items: tuple[DeliveredItem, ...]) -> DeliveryStatus:
    if not items:
        return DeliveryStatus.FAILED
    statuses = {item.status for item in items}
    if statuses <= {DeliveryStatus.SUCCESS, DeliveryStatus.DELIVERED}:
        return DeliveryStatus.SUCCESS
    if statuses == {DeliveryStatus.FAILED}:
        return DeliveryStatus.FAILED
    if statuses == {DeliveryStatus.UNCERTAIN}:
        return DeliveryStatus.UNCERTAIN
    return DeliveryStatus.PARTIAL


def _album_chunks(
    assets: tuple[DeliveryAsset, ...],
) -> tuple[tuple[DeliveryAsset, ...], ...]:
    chunks: list[tuple[DeliveryAsset, ...]] = []
    start = 0
    while len(assets) - start > MAX_TELEGRAM_ALBUM_SIZE:
        take = MAX_TELEGRAM_ALBUM_SIZE
        if len(assets) - start - take == 1:
            take -= 1
        chunks.append(assets[start : start + take])
        start += take
    chunks.append(assets[start:])
    return tuple(chunks)


def _delivery_units(
    assets: tuple[DeliveryAsset, ...],
) -> tuple[tuple[DeliveryAsset, ...], ...]:
    """Build only Bot API-compatible, contiguous albums without reordering."""
    units: list[tuple[DeliveryAsset, ...]] = []
    pending: list[DeliveryAsset] = []
    pending_family: str | None = None

    def flush() -> None:
        nonlocal pending, pending_family
        if pending:
            units.extend(_album_chunks(tuple(pending)))
        pending = []
        pending_family = None

    for asset in assets:
        kind = asset.item.kind
        if kind in {MediaKind.PHOTO, MediaKind.VIDEO}:
            family = "visual"
        elif kind is MediaKind.AUDIO:
            family = "audio"
        elif kind is MediaKind.DOCUMENT:
            family = "document"
        else:
            flush()
            units.append((asset,))
            continue
        if pending_family is not None and pending_family != family:
            flush()
        pending_family = family
        pending.append(asset)
    flush()
    return tuple(units)


def _is_invalid_file_id(error: BadRequest) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "wrong file identifier",
            "invalid file identifier",
            "invalid file_id",
            "file_id is invalid",
        )
    )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _chat_id(destination: str) -> int | str:
    return int(destination) if destination.lstrip("-").isdigit() else destination


__all__ = ["MAX_TELEGRAM_ALBUM_SIZE", "DeliveryAsset", "TelegramDelivery"]
