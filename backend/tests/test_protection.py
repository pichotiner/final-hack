import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.adapters.protection import AesGcmProtector
from app.domain.errors import IntegrityViolation, KeyUnavailable, UnsupportedCryptoProfile
from app.domain.events import RecordMetadata


@pytest.fixture
def record_metadata(codec, event_bytes):
    event = codec.decode(event_bytes)
    return RecordMetadata(
        record_id=uuid4(),
        envelope=event.envelope,
        received_at=datetime(2026, 9, 25, 11, tzinfo=UTC),
        content_digest=event.content_digest,
        initial_projection_status="unresolved",
        initial_issues=("domain_projection_not_implemented",),
    )


def test_original_round_trip_and_randomized_ciphertext(protector, record_metadata, event_bytes):
    first = protector.seal(event_bytes, record_metadata)
    second = protector.seal(event_bytes, record_metadata)
    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext
    assert event_bytes not in first.ciphertext
    assert protector.open(first, record_metadata) == event_bytes


@pytest.mark.parametrize(
    "change",
    [
        lambda m: replace(m, record_id=uuid4()),
        lambda m: replace(m, received_at=m.received_at + timedelta(seconds=1)),
        lambda m: replace(m, content_digest="0" * 64),
        lambda m: replace(m, initial_issues=()),
        lambda m: replace(m, initial_projection_status="applied"),
        lambda m: replace(m, envelope=replace(m.envelope, item_id="other-item")),
        lambda m: replace(m, envelope=replace(m.envelope, source_id="other-source")),
    ],
)
def test_record_metadata_is_authenticated(protector, record_metadata, event_bytes, change):
    sealed = protector.seal(event_bytes, record_metadata)
    with pytest.raises(IntegrityViolation):
        protector.open(sealed, change(record_metadata))


def test_corruption_and_wrong_key_are_detected(protector, record_metadata, event_bytes):
    sealed = protector.seal(event_bytes, record_metadata)
    corrupt = replace(sealed, ciphertext=bytes([sealed.ciphertext[0] ^ 1]) + sealed.ciphertext[1:])
    with pytest.raises(IntegrityViolation):
        protector.open(corrupt, record_metadata)
    wrong = AesGcmProtector({("test", 1): os.urandom(32)}, active_key=("test", 1))
    with pytest.raises(IntegrityViolation):
        wrong.open(sealed, record_metadata)
    with pytest.raises(UnsupportedCryptoProfile):
        protector.open(replace(sealed, crypto_profile_id="unknown"), record_metadata)


def test_key_rotation_keeps_old_records_readable(record_metadata, event_bytes):
    old_key, new_key = os.urandom(32), os.urandom(32)
    old = AesGcmProtector({("journal", 1): old_key}, active_key=("journal", 1))
    before = old.seal(event_bytes, record_metadata)
    rotated = AesGcmProtector(
        {("journal", 1): old_key, ("journal", 2): new_key}, active_key=("journal", 2)
    )
    after = rotated.seal(event_bytes, record_metadata)
    assert before.key_version == 1 and after.key_version == 2
    assert (
        rotated.open(before, record_metadata) == rotated.open(after, record_metadata) == event_bytes
    )
    missing = AesGcmProtector({("journal", 2): new_key}, active_key=("journal", 2))
    with pytest.raises(KeyUnavailable):
        missing.open(before, record_metadata)
