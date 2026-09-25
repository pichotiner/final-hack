import os
from dataclasses import asdict
from datetime import UTC, datetime

import rfc8785
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.domain.errors import IntegrityViolation, KeyUnavailable, UnsupportedCryptoProfile
from app.domain.events import ProtectedPayload, RecordMetadata

PROFILE = "aes-256-gcm-v1"


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _aad(metadata: RecordMetadata, *, key_id: str, key_version: int) -> bytes:
    values = asdict(metadata)
    values["record_id"] = str(metadata.record_id)
    values["received_at"] = _timestamp(metadata.received_at)
    values["envelope"]["occurred_at"] = _timestamp(metadata.envelope.occurred_at)
    return rfc8785.dumps(
        {
            "format_version": 1,
            "crypto_profile_id": PROFILE,
            "key_id": key_id,
            "key_version": key_version,
            "metadata": values,
        }
    )


class AesGcmProtector:
    """AEAD protection with injected keys; no keys or plaintext are logged.

    This authenticates individual records, not completeness of an entire journal.
    A privileged DB administrator can still remove records or disable triggers.
    """

    def __init__(self, keys: dict[tuple[str, int], bytes], *, active_key: tuple[str, int]) -> None:
        for (key_id, version), key in keys.items():
            if not key_id or version < 1 or len(key) != 32:
                raise ValueError(
                    "AES-256 keys require a non-empty ID, positive version and 32 bytes."
                )
        self._keys = dict(keys)
        self._active_key = active_key
        self._get_key(*active_key)

    def _get_key(self, key_id: str, version: int) -> bytes:
        try:
            return self._keys[(key_id, version)]
        except KeyError:
            raise KeyUnavailable("The required encryption key is unavailable.") from None

    def seal(self, raw: bytes, metadata: RecordMetadata) -> ProtectedPayload:
        key_id, version = self._active_key
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._get_key(key_id, version)).encrypt(
            nonce, raw, _aad(metadata, key_id=key_id, key_version=version)
        )
        return ProtectedPayload(1, PROFILE, key_id, version, nonce, ciphertext)

    def open(self, protection: ProtectedPayload, metadata: RecordMetadata) -> bytes:
        if protection.format_version != 1 or protection.crypto_profile_id != PROFILE:
            raise UnsupportedCryptoProfile("The stored protection profile is not supported.")
        key = self._get_key(protection.key_id, protection.key_version)
        try:
            return AESGCM(key).decrypt(
                protection.nonce,
                protection.ciphertext,
                _aad(metadata, key_id=protection.key_id, key_version=protection.key_version),
            )
        except (InvalidTag, ValueError) as error:
            raise IntegrityViolation("The stored event failed authentication.") from error
