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


class StaleRevision(QualityError):
    code = "stale_revision"


class BasisMismatch(QualityError):
    code = "basis_mismatch"


class InvalidTransition(QualityError):
    code = "invalid_transition"


class ReleaseBlocked(QualityError):
    code = "release_blocked"

    def __init__(self, blockers: tuple[str, ...] | list[str]) -> None:
        self.blockers = tuple(blockers)
        super().__init__("Release is blocked by the current quality context.")


class IdempotencyConflict(QualityError):
    code = "idempotency_conflict"


class AckMismatch(QualityError):
    code = "ack_mismatch"


class UnknownEntity(QualityError):
    code = "unknown_entity"


class AuthenticationRequired(QualityError):
    code = "authentication_required"


class PermissionDenied(QualityError):
    code = "permission_denied"
