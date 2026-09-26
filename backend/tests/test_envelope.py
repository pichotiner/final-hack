import json
from datetime import UTC, datetime

import pytest

from app.adapters.json_events import JsonEventCodec
from app.domain.errors import InvalidEnum, InvalidEvent, UnsupportedSchemaVersion


def encode(document):
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


def test_canonical_idempotency_preserves_original_bytes(codec, event_document):
    event_document["future_optional"] = {"note": "наблюдение", "values": [1, 2]}
    compact = encode(event_document)
    reordered = json.dumps(dict(reversed(list(event_document.items()))), indent=2).encode()
    first, second = codec.decode(compact), codec.decode(reordered)
    assert first.content_digest == second.content_digest
    assert first.original_json == compact
    assert second.original_json == reordered
    event_document["future_optional"]["values"] = [2, 1]
    assert codec.decode(encode(event_document)).content_digest != first.content_digest


@pytest.mark.parametrize(
    "raw",
    [
        b'{"event_id":"a","event_id":"b"}',
        b'{"payload":{"a":1,"a":2}}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":-Infinity}',
        b"[]",
        b"null",
        b"{",
        b'"\xff"',
    ],
)
def test_ambiguous_or_invalid_json_is_rejected(codec, raw):
    with pytest.raises(InvalidEvent):
        codec.decode(raw)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("schema_version", "2.0", UnsupportedSchemaVersion),
        ("event_type", "controller_decision", InvalidEnum),
        ("event_type", [], InvalidEnum),
        ("source_id", "", InvalidEvent),
        ("event_id", 1, InvalidEvent),
        ("event_id", "x" * 129, InvalidEvent),
        ("occurred_at", "2026-09-25T10:00:00", InvalidEvent),
        ("occurred_at", "2026-02-30T10:00:00Z", InvalidEvent),
        ("occurred_at", "2026-09-25T10:00:00+00:99", InvalidEvent),
        ("payload", [], InvalidEvent),
        ("identification_status", "guessed", InvalidEnum),
        ("time_quality", "guessed", InvalidEnum),
        ("item_id", None, InvalidEvent),
    ],
)
def test_common_envelope_validation(codec, event_document, field, value, error):
    event_document[field] = value
    with pytest.raises(error):
        codec.decode(encode(event_document))


def test_missing_field_server_timestamp_and_unsafe_numbers(codec, event_document):
    missing = dict(event_document)
    del missing["item_id"]
    with pytest.raises(InvalidEvent):
        codec.decode(encode(missing))
    with pytest.raises(InvalidEvent):
        codec.decode(encode({**event_document, "received_at": "2026-09-25T10:00:00Z"}))
    with pytest.raises(InvalidEvent):
        codec.decode(encode({**event_document, "future_count": 2**60}))


def test_unknown_identity_is_explicit_and_time_is_normalized(codec, event_document):
    event_document["item_id"] = None
    del event_document["identification_status"]
    del event_document["time_quality"]
    event_document["occurred_at"] = "2026-09-25T13:00:00+03:00"
    event = codec.decode(encode(event_document))
    assert event.envelope.item_id is None
    assert event.envelope.identification_status == "uncertain"
    assert event.envelope.time_quality == "uncertain"
    assert event.envelope.occurred_at == datetime(2026, 9, 25, 10, tzinfo=UTC)


def test_size_limit(event_bytes):
    with pytest.raises(InvalidEvent):
        JsonEventCodec(max_bytes=len(event_bytes) - 1).decode(event_bytes)
