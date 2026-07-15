from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

FrameHandler = Callable[[Mapping[str, Any]], Awaitable[None]]
TemplateCardEventHandler = Callable[[Mapping[str, Any]], Awaitable[None]]
FatalErrorHandler = Callable[[BaseException], None]

_AUTH_FAILURE_MARKER = "authentication failed"
_RECONNECT_EXHAUSTED_MARKER = "max reconnect attempts exceeded"


class WeComBotAdapterProtocol(Protocol):
    def set_text_handler(self, handler: FrameHandler) -> None: ...
    def set_template_card_event_handler(self, handler: TemplateCardEventHandler) -> None: ...
    def set_fatal_error_handler(self, handler: FatalErrorHandler) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def reply_text(self, frame: Mapping[str, Any], text: str) -> None: ...
    async def reply_template_card(self, frame: Mapping[str, Any], template_card: dict[str, Any]) -> None: ...
    async def update_template_card(
        self, frame: Mapping[str, Any], template_card: dict[str, Any], userids: list[str] | None = None
    ) -> None: ...


class WeComSdkAdapter:
    """Thin adapter around the optional official SDK; no feedback business logic lives here."""

    def __init__(self, bot_id: str, secret: str, *, close_timeout_seconds: float = 5.0) -> None:
        try:
            from aibot import WSClient, WSClientOptions, generate_req_id
        except ImportError as exc:
            raise RuntimeError(
                'wecom-aibot-python-sdk is required; install with: pip install -e ".[wecom-bot]"'
            ) from exc
        self._generate_req_id = generate_req_id
        self._client = WSClient(WSClientOptions(bot_id=bot_id, secret=secret, logger=_SdkLogger()))
        self._text_handler: FrameHandler | None = None
        self._card_handler: TemplateCardEventHandler | None = None
        self._fatal_error_handler: FatalErrorHandler | None = None
        self._close_timeout_seconds = max(0.0, float(close_timeout_seconds))
        self._client.on("message.text", self._on_text)
        self._client.on("event.template_card_event", self._on_template_card_event)
        self._client.on("connected", lambda: logging.getLogger(__name__).info("WeCom bot connected"))
        self._client.on("authenticated", lambda: logging.getLogger(__name__).info("WeCom bot authenticated"))
        self._client.on("disconnected", lambda reason: logging.getLogger(__name__).warning("WeCom bot disconnected: %s", reason))
        self._client.on("reconnecting", lambda attempt: logging.getLogger(__name__).info("WeCom bot reconnecting: attempt %s", attempt))
        self._client.on("error", self._on_sdk_error)

    def set_text_handler(self, handler: FrameHandler) -> None:
        self._text_handler = handler

    def set_template_card_event_handler(self, handler: TemplateCardEventHandler) -> None:
        self._card_handler = handler

    def set_fatal_error_handler(self, handler: FatalErrorHandler) -> None:
        self._fatal_error_handler = handler

    def _on_sdk_error(self, error: BaseException) -> None:
        logger = logging.getLogger(__name__)
        if is_fatal_sdk_error(error):
            logger.error("WeCom bot fatal SDK error: %s", fatal_sdk_error_reason(error))
            if self._fatal_error_handler is not None:
                self._fatal_error_handler(error)
            return
        logger.warning("WeCom bot recoverable SDK error: %s", error)

    async def _on_text(self, frame: Mapping[str, Any]) -> None:
        if self._text_handler is not None:
            await self._text_handler(frame)

    async def _on_template_card_event(self, frame: Mapping[str, Any]) -> None:
        if self._card_handler is not None:
            await self._card_handler(frame)

    async def start(self) -> None:
        await self._client.connect()

    async def stop(self) -> None:
        result = self._client.disconnect()
        if inspect.isawaitable(result):
            await result
            return
        connected = _client_is_connected(self._client)
        if connected is None:
            return
        try:
            async with asyncio.timeout(self._close_timeout_seconds):
                while _client_is_connected(self._client):
                    await asyncio.sleep(0.05)
        except TimeoutError:
            logging.getLogger(__name__).warning("Timed out waiting for WeCom WebSocket to close")

    async def reply_text(self, frame: Mapping[str, Any], text: str) -> None:
        stream_id = self._generate_req_id("stream")
        await self._client.reply_stream(frame, stream_id, text, True)

    async def reply_template_card(self, frame: Mapping[str, Any], template_card: dict[str, Any]) -> None:
        await self._client.reply_template_card(frame, template_card)

    async def update_template_card(
        self, frame: Mapping[str, Any], template_card: dict[str, Any], userids: list[str] | None = None
    ) -> None:
        kwargs: dict[str, Any] = {"frame": frame, "card": template_card}
        if userids is not None:
            kwargs["userids"] = userids
        await self._client.update_template_card(**kwargs)

    # Compatibility alias
    reply = reply_text


def is_fatal_sdk_error(error: BaseException) -> bool:
    message = str(error).lower()
    return _AUTH_FAILURE_MARKER in message or _RECONNECT_EXHAUSTED_MARKER in message


def fatal_sdk_error_reason(error: BaseException) -> str:
    message = str(error).lower()
    if _AUTH_FAILURE_MARKER in message:
        return "authentication failed"
    if _RECONNECT_EXHAUSTED_MARKER in message:
        return "maximum reconnect attempts exceeded"
    return "unrecoverable SDK error"


def _client_is_connected(client: Any) -> bool | None:
    try:
        value = getattr(client, "is_connected")
    except Exception:
        return None
    return bool(value)


class _SdkLogger:
    """SDK logger that deliberately never serializes raw frames or credentials."""

    def debug(self, message: str, *args: Any) -> None:
        logging.getLogger(__name__).debug("%s", message)

    def info(self, message: str, *args: Any) -> None:
        logging.getLogger(__name__).info("%s", message)

    def warn(self, message: str, *args: Any) -> None:
        logging.getLogger(__name__).warning("%s", message)

    def error(self, message: str, *args: Any) -> None:
        logging.getLogger(__name__).error("%s", message)