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
        "identities.json": json.dumps(identities, indent=2),
        "demo-tokens.json": json.dumps(tokens, indent=2),
        "erp.token": secrets.token_urlsafe(32),
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
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--directory", type=Path, default=Path("../.local"))
    commands.add_parser("migrate")
    args = parser.parse_args()
    if args.command == "init":
        initialize(args.directory)
        print(f"Created synthetic credentials in {args.directory.resolve()}")
    else:
        migrate()


if __name__ == "__main__":
    main()
