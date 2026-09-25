import os

from alembic import context
from sqlalchemy import create_engine, pool

from app.adapters.postgres import metadata

config = context.config


def run(connection) -> None:
    context.configure(connection=connection, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(
        dialect_name="postgresql",
        target_metadata=metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
elif config.attributes.get("connection") is not None:
    run(config.attributes["connection"])
else:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "Set DATABASE_URL for migrations; do not put credentials in alembic.ini."
        )
    engine = create_engine(url, poolclass=pool.NullPool, hide_parameters=True)
    try:
        with engine.connect() as connection:
            run(connection)
    finally:
        engine.dispose()
