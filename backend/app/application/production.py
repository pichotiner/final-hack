"""Authenticated internal entry point, ready for the shared v2 HTTP adapter."""

from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest
from uuid import NAMESPACE_URL, uuid5

from app.adapters.json_events import JsonEventCodec
from app.application.ingestion import IngestEvent
from app.domain.errors import AuthenticationRequired, InvalidEvent, PermissionDenied


@dataclass(frozen=True)
class Principal:
    actor_id: str
    roles: frozenset[str]
    source_id: str | None = None


class IdentityDirectory:
    """Injected token hashes; tokens and human identities never come from request bodies."""

    def __init__(self, entries: dict[str, Principal]):
        self.entries = dict(entries)

    def authenticate(self, token: str) -> Principal:
        if not isinstance(token, str) or not token:
            raise AuthenticationRequired("A service or user credential is required.")
        digest = sha256(token.encode()).hexdigest()
        for expected, principal in self.entries.items():
            if compare_digest(digest, expected):
                return principal
        raise AuthenticationRequired("Unknown credential.")


class ProductionService:
    def __init__(self, journal, protector, identities: IdentityDirectory):
        self.journal = journal
        self.quality = journal.quality
        self.identities = identities
        self.ingestion = IngestEvent(journal, protector, JsonEventCodec())

    def _require(self, token, roles):
        principal = self.identities.authenticate(token)
        if not principal.roles.intersection(roles):
            raise PermissionDenied("This identity cannot perform the requested action.")
        return principal

    def ingest(self, token: str, raw: bytes):
        principal = self._require(token, {"source"})
        if principal.source_id is None:
            raise PermissionDenied("The source identity is not registered.")
        return self.ingestion.execute(raw, authenticated_source_id=principal.source_id)

    def import_context(self, token, item_id, context, *, import_id):
        principal = self._require(token, {"integration"})
        return self.journal.import_context(
            item_id, context, import_id=import_id, actor_id=principal.actor_id
        )

    def import_work_order(self, token, order, mapper):
        principal = self._require(token, {"integration"})
        return self.journal.import_work_order(order, mapper, actor_id=principal.actor_id)

    def command(self, token: str, method: str, target_id: str, payload: dict):
        roles = {
            "decide_nonconformity": {"controller"},
            "resolve_nonconformity": {"controller"},
            "reopen_nonconformity": {"controller"},
            "dispose": {"controller"},
            "start_cycle": {"master"},
        }
        if method not in roles:
            raise InvalidEvent("Unsupported production command.")
        principal = self._require(token, roles[method])
        if "actor_id" in payload:
            raise InvalidEvent("The command author is assigned by the server.")
        # Current permissions are always checked, including retries of successful commands.
        command_payload = dict(payload)
        if method in {"resolve_nonconformity", "reopen_nonconformity"}:
            command_payload.pop("action", None)
        if method == "start_cycle":
            if "cycle_id" in payload:
                raise InvalidEvent("The control cycle ID is assigned by the server.")
            command_payload["cycle_id"] = str(
                uuid5(
                    NAMESPACE_URL,
                    repr((principal.actor_id, target_id, payload["idempotency_key"])),
                )
            )
        return self.quality.execute_command(
            method, target_id, command_payload, actor_id=principal.actor_id, document=payload
        )

    def state(self, token, item_id):
        self._require(token, {"controller", "master", "technologist", "manager", "admin"})
        return self.quality.state(item_id)

    def create_basis(self, token, item_id, target_id=None):
        self._require(token, {"controller", "master"})
        return self.quality.create_basis(item_id, target_id)

    def basis(self, token, item_id, basis_id):
        self._require(token, {"controller", "master", "technologist", "manager"})
        return self.quality.get_basis(item_id, basis_id)

    def resolve_scope(self, token, constraint_id, *, reason, idempotency_key, bases):
        principal = self._require(token, {"controller"})
        return self.journal.resolve_scope(
            constraint_id,
            actor_id=principal.actor_id,
            reason=reason,
            idempotency_key=idempotency_key,
            bases=bases,
        )
