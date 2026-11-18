"""SQLAlchemy 2.0 resources; transaction ownership belongs to domain services."""

from collections.abc import Generator

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Shared metadata for subsequent model and migration tasks."""


class Database:
    def __init__(self, database_url: str) -> None:
        # Engine creation is lazy: startup neither connects nor migrates.
        self.engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_timeout=5,
            connect_args={"connect_timeout": 5},
            hide_parameters=True,
        )
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=False)

    def dispose(self) -> None:
        self.engine.dispose()


def get_session(request: Request) -> Generator[Session, None, None]:
    database: Database = request.app.state.database
    with database.sessions() as session:
        # close() rolls back uncommitted work, including failed readiness queries.
        yield session
