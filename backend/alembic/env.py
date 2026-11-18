"""Controller invokes migrations explicitly; application startup never does."""

import os

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import ArgumentError

from app.db import Base
from app.models import __all__ as registered_models

assert registered_models  # Import registers all tables; no runtime DB connection.
target_metadata = Base.metadata


def database_url() -> str:
    value = os.environ.get("DATABASE_URL", "")
    try:
        url = make_url(value)
        if url.drivername != "postgresql+psycopg" or not url.host or not url.database:
            raise ValueError
    except (ArgumentError, ValueError, TypeError):
        raise RuntimeError("Missing or invalid setting: DATABASE_URL") from None
    return value


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Tests provide a connection inside a disposable schema and outer rollback.
    supplied_connection = context.config.attributes.get("connection")
    if supplied_connection is not None:
        run_on_connection(supplied_connection)
        return
    # Avoid ConfigParser interpolation: passwords can legitimately contain '%'.
    engine = create_engine(
        database_url(),
        poolclass=pool.NullPool,
        connect_args={"connect_timeout": 5},
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            run_on_connection(connection)
    finally:
        engine.dispose()


def run_on_connection(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
