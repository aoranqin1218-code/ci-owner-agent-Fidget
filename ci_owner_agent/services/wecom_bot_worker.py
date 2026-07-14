from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Mapping
from typing import Any

from ci_owner_agent.services.pending_feedback_store import WeComEventStore
from ci_owner_agent.services.wecom_bot_adapter import WeComBotAdapterProtocol, fatal_sdk_error_reason
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
        self._fatal_error: BaseException | None = None
        adapter.set_text_handler(self.handle_frame)
        adapter.set_fatal_error_handler(self._handle_fatal_error)

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
        run_error: BaseException | None = None
        stop_error: BaseException | None = None
        try:
            if self._fatal_error is None:
                await self.adapter.start()
            if self._fatal_error is None:
                await self._stop_event.wait()
        except BaseException as exc:
            run_error = exc
        finally:
            try:
                await self.adapter.stop()
            except BaseException as exc:
                stop_error = exc
                logging.getLogger(__name__).exception("Failed to stop WeCom bot adapter")
        if self._fatal_error is not None:
            raise RuntimeError(
                f"WeCom bot stopped because of a fatal SDK error: {fatal_sdk_error_reason(self._fatal_error)}"
            ) from self._fatal_error
        if run_error is not None:
            raise run_error
        if stop_error is not None:
            raise stop_error

    def _handle_fatal_error(self, error: BaseException) -> None:
        if self._fatal_error is not None:
            return
        self._fatal_error = error
        logging.getLogger(__name__).error(
            "Stopping WeCom bot after fatal SDK error: %s", fatal_sdk_error_reason(error)
        )
        if self._stop_event is not None:
            self._stop_event.set()

    def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()

    async def handle_frame(self, frame: Mapping[str, Any]) -> None:
        try:
            message = normalize_wecom_text_frame(frame)
            claim_status, event = await asyncio.to_thread(self.events.claim, message)
            if claim_status == "completed":
                reply = str((event or {}).get("replyText") or "消息已处理完成。")
            elif claim_status == "processing":
                reply = "相同消息正在处理中，请稍后。"
            elif claim_status != "claimed":
                reply = "消息暂时无法处理，请稍后重试。"
            else:
                reply = await asyncio.to_thread(self.service.handle, message)
                try:
                    await asyncio.to_thread(self.events.mark_completed, message.event_key, reply)
                except Exception:
                    logging.getLogger(__name__).exception("Failed to mark WeCom message event completed")
        except Exception as exc:
            logging.getLogger(__name__).exception("Failed to process WeCom text message")
            if "message" in locals():
                try:
                    await asyncio.to_thread(self.events.mark_failed, message.event_key, str(exc))
                except Exception:
                    logging.getLogger(__name__).exception("Failed to mark WeCom message event failed")
            reply = f"消息处理失败：{str(exc)[:200]}"
        try:
            await self.adapter.reply(frame, reply)
        except Exception:
            logging.getLogger(__name__).exception("Failed to reply to WeCom message")
