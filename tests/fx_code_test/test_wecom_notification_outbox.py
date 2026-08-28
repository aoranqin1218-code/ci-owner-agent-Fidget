from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ci_owner_agent.services.wecom_notification_outbox import WeComNotificationOutbox
from tests.fx_code_test.test_history_store import make_store


def _outbox(*, attempts: int = 3):
    return WeComNotificationOutbox(make_store(), lease_seconds=30, max_attempts=attempts)


def _enqueue(outbox, *, force=False):
    return outbox.enqueue_markdown(notification_type="ci_notice", target_chat_id="chat-1", markdown="# hello",
                                   dedup_key="build-1", metadata={"repo": "repo"}, force=force)


def test_enqueue_creates_pending_document():
    result = _enqueue(_outbox())
    assert result["inserted"] is True and result["status"] == "pending"
    assert result["attemptCount"] == 0 and result["payload"]["content"] == "# hello"
    assert result["targetChatId"] == "chat-1" and result["metadata"] == {"repo": "repo"}
    assert result["createdAt"].tzinfo is not None and result["deliveryKey"]


def test_enqueue_duplicate_and_force():
    outbox = _outbox(); first = _enqueue(outbox); second = _enqueue(outbox); forced = _enqueue(outbox, force=True)
    assert second["inserted"] is False and first["deliveryKey"] == second["deliveryKey"]
    assert forced["inserted"] is True and forced["deliveryKey"] != first["deliveryKey"]


def test_claim_and_lease_recovery():
    outbox = _outbox(); item = _enqueue(outbox)
    now = item["nextAttemptAt"]
    claimed = outbox.claim_next(now=now)
    assert claimed and claimed["status"] == "sending" and claimed["attemptCount"] == 1
    assert claimed["leaseToken"] and claimed["leaseUntil"] > now
    assert outbox.claim_next(now=now) is None
    recovered = outbox.claim_next(now=claimed["leaseUntil"] + timedelta(seconds=1))
    assert recovered and recovered["deliveryKey"] == item["deliveryKey"] and recovered["attemptCount"] == 2


def test_mark_sent_and_failure_backoff_and_dead():
    outbox = _outbox(attempts=2); _enqueue(outbox)
    now = outbox.collection.docs[0]["nextAttemptAt"]; claimed = outbox.claim_next(now=now)
    assert outbox.mark_sent(delivery_key=claimed["deliveryKey"], lease_token="wrong", now=now) is False
    assert outbox.mark_failed(delivery_key=claimed["deliveryKey"], lease_token=claimed["leaseToken"], error=RuntimeError(" bad\nerror "), now=now) == "retry"
    pending = outbox.collection.find_one({"deliveryKey": claimed["deliveryKey"]})
    assert pending["nextAttemptAt"] == now + timedelta(seconds=1) and pending["lastError"] == "bad error"
    retry = outbox.claim_next(now=pending["nextAttemptAt"])
    assert outbox.mark_failed(delivery_key=retry["deliveryKey"], lease_token=retry["leaseToken"], error=RuntimeError("x"), now=now) == "dead"
    assert outbox.claim_next(now=now + timedelta(days=1)) is None


def test_mark_invalid_dead_requires_matching_lease_and_clears_lease():
    outbox = _outbox()
    item = _enqueue(outbox)
    claimed = outbox.claim_next(now=item["nextAttemptAt"])
    assert outbox.mark_invalid_dead(delivery_key=claimed["deliveryKey"], lease_token="wrong") is False
    assert outbox.mark_invalid_dead(delivery_key=claimed["deliveryKey"], lease_token=claimed["leaseToken"]) is True
    dead = outbox.collection.find_one({"deliveryKey": claimed["deliveryKey"]})
    assert dead["status"] == "dead" and dead["deadAt"] is not None and dead["nextAttemptAt"] is None
    assert dead["leaseToken"] is None and dead["leaseUntil"] is None and dead["lastErrorType"] == "InvalidOutboxItem"


def test_claim_uses_document_id_when_delivery_key_missing():
    outbox = _outbox()
    now = datetime.now(timezone.utc)
    outbox.collection.insert_one({"_id": "broken", "status": "pending", "nextAttemptAt": now, "createdAt": now})
    claimed = outbox.claim_next(now=now)
    assert claimed and claimed["_id"] == "broken" and claimed["leaseToken"]
    assert outbox.mark_invalid_dead(document_id="broken", lease_token=claimed["leaseToken"]) is True
