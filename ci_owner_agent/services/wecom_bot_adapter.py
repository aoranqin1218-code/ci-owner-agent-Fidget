from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

FrameHandler = Callable[[Mapping[str, Any]], Awaitable[None]]


class WeComBotAdapterProtocol(Protocol):
    def set_text_handler(self, handler: FrameHandler) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def reply(self, frame: Mapping[str, Any], text: str) -> None: ...


class WeComSdkAdapter:
    """Thin adapter around the optional official SDK; no feedback business logic lives here."""

    def __init__(self, bot_id: str, secret: str) -> None:
        try:
            from aibot import WSClient, WSClientOptions, generate_req_id
        except ImportError as exc:
            raise RuntimeError(
                'wecom-aibot-python-sdk is required; install with: pip install -e ".[wecom-bot]"'
            ) from exc
        self._generate_req_id = generate_req_id
        self._client = WSClient(WSClientOptions(bot_id=bot_id, secret=secret, logger=_SdkLogger()))
        self._handler: FrameHandler | None = None
        self._client.on("message.text", self._on_text)
        self._client.on("connected", lambda: logging.getLogger(__name__).info("WeCom bot connected"))
        self._client.on("authenticated", lambda: logging.getLogger(__name__).info("WeCom bot authenticated"))
        self._client.on("disconnected", lambda reason: logging.getLogger(__name__).warning("WeCom bot disconnected: %s", reason))
        self._client.on("reconnecting", lambda attempt: logging.getLogger(__name__).info("WeCom bot reconnecting: attempt %s", attempt))
        self._client.on("error", lambda error: logging.getLogger(__name__).error("WeCom bot SDK error: %s", error))

    def set_text_handler(self, handler: FrameHandler) -> None:
        self._handler = handler

    async def _on_text(self, frame: Mapping[str, Any]) -> None:
        if self._handler is not None:
            await self._handler(frame)

    async def start(self) -> None:
        await self._client.connect()

    async def stop(self) -> None:
        result = self._client.disconnect()
        if inspect.isawaitable(result):
            await result
        else:
            # SDK 1.0.2 schedules its async socket close from the synchronous method.
            await asyncio.sleep(0)

    async def reply(self, frame: Mapping[str, Any], text: str) -> None:
        stream_id = self._generate_req_id("stream")
        await self._client.reply_stream(frame, stream_id, text, True)


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
