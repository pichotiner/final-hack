from typing import Protocol

from app.domain.events import PreparedEvent, ProtectedPayload, RecordMetadata, StoredEvent


class EventCodec(Protocol):
    def decode(self, raw: bytes) -> PreparedEvent: ...


class EventProtector(Protocol):
    def seal(self, raw: bytes, metadata: RecordMetadata) -> ProtectedPayload: ...

    def open(self, protection: ProtectedPayload, metadata: RecordMetadata) -> bytes: ...


class EventJournal(Protocol):
    def append_if_absent(self, event: StoredEvent) -> tuple[StoredEvent, bool]:
        """Return the committed original record and whether this call inserted it.

        The initial projection is created atomically with a new record. Adapters
        may finalize and reseal the initial receipt before persisting the record.
        A duplicate must never overwrite either record or projection.
        """
        ...
