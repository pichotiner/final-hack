class QualityError(Exception):
    """Safe public message; never includes the original event or key material."""

    code = "quality_error"


class InvalidEvent(QualityError):
    code = "validation_error"


class UnsupportedSchemaVersion(InvalidEvent):
    code = "unsupported_schema_version"


class InvalidEnum(InvalidEvent):
    code = "invalid_enum"


class SourceMismatch(QualityError):
    code = "source_mismatch"


class EventConflict(QualityError):
    code = "event_conflict"


class IntegrityViolation(QualityError):
    code = "integrity_violation"


class KeyUnavailable(QualityError):
    code = "key_unavailable"


class UnsupportedCryptoProfile(QualityError):
    code = "unsupported_crypto_profile"
