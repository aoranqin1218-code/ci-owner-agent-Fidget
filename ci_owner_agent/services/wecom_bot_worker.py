from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Mapping
from typing import Any

from ci_owner_agent.services.pending_feedback_store import WeComEventStore
from ci_owner_agent.services.wecom_bot_adapter import WeComBotAdapterProtocol, fatal_sdk_error_reason
from ci_owner_agent.services.wecom_feedback_ai_parser import WeComFeedbackAiParserProtocol
from ci_owner_agent.services.wecom_feedback_service import WeComFeedbackService
from ci_owner_agent.services.wecom_message_normalizer import normalize_wecom_text_frame, normalize_wecom_template_card_event
from ci_owner_agent.services.wecom_notification_outbox import WeComNotificationOutbox


class WeComBotWorker:
    def __init__(
        self,
        adapter: WeComBotAdapterProtocol,
        history_store: Any,
        *,
        confirm_ttl_seconds: int = 300,
        feedback_code_ttl_days: int = 30,
        event_ttl_days: int = 7,
        ai_parser: WeComFeedbackAiParserProtocol | None = None,
        card_action_url: str | None = None,
        notification_chat_id: str | None = None,
        notification_poll_seconds: int = 2,
        notification_lease_seconds: int = 30,
        notification_max_attempts: int = 5,
    ) -> None:
        self.adapter = adapter
        self.events = WeComEventStore(history_store, event_ttl_days)
        self.service = WeComFeedbackService(
            history_store,
            context_ttl_days=feedback_code_ttl_days,
            confirm_ttl_seconds=confirm_ttl_seconds,
            ai_parser=ai_parser,
            card_action_url=card_action_url,
        )
        self._stop_event: asyncio.Event | None = None
        self._fatal_error: BaseException | None = None
        self.notification_chat_id = (notification_chat_id or "").strip() or None
        self.notification_poll_seconds = max(1, notification_poll_seconds)
        self.notification_outbox = WeComNotificationOutbox(history_store, lease_seconds=notification_lease_seconds,
                                                            max_attempts=notification_max_attempts)
        self._notification_task: asyncio.Task[None] | None = None
        adapter.set_text_handler(self.handle_text_frame)
        adapter.set_template_card_event_handler(self.handle_template_card_event_frame)
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
            if self._fatal_error is None and self.notification_chat_id:
                self._notification_task = asyncio.create_task(self._notification_loop())
            if self._fatal_error is None:
                await self._stop_event.wait()
        except BaseException as exc:
            run_error = exc
        finally:
            if self._notification_task is not None:
                self._notification_task.cancel()
                try:
                    await self._notification_task
                except asyncio.CancelledError:
                    pass
                self._notification_task = None
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

    async def deliver_one_notification(self) -> bool:
        item = await asyncio.to_thread(self.notification_outbox.claim_next)
        if item is None:
            return False
        delivery_key, lease_token = item.get("deliveryKey"), item.get("leaseToken")
        chat_id = item.get("targetChatId")
        markdown = (item.get("payload") or {}).get("content")
        if not all(isinstance(value, str) and value for value in (delivery_key, lease_token, chat_id, markdown)):
            logging.getLogger(__name__).warning("Invalid notification outbox item")
            return True
        prefix = delivery_key[:12]
        try:
            await self.adapter.send_markdown(chat_id, markdown)
        except Exception as exc:
            outcome = await asyncio.to_thread(self.notification_outbox.mark_failed, delivery_key=delivery_key,
                                              lease_token=lease_token, error=exc)
            logging.getLogger(__name__).warning("Notification %s %s attempt=%s", prefix, outcome, item.get("attemptCount"))
            return True
        sent = await asyncio.to_thread(self.notification_outbox.mark_sent, delivery_key=delivery_key, lease_token=lease_token)
        if not sent:
            logging.getLogger(__name__).warning("notification lease lost after send")
        else:
            logging.getLogger(__name__).info("Notification %s sent attempt=%s", prefix, item.get("attemptCount"))
        return True

    async def _notification_loop(self) -> None:
        logger = logging.getLogger(__name__)
        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                if await self.deliver_one_notification():
                    continue
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.notification_poll_seconds)
                except TimeoutError:
                    pass
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Notification delivery loop failed")
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.notification_poll_seconds)
                except TimeoutError:
                    pass

    async def handle_text_frame(self, frame: Mapping[str, Any]) -> None:
        claim_token: str | None = None
        logger = logging.getLogger(__name__)

        try:
            message = normalize_wecom_text_frame(frame)

            claim_status, event = await asyncio.to_thread(
                self.events.claim,
                message,
            )

            if claim_status == "completed":
                reply_type = (event or {}).get("responseType") or "text"
                if reply_type == "template_card":
                    payload = (event or {}).get("responsePayload") or {}
                    await self.adapter.reply_template_card(frame, payload.get("template_card", {}))
                    return
                reply = str(
                    (event or {}).get("replyText")
                    or "消息已处理完成。"
                )
                await self.adapter.reply_text(frame, reply)
                return
            elif claim_status == "processing":
                reply = "相同消息正在处理中，请稍后。"
                await self.adapter.reply_text(frame, reply)
                return
            elif claim_status != "claimed":
                reply = "消息暂时无法处理，请稍后重试。"
                await self.adapter.reply_text(frame, reply)
                return
            else:
                claim_token = event["claimToken"]
                bot_reply = await asyncio.to_thread(
                    self.service.handle_text,
                    message,
                )

                if bot_reply.reply_type == "template_card":
                    try:
                        completed = await asyncio.to_thread(
                            self.events.mark_completed_with_response,
                            message.event_key,
                            claim_token,
                            "template_card",
                            {"template_card": bot_reply.template_card},
                        )
                        if not completed:
                            logger.warning(
                                "Lost WeCom event claim before completion: %s",
                                message.event_key,
                            )
                    except Exception:
                        logger.exception("Failed to mark WeCom message event completed")
                    await self.adapter.reply_template_card(frame, bot_reply.template_card or {})
                    return
                else:
                    reply = bot_reply.text or ""
                    try:
                        completed = await asyncio.to_thread(
                            self.events.mark_completed,
                            message.event_key,
                            claim_token,
                            reply,
                        )
                        if not completed:
                            logger.warning(
                                "Lost WeCom event claim before completion: %s",
                                message.event_key,
                            )
                    except Exception:
                        logger.exception("Failed to mark WeCom message event completed")
                    await self.adapter.reply_text(frame, reply)
                    return

        except Exception as exc:
            logger.exception("Failed to process WeCom text message")

            if "message" in locals() and claim_token:
                try:
                    await asyncio.to_thread(
                        self.events.mark_failed,
                        message.event_key,
                        claim_token,
                        str(exc),
                    )
                except Exception:
                    logger.exception("Failed to mark WeCom message event failed")

            reply = f"消息处理失败：{str(exc)[:200]}"

        try:
            await self.adapter.reply_text(frame, reply)
        except Exception:
            logger.exception("Failed to reply to WeCom message")

    async def handle_template_card_event_frame(self, frame: Mapping[str, Any]) -> None:
        claim_token: str | None = None
        logger = logging.getLogger(__name__)

        try:
            event = normalize_wecom_template_card_event(frame)

            claim_status, ev = await asyncio.to_thread(
                self.events.claim,
                event,
            )

            if claim_status == "completed":
                payload = (ev or {}).get("responsePayload") or {}
                await self.adapter.update_template_card(
                    frame,
                    payload.get("template_card", {}),
                    payload.get("userids"),
                )
                return
            elif claim_status == "processing":
                return
            elif claim_status != "claimed":
                return
            else:
                claim_token = ev["claimToken"]
                bot_reply = await asyncio.to_thread(
                    self.service.handle_template_card_event,
                    event,
                )

                card_data = dict(bot_reply.template_card or {})
                userids = card_data.pop("userids", None)

                try:
                    completed = await asyncio.to_thread(
                        self.events.mark_completed_with_response,
                        event.event_key,
                        claim_token,
                        "update_template_card",
                        {"template_card": card_data, "userids": userids},
                    )
                    if not completed:
                        logger.warning(
                            "Lost WeCom template card event claim before completion: %s",
                            event.event_key,
                        )
                except Exception:
                    logger.exception("Failed to mark template card event completed")

                await self.adapter.update_template_card(frame, card_data, userids)
                return

        except Exception as exc:
            logger.exception("Failed to process WeCom template card event")
            if "event" in locals() and claim_token:
                try:
                    await asyncio.to_thread(
                        self.events.mark_failed,
                        event.event_key,
                        claim_token,
                        str(exc),
                    )
                except Exception:
                    logger.exception("Failed to mark template card event failed")
