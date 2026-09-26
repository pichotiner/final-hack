"""Durable leases, bounded retry delay and fenced ACK commits (DP-01)."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import or_, select

from app.adapters.locks import production_guard
from app.adapters.postgres import quality_deliveries, quality_outbox
from app.domain.errors import AckMismatch, IntegrityViolation, UnknownEntity
from app.domain.quality import _digest


class DeliveryFailure(Exception):
    def __init__(self, code, *, retryable):
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class OutboxDeliveryWorker:
    def __init__(self, journal, receiver, *, lease_seconds=60, clock=None):
        self.journal = journal
        self.engine = journal.engine
        self.receiver = receiver
        self.lease_seconds = lease_seconds
        self.clock = clock or (lambda: datetime.now(UTC))

    def claim_one(self, message_id=None):
        now = self.clock()
        with self.engine.begin() as connection:
            production_guard(connection)
            query = (
                select(quality_deliveries)
                .join(quality_outbox)
                .where(
                    quality_outbox.c.status != "acknowledged",
                    or_(
                        (quality_deliveries.c.status == "pending")
                        & (quality_deliveries.c.next_attempt_at <= now),
                        (quality_deliveries.c.status == "sending")
                        & (quality_deliveries.c.lease_until <= now),
                    ),
                )
            )
            if message_id is not None:
                query = query.where(quality_deliveries.c.message_id == message_id)
            row = (
                connection.execute(
                    query.order_by(
                        quality_deliveries.c.next_attempt_at, quality_deliveries.c.message_id
                    ).limit(1)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            token = str(uuid4())
            connection.execute(
                quality_deliveries.update()
                .where(quality_deliveries.c.message_id == row["message_id"])
                .values(
                    status="sending",
                    lease_token=token,
                    lease_until=now + timedelta(seconds=self.lease_seconds),
                    attempts=row["attempts"] + 1,
                )
            )
            message = (
                connection.execute(
                    select(quality_outbox).where(quality_outbox.c.message_id == row["message_id"])
                )
                .mappings()
                .one()
            )
            return dict(message) | {"lease_token": token, "attempts": row["attempts"] + 1}

    def _finish(self, message, *, receipt=None, error=None):
        with self.engine.begin() as connection:
            production_guard(connection)
            row = (
                connection.execute(
                    select(quality_deliveries).where(
                        quality_deliveries.c.message_id == message["message_id"]
                    )
                )
                .mappings()
                .one()
            )
            if row["lease_token"] != message["lease_token"] or row["status"] != "sending":
                return "lease_lost"
            if error is None:
                self.journal.quality._run(
                    message["item_id"],
                    lambda workflow, _: workflow.acknowledge(
                        message["message_id"], receipt=receipt
                    ),
                    connection=connection,
                )
            status = (
                "acknowledged" if error is None else ("pending" if error.retryable else "error")
            )
            delay = min(300, 2 ** min(row["attempts"], 9))
            connection.execute(
                quality_deliveries.update()
                .where(quality_deliveries.c.message_id == message["message_id"])
                .values(
                    status=status,
                    lease_token=None,
                    lease_until=None,
                    next_attempt_at=self.clock() + timedelta(seconds=delay),
                    last_error=error.code if error else None,
                )
            )
            return status

    def deliver_once(self, message_id=None):
        message = self.claim_one(message_id)
        if message is None:
            return None
        try:
            if _digest(message["payload"]) != message["content_digest"]:
                raise IntegrityViolation("Stored outbox digest does not match its body.")
            receipt = self.receiver(message["payload"])
            status = self._finish(message, receipt=receipt)
        except (AckMismatch, IntegrityViolation) as error:
            status = self._finish(message, error=DeliveryFailure(error.code, retryable=False))
        except DeliveryFailure as error:
            status = self._finish(message, error=error)
        # Unexpected programming/storage errors propagate. The durable lease expires;
        # no arbitrary exception text (possibly a credential) is stored or returned.
        return {
            "message_id": message["message_id"],
            "status": status,
            "attempts": message["attempts"],
        }

    def retry(self, message_id):
        with self.engine.begin() as connection:
            production_guard(connection)
            row = (
                connection.execute(
                    select(quality_deliveries).where(quality_deliveries.c.message_id == message_id)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise UnknownEntity("Unknown delivery.")
            if row["status"] == "error":
                connection.execute(
                    quality_deliveries.update()
                    .where(quality_deliveries.c.message_id == message_id)
                    .values(status="pending", next_attempt_at=self.clock())
                )
            return {
                "message_id": message_id,
                "status": "pending" if row["status"] == "error" else row["status"],
            }
