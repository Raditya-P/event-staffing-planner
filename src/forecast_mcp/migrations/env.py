"""Alembic environment: runs migrations against the app's database (Neon or the local SQLite file)."""

from alembic import context

from forecast_mcp.config import settings
from forecast_mcp.db import Base

config = context.config
target_metadata = Base.metadata


def _configure(**kwargs) -> None:
    context.configure(target_metadata=target_metadata, compare_type=True, **kwargs)


def run_offline() -> None:
    _configure(url=settings.database_url, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _run(connection) -> None:
    # SQLite cannot ALTER most things in place; batch mode rebuilds tables when needed.
    _configure(connection=connection, render_as_batch=connection.dialect.name == "sqlite")
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:  # called from forecast_mcp.migrate with an open connection
        _run(connection)
        return
    from forecast_mcp.db import engine

    with engine.begin() as conn:
        _run(conn)


if context.is_offline_mode():
    run_offline()
else:
    run_online()
