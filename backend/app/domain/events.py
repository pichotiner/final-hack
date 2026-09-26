from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class EventType(StrEnum):
    ITEM_RECEIVED = "item_received"
    OPERATION_STARTED = "operation_started"
    OPERATION_FINISHED = "operation_finished"
    OPERATOR_ACTION = "operator_action"
    MACHINE_STATE = "machine_state"
    INSPECTION_OBSERVED = "inspection_observed"


@dataclass(frozen=True)
class EventEnvelope:
    source_id: str
    event_id: str
    schema_version: str
    event_type: EventType
    item_id: str | None
    occurred_at: datetime
    identification_status: str
    time_quality: str


@dataclass(frozen=True)
class PreparedEvent:
    envelope: EventEnvelope
    original_json: bytes = field(repr=False)
    content_digest: str


@dataclass(frozen=True)
class RecordMetadata:
    record_id: UUID
    envelope: EventEnvelope
    received_at: datetime
    content_digest: str
    initial_projection_status: str
    initial_issues: tuple[str, ...]


@dataclass(frozen=True)
class ProtectedPayload:
    format_version: int
    crypto_profile_id: str
    key_id: str
    key_version: int
    nonce: bytes = field(repr=False)
    ciphertext: bytes = field(repr=False)


@dataclass(frozen=True)
class StoredEvent:
    metadata: RecordMetadata
    protection: ProtectedPayload


@dataclass(frozen=True)
class EventReceipt:
    record_id: UUID
    source_id: str
    event_id: str
    status: str
    received_at: datetime
    projection_status: str
    issues: tuple[str, ...]
