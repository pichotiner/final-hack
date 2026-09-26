from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.adapters.postgres import quality_deliveries
from app.application.outbox import DeliveryFailure, OutboxDeliveryWorker
from app.domain.quality import _digest

pytestmark = pytest.mark.integration
pytest_plugins = ("tests.integration.test_traceability_postgres",)


def ack(payload):
    return {
        **{
            key: payload[key]
            for key in (
                "message_id",
                "result_version",
                "producer_id",
                "external_system",
                "external_order_id",
                "external_item_id",
            )
        },
        "content_digest": _digest(payload),
        "application_status": "applied",
        "received_at": datetime.now(UTC).isoformat(),
        "current_version_at_accept": payload["result_version"],
        "status": "accepted",
    }


def test_worker_lost_ack_restart_backoff_and_fenced_lease(production):
    _, _, journal, _, _, item_id, _, _ = production
    initial = journal.quality.state(item_id)
    message = initial["outbox"][0]
    message_id = message["message_id"]
    clock = [datetime.now(UTC)]
    accepted = ack(message["payload"])
    calls = []

    def receiver(payload):
        calls.append(payload)
        if len(calls) == 1:
            raise DeliveryFailure("transport_unavailable", retryable=True)
        return accepted | {"status": "duplicate"}

    worker = OutboxDeliveryWorker(journal, receiver, clock=lambda: clock[0])
    assert worker.deliver_once(message_id)["status"] == "pending"
    assert worker.deliver_once(message_id) is None  # persisted backoff
    clock[0] += timedelta(seconds=3)
    lost = worker.claim_one(message_id)  # crash after claim
    restarted = OutboxDeliveryWorker(journal, receiver, clock=lambda: clock[0])
    assert restarted.claim_one(message_id) is None
    clock[0] += timedelta(seconds=61)
    assert restarted.deliver_once(message_id)["status"] == "acknowledged"
    assert (
        worker._finish(lost, error=DeliveryFailure("old_failure", retryable=True)) == "lease_lost"
    )
    assert calls[0] == calls[1] == message["payload"]
    after = journal.quality.state(item_id)
    assert after["context_revision"] == initial["context_revision"]
    assert len(after["outbox"]) == 1
    assert journal.quality.get_outbox(message_id)["attempts"] == 3
    assert restarted.deliver_once(message_id) is None


def test_bad_ack_is_visible_and_retry_retains_original_body(production):
    _, writer, journal, _, _, item_id, _, _ = production
    message_id = journal.quality.state(item_id)["outbox"][0]["message_id"]
    worker = OutboxDeliveryWorker(journal, lambda payload: ack(payload) | {"content_digest": "bad"})
    assert worker.deliver_once(message_id)["status"] == "error"
    with writer.connect() as connection:
        row = (
            connection.execute(
                select(quality_deliveries).where(quality_deliveries.c.message_id == message_id)
            )
            .mappings()
            .one()
        )
    assert row["last_error"] == "ack_mismatch"
    assert journal.quality.state(item_id)["outbox"][0]["ack"] is None
    assert worker.deliver_once(message_id) is None
    assert worker.retry(message_id)["status"] == "pending"
    worker.receiver = lambda payload: None
    assert worker.deliver_once(message_id)["status"] == "error"
    assert journal.quality.state(item_id)["outbox"][0]["ack"] is None
    worker.retry(message_id)
    worker.receiver = ack
    assert worker.deliver_once(message_id)["status"] == "acknowledged"
