"""Provision local synthetic credentials and restricted application database role."""

import argparse
import base64
import hashlib
import json
import os
import secrets
from pathlib import Path

from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import create_engine, text


def initialize(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    users = {
        "controller": ("controller", None),
        "master": ("master", None),
        "manager": ("manager", None),
        "integration": ("integration", None),
        "vision": ("source", "vision-1"),
        "mes": ("source", "mes-source"),
        "erp": ("source", "erp-source"),
        "vision-demo": ("source", "vision-emulator-1"),
        "line-demo": ("source", "line-emulator-1"),
    }
    tokens = {name: secrets.token_urlsafe(32) for name in users}
    identities = [
        {
            "actor_id": name,
            "roles": [role],
            "source_id": source,
            "token_sha256": hashlib.sha256(tokens[name].encode()).hexdigest(),
        }
        for name, (role, source) in users.items()
    ]
    files = {
        "event.key": base64.b64encode(secrets.token_bytes(32)).decode(),
        "keyring.json": json.dumps(
            {
                "active": {"key_id": "demo-key", "version": 1},
                "keys": [{"key_id": "demo-key", "version": 1, "file": "event.key"}],
            },
            indent=2,
        ),
        "identities.json": json.dumps(identities, indent=2),
        "demo-tokens.json": json.dumps(tokens, indent=2),
        "erp.token": secrets.token_urlsafe(32),
        "import.token": tokens["integration"],
        "compose.env": (
            f"POSTGRES_PASSWORD={secrets.token_urlsafe(32)}\n"
            f"QC_APP_PASSWORD={secrets.token_urlsafe(32)}\n"
            f"QC_UID={os.getuid() if hasattr(os, 'getuid') else 10001}\n"
            f"QC_GID={os.getgid() if hasattr(os, 'getgid') else 10001}\n"
        ),
    }
    for name, value in files.items():
        path = directory / name
        with path.open("x", encoding="utf-8") as stream:
            stream.write(value + "\n")
        path.chmod(0o600)


def rotate_key(directory):
    manifest = directory / "keyring.json"
    value = json.loads(manifest.read_text("utf-8"))
    key_id = value["active"]["key_id"]
    version = max(entry["version"] for entry in value["keys"] if entry["key_id"] == key_id) + 1
    filename = f"event-v{version}.key"
    with (directory / filename).open("x", encoding="ascii") as stream:
        stream.write(base64.b64encode(secrets.token_bytes(32)).decode() + "\n")
    (directory / filename).chmod(0o600)
    value["keys"].append({"key_id": key_id, "version": version, "file": filename})
    value["active"] = {"key_id": key_id, "version": version}
    _atomic_json(manifest, value)
    return version


def _atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".new")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)


def rotate_identity(directory, actor):
    path = directory / "identities.json"
    entries = json.loads(path.read_text("utf-8"))
    matches = [entry for entry in entries if entry["actor_id"] == actor]
    if not matches or any(
        (e["roles"], e.get("source_id")) != (matches[0]["roles"], matches[0].get("source_id"))
        for e in matches
    ):
        raise ValueError("Select an existing actor with one role/source assignment.")
    token = secrets.token_urlsafe(32)
    credential = directory / ("credential-" + secrets.token_hex(8) + ".token")
    with credential.open("x", encoding="ascii") as stream:
        stream.write(token + "\n")
    credential.chmod(0o600)
    entries = [entry for entry in entries if entry["actor_id"] != actor]
    entries.append({**matches[0], "token_sha256": hashlib.sha256(token.encode()).hexdigest()})
    _atomic_json(path, entries)
    return credential


def migrate():
    engine = create_engine(os.environ["DATABASE_URL"], hide_parameters=True)
    try:
        with engine.begin() as connection:
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            password = os.environ["QC_APP_PASSWORD"]
            if not password:
                raise ValueError("QC_APP_PASSWORD is required.")
            exists = connection.scalar(text("SELECT 1 FROM pg_roles WHERE rolname='qc_app'"))
            verb = "ALTER" if exists else "CREATE"
            statement = sql.SQL(verb + " ROLE qc_app LOGIN PASSWORD {}").format(
                sql.Literal(password)
            )
            connection.exec_driver_sql(statement.as_string(connection.connection.driver_connection))
            connection.execute(text("GRANT USAGE ON SCHEMA public TO qc_app"))
            connection.execute(
                text("""GRANT SELECT, INSERT ON raw_events,
                item_projection_versions, quality_bases, quality_commands, quality_decisions,
                quality_erp_imports,
                quality_context_versions, quality_state_versions, quality_external_streams
                TO qc_app""")
            )
            connection.execute(
                text("""GRANT SELECT, INSERT, UPDATE ON event_projections,
                item_projections, quality_items, quality_outbox, quality_constraints,
                quality_deliveries TO qc_app""")
            )
        from app.adapters.quality import PostgresQualityWorkflow

        PostgresQualityWorkflow(engine).upgrade_storage()
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--directory", type=Path, default=Path("../.local"))
    rotate = commands.add_parser("rotate-key")
    rotate.add_argument("--directory", type=Path, default=Path("../.local"))
    identity = commands.add_parser("rotate-identity")
    identity.add_argument("--directory", type=Path, default=Path("../.local"))
    identity.add_argument("--actor", required=True)
    commands.add_parser("migrate")
    args = parser.parse_args()
    if args.command == "init":
        initialize(args.directory)
        print(f"Created synthetic credentials in {args.directory.resolve()}")
    elif args.command == "rotate-key":
        print(
            f"Active key version: {rotate_key(args.directory)}. "
            "Retained keys preserved; restart API/worker."
        )
    elif args.command == "rotate-identity":
        print(f"New credential: {rotate_identity(args.directory, args.actor)}. Restart API/worker.")
    else:
        migrate()


if __name__ == "__main__":
    main()
