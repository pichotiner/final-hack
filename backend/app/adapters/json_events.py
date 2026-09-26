import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

import rfc8785

from app.domain.errors import InvalidEnum, InvalidEvent, UnsupportedSchemaVersion
from app.domain.events import EventEnvelope, EventType, PreparedEvent

_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)"
)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidEvent("Duplicate JSON keys are not supported.")
        result[key] = value
    return result


def _invalid_constant(_: str) -> None:
    raise InvalidEvent("JSON numbers must be finite.")


def _identifier(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise InvalidEvent(f"{name} must be a non-empty identifier of at most 128 characters.")
    return value


class JsonEventCodec:
    """Validate the common envelope only; retain the complete original JSON.

    Type-specific payload validation belongs to the future generated DTO adapter.
    This module is not a replacement OpenAPI schema.
    """

    def __init__(self, max_bytes: int = 1_048_576) -> None:
        self.max_bytes = max_bytes

    def decode(self, raw: bytes) -> PreparedEvent:
        if len(raw) > self.max_bytes:
            raise InvalidEvent("The event exceeds the configured size limit.")
        try:
            data = json.loads(
                raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_invalid_constant
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise InvalidEvent("The event must be a valid UTF-8 JSON object.") from error
        if not isinstance(data, dict):
            raise InvalidEvent("The event must be a JSON object.")
        required = {
            "event_id",
            "event_type",
            "schema_version",
            "occurred_at",
            "source_id",
            "item_id",
            "payload",
        }
        if not required.issubset(data):
            raise InvalidEvent("Required envelope fields are missing.")
        if "received_at" in data:
            raise InvalidEvent("received_at is assigned by the server.")
        if data["schema_version"] != "1.0":
            raise UnsupportedSchemaVersion("Only event schema version 1.0 is supported.")
        if not isinstance(data["payload"], dict):
            raise InvalidEvent("payload must be a JSON object.")
        try:
            event_type = EventType(data["event_type"])
        except (ValueError, TypeError) as error:
            raise InvalidEnum("Unsupported source event type.") from error
        identification = data.get("identification_status", "uncertain")
        quality = data.get("time_quality", "uncertain")
        if identification not in ("reliable", "uncertain"):
            raise InvalidEnum("Unsupported identification status.")
        if quality not in ("synchronized", "uncertain"):
            raise InvalidEnum("Unsupported time quality.")
        source_id = _identifier(data["source_id"], "source_id")
        event_id = _identifier(data["event_id"], "event_id")
        item_id = _identifier(data["item_id"], "item_id", nullable=True)
        if item_id is None and identification == "reliable":
            raise InvalidEvent("A reliable identification requires item_id.")
        timestamp = data["occurred_at"]
        if not isinstance(timestamp, str) or not _TIMESTAMP.fullmatch(timestamp):
            raise InvalidEvent("occurred_at must be an RFC 3339 timestamp with a timezone.")
        try:
            occurred_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(UTC)
            canonical = rfc8785.dumps(data)
        except (ValueError, RecursionError) as error:
            raise InvalidEvent("The event contains an invalid timestamp or JSON value.") from error
        return PreparedEvent(
            envelope=EventEnvelope(
                source_id=source_id,
                event_id=event_id,
                schema_version="1.0",
                event_type=event_type,
                item_id=item_id,
                occurred_at=occurred_at,
                identification_status=identification,
                time_quality=quality,
            ),
            original_json=raw,
            content_digest=hashlib.sha256(canonical).hexdigest(),
        )
