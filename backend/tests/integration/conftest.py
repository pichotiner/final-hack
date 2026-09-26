import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


@pytest.fixture(scope="session")
def database():
    value = os.environ.get("TEST_DATABASE_URL")
    if not value:
        pytest.skip("Set TEST_DATABASE_URL or use scripts/test_postgres.py for PostgreSQL tests.")
    url = make_url(value)
    if not url.database or not url.database.endswith("_test"):
        pytest.fail("TEST_DATABASE_URL must point to a dedicated database ending in _test.")
    schema = f"qc_test_{uuid4().hex}"
    writer_role = f"qc_writer_{uuid4().hex}"
    root = create_engine(url, hide_parameters=True)
    with root.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(text(f'CREATE ROLE "{writer_role}" NOLOGIN'))
    options = f"-csearch_path={schema},public"
    admin = create_engine(url, connect_args={"options": options}, hide_parameters=True)
    writer = create_engine(
        url,
        connect_args={"options": f"{options} -crole={writer_role}"},
        hide_parameters=True,
        pool_size=8,
        max_overflow=0,
    )
    try:
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        with admin.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{writer_role}"'))
            connection.execute(text(f'GRANT SELECT, INSERT ON raw_events TO "{writer_role}"'))
            connection.execute(
                text(f'GRANT SELECT, INSERT, UPDATE ON event_projections TO "{writer_role}"')
            )
            connection.execute(
                text(f'GRANT SELECT, INSERT, UPDATE ON item_projections TO "{writer_role}"')
            )
            connection.execute(
                text(f'GRANT SELECT, INSERT ON item_projection_versions TO "{writer_role}"')
            )
            connection.execute(
                text(
                    f"GRANT SELECT, INSERT, UPDATE ON quality_items, quality_bases, "
                    f'quality_commands, quality_decisions, quality_outbox TO "{writer_role}"'
                )
            )
            connection.execute(
                text(f'GRANT SELECT, INSERT, UPDATE ON quality_constraints TO "{writer_role}"')
            )
            connection.execute(
                text(f'GRANT SELECT, INSERT, UPDATE ON quality_deliveries TO "{writer_role}"')
            )
            connection.execute(
                text(f'GRANT SELECT, INSERT ON quality_context_versions TO "{writer_role}"')
            )
            connection.execute(
                text(f'GRANT SELECT, INSERT ON quality_erp_imports TO "{writer_role}"')
            )
            connection.execute(
                text(
                    f"GRANT SELECT, INSERT ON quality_state_versions, "
                    f'quality_external_streams TO "{writer_role}"'
                )
            )
        yield admin, writer, config
    finally:
        writer.dispose()
        admin.dispose()
        with root.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            connection.execute(text(f'DROP ROLE "{writer_role}"'))
        root.dispose()
