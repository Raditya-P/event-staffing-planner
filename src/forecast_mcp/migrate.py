"""Run database migrations from code (at start-up) or from the command line.

    uv run alembic revision --autogenerate -m "describe the change"   # after editing db.py
    uv run alembic upgrade head                                        # apply (the server also does this at start)
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from .config import settings

MIGRATIONS = Path(__file__).parent / "migrations"
LOCK_ID = 7_202_610  # any constant: serialises migrations when several instances start together


def alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    return cfg


def upgrade(engine) -> None:
    cfg = alembic_config()
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": LOCK_ID})
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
