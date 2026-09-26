"""Fail-closed composition for the future generated HTTP adapter."""

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from sqlalchemy import create_engine

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
    event_key_file: Path
    event_key_id: str
    event_key_version: int
    identities_file: Path

    @classmethod
    def from_environment(cls, environment):
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
    key = _read_key(settings.event_key_file)
    identities = _read_identities(settings.identities_file)
    engine = create_engine(settings.database_url, hide_parameters=True)
    protector = AesGcmProtector(
        {(settings.event_key_id, settings.event_key_version): key},
        active_key=(settings.event_key_id, settings.event_key_version),
    )
    journal = PostgresProductionJournal(engine, protector)
    return ProductionRuntime(
        engine,
        ProductionService(journal, protector, IdentityDirectory(identities)),
    )
