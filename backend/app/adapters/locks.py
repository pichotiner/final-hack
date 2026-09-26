import hashlib
import json

from sqlalchemy import text


def lock(connection, namespace: str, *identity: str) -> None:
    encoded = json.dumps([namespace, *identity], ensure_ascii=False).encode("utf-8")
    key = int.from_bytes(hashlib.sha256(encoded).digest()[:8], "big", signed=True)
    connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def production_guard(connection) -> None:
    lock(connection, "production-guard", "demonstrator")
