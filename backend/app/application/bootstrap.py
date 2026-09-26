"""Fail-closed composition for the future generated HTTP adapter."""

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from app.adapters.production import PostgresProductionJournal
from app.adapters.protection import AesGcmProtector
from app.application.production import IdentityDirectory, Principal, ProductionService


def _required(environment, name):
    value = environment.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required.")
    return value


@dataclass(frozen=True)
class ProductionSettings:
    database_url: str
    event_key_file: Path | None
    event_key_id: str | None
    event_key_version: int | None
    identities_file: Path
    keyring_file: Path | None = None

    @classmethod
    def from_environment(cls, environment):
        if environment.get("QC_REQUIRE_TLS") == "1":
            url = make_url(_required(environment, "DATABASE_URL"))
            if url.query.get("sslmode") != "verify-full" or not url.query.get("sslrootcert"):
                raise ValueError("This deployment requires PostgreSQL verify-full and a CA file.")
        if environment.get("QC_KEYRING_FILE"):
            return cls(
                _required(environment, "DATABASE_URL"),
                None,
                None,
                None,
                Path(_required(environment, "QC_IDENTITIES_FILE")),
                Path(_required(environment, "QC_KEYRING_FILE")),
            )
        try:
            version = int(_required(environment, "QC_EVENT_KEY_VERSION"))
        except ValueError as error:
            raise ValueError("QC_EVENT_KEY_VERSION must be a positive integer.") from error
        if version < 1:
            raise ValueError("QC_EVENT_KEY_VERSION must be a positive integer.")
        return cls(
            database_url=_required(environment, "DATABASE_URL"),
            event_key_file=Path(_required(environment, "QC_EVENT_KEY_FILE")),
            event_key_id=_required(environment, "QC_EVENT_KEY_ID"),
            event_key_version=version,
            identities_file=Path(_required(environment, "QC_IDENTITIES_FILE")),
        )


@dataclass
class ProductionRuntime:
    engine: object
    service: ProductionService

    def close(self):
        self.engine.dispose()


def _read_key(path):
    try:
        encoded = path.read_text(encoding="ascii").strip()
        key = base64.b64decode(encoded, validate=True)
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("The event key secret cannot be loaded.") from error
    if len(key) != 32:
        raise ValueError("The event key secret must contain one base64-encoded AES-256 key.")
    return key


def load_protector(settings):
    """Load the active key and retained versions before opening any DB connection."""
    if settings.keyring_file is None:
        return AesGcmProtector(
            {
                (settings.event_key_id, settings.event_key_version): _read_key(
                    settings.event_key_file
                )
            },
            active_key=(settings.event_key_id, settings.event_key_version),
        )
    try:
        path = settings.keyring_file
        document = json.loads(path.read_text(encoding="utf-8"))
        active = (document["active"]["key_id"], document["active"]["version"])
        keys = {}
        for entry in document["keys"]:
            identity = (entry["key_id"], entry["version"])
            if (
                not isinstance(identity[0], str)
                or not identity[0].strip()
                or type(identity[1]) is not int
                or identity[1] < 1
                or identity in keys
            ):
                raise ValueError
            keys[identity] = _read_key(path.parent / entry["file"])
        if type(active[1]) is not int or active not in keys:
            raise ValueError
        return AesGcmProtector(keys, active_key=active)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as error:
        raise ValueError(
            "The event keyring is invalid or a retained key is unavailable."
        ) from error


def _read_identities(path):
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("The identity registry cannot be loaded.") from error
    if not isinstance(document, list) or not document:
        raise ValueError("The identity registry must be a non-empty array.")
    entries = {}
    for value in document:
        if not isinstance(value, dict):
            raise ValueError("Each identity registry entry must be an object.")
        digest, actor = value.get("token_sha256"), value.get("actor_id")
        roles = value.get("roles")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not isinstance(actor, str)
            or not actor.strip()
            or not isinstance(roles, list)
            or not roles
            or any(not isinstance(role, str) or not role for role in roles)
            or digest in entries
        ):
            raise ValueError("The identity registry contains an invalid or duplicate entry.")
        entries[digest] = Principal(actor, frozenset(roles), value.get("source_id"))
    return MappingProxyType(entries)


def build_runtime(settings):
    protector = load_protector(settings)
    identities = _read_identities(settings.identities_file)
    engine = create_engine(settings.database_url, hide_parameters=True)
    journal = PostgresProductionJournal(engine, protector)
    return ProductionRuntime(
        engine,
        ProductionService(journal, protector, IdentityDirectory(identities)),
    )
