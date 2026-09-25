from collections.abc import Callable
from datetime import UTC, datetime
from hmac import compare_digest
from uuid import uuid4

from app.domain.errors import EventConflict, IntegrityViolation, SourceMismatch
from app.domain.events import EventReceipt, RecordMetadata, StoredEvent
from app.ports.journal import EventCodec, EventJournal, EventProtector


class IngestEvent:
    """Internal envelope journal, not an authenticated public ingestion endpoint.

    The future transport must authenticate its source and validate generated
    type-specific DTOs before invoking this service. Projection work is explicitly
    unresolved until the production domain processor is connected.
    """

    def __init__(
        self,
        journal: EventJournal,
        protector: EventProtector,
        codec: EventCodec,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.journal = journal
        self.protector = protector
        self.codec = codec
        self.clock = clock

    def execute(self, raw: bytes, *, authenticated_source_id: str) -> EventReceipt:
        event = self.codec.decode(raw)
        if event.envelope.source_id != authenticated_source_id:
            raise SourceMismatch("The event source does not match the authenticated source.")
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("The server clock must be timezone-aware.")
        issues = ["domain_projection_not_implemented"]
        if event.envelope.item_id is None or event.envelope.identification_status != "reliable":
            issues.append("unreliable_item_identification")
        if event.envelope.time_quality != "synchronized":
            issues.append("uncertain_source_time")
        metadata = RecordMetadata(
            record_id=uuid4(),
            envelope=event.envelope,
            received_at=now.astimezone(UTC),
            content_digest=event.content_digest,
            initial_projection_status="unresolved",
            initial_issues=tuple(issues),
        )
        candidate = StoredEvent(metadata, self.protector.seal(event.original_json, metadata))
        stored, inserted = self.journal.append_if_absent(candidate)
        if not inserted:
            # Do not trust even a matching digest/receipt until the stored record
            # has been authenticated. Metadata is covered by the AEAD tag.
            original = self.protector.open(stored.protection, stored.metadata)
            saved = self.codec.decode(original)
            if saved.envelope != stored.metadata.envelope or not compare_digest(
                saved.content_digest, stored.metadata.content_digest
            ):
                raise IntegrityViolation("Stored metadata does not match the original event.")
            if not compare_digest(saved.content_digest, event.content_digest):
                raise EventConflict("This source and event ID already identify different content.")
        return EventReceipt(
            record_id=stored.metadata.record_id,
            source_id=stored.metadata.envelope.source_id,
            event_id=stored.metadata.envelope.event_id,
            status="accepted" if inserted else "duplicate",
            received_at=stored.metadata.received_at,
            projection_status=stored.metadata.initial_projection_status,
            issues=stored.metadata.initial_issues,
        )
