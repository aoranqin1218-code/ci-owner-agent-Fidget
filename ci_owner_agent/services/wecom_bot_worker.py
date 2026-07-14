from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Mapping
from typing import Any

from ci_owner_agent.services.pending_feedback_store import WeComEventStore
from ci_owner_agent.services.wecom_bot_adapter import WeComBotAdapterProtocol
from ci_owner_agent.services.wecom_feedback_service import WeComFeedbackService
from ci_owner_agent.services.wecom_message_normalizer import normalize_wecom_text_frame


class WeComBotWorker:
    def __init__(
        self,
        adapter: WeComBotAdapterProtocol,
        history_store: Any,
        *,
        confirm_ttl_seconds: int = 300,
        feedback_code_ttl_days: int = 30,
        event_ttl_days: int = 7,
    ) -> None:
        self.adapter = adapter
        self.events = WeComEventStore(history_store, event_ttl_days)
        self.service = WeComFeedbackService(
            history_store,
            context_ttl_days=feedback_code_ttl_days,
            confirm_ttl_seconds=confirm_ttl_seconds,
        )
        self._stop_event: asyncio.Event | None = None
        adapter.set_text_handler(self.handle_frame)

    def run(self) -> None:
        try:
            asyncio.run(self._run())
        except KeyboardInterrupt:
            logging.getLogger(__name__).info("WeCom bot interrupted")

    async def _run(self) -> None:
        self._stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, self._stop_event.set)
            except (AttributeError, NotImplementedError, RuntimeError):
                pass
        try:
            await self.adapter.start()
            await self._stop_event.wait()
        finally:
            await self.adapter.stop()

    def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()

    async def handle_frame(self, frame: Mapping[str, Any]) -> None:
        try:
            message = normalize_wecom_text_frame(frame)
            is_new = await asyncio.to_thread(self.events.mark_once, message)
            if not is_new:
                return
            reply = await asyncio.to_thread(self.service.handle, message)
        except Exception as exc:
            logging.getLogger(__name__).exception("Failed to process WeCom text message")
            reply = f"消息处理失败：{str(exc)[:200]}"
        try:
            await self.adapter.reply(frame, reply)
        except Exception:
            logging.getLogger(__name__).exception("Failed to reply to WeCom message")

