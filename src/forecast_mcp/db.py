"""Database tables: the single source of truth both front doors read and write.

Works on Neon (Postgres) in deployment and on a local SQLite file when no
DATABASE_URL is set. Every top-level row carries a workspace_id, and every
lookup checks it, so each signed-in user only sees their own workspace.
The schema is created and changed by Alembic migrations (forecast_mcp/migrations).
"""

from __future__ import annotations

import secrets
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any, Iterator

from sqlalchemy import (
    JSON,
    Index,
    MetaData,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from .config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


# Stable constraint names, so migrations can alter them on Postgres and SQLite alike.
NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    """A person who signed in. Identified by the identity provider's issuer + subject, never by email alone."""

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("issuer", "subject"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("usr"))
    issuer: Mapped[str] = mapped_column(String(300))
    subject: Mapped[str] = mapped_column(String(300))
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Membership(Base):
    __tablename__ = "memberships"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), primary_key=True, index=True)
    role: Mapped[str] = mapped_column(String(20), default="owner")  # owner | planner | viewer
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Venue(Base):
    __tablename__ = "venues"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ven"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64))
    open_hour: Mapped[int] = mapped_column(Integer)
    close_hour: Mapped[int] = mapped_column(Integer)
    service_rate_per_lane: Mapped[float] = mapped_column(Float)
    wage_per_staff_hour: Mapped[float] = mapped_column(Float)
    history_version: Mapped[int] = mapped_column(Integer, default=1)
    # Venues whose history is identical (every demo copy) share one trained forecast model.
    history_fingerprint: Mapped[str | None] = mapped_column(String(80), nullable=True)
    gates: Mapped[list["Gate"]] = relationship(back_populates="venue", order_by="Gate.position")


class Gate(Base):
    __tablename__ = "gates"
    __table_args__ = (UniqueConstraint("venue_id", "id"),)
    pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    venue_id: Mapped[str] = mapped_column(ForeignKey("venues.id"), index=True)
    id: Mapped[str] = mapped_column(String(40))  # short id Claude and planners use, unique per venue
    name: Mapped[str] = mapped_column(String(200))
    lanes: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer, default=0)
    venue: Mapped[Venue] = relationship(back_populates="gates")


class Observation(Base):
    """Historical arrivals per gate and hour (synthetic for now)."""

    __tablename__ = "observations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    venue_id: Mapped[str] = mapped_column(ForeignKey("venues.id"), index=True)
    gate_id: Mapped[str] = mapped_column(String(40))
    day: Mapped[date] = mapped_column(Date)
    hour: Mapped[int] = mapped_column(Integer)
    arrivals: Mapped[int] = mapped_column(Integer)
    day_type: Mapped[str] = mapped_column(String(20))
    weather: Mapped[str] = mapped_column(String(20))
    show_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    competing: Mapped[int] = mapped_column(Integer, default=0)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("evt"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    venue_id: Mapped[str] = mapped_column(ForeignKey("venues.id"))
    name: Mapped[str] = mapped_column(String(200))
    day: Mapped[date] = mapped_column(Date)
    day_type: Mapped[str] = mapped_column(String(20))
    weather: Mapped[str] = mapped_column(String(20))
    show_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Bumped on every change, so views can poll one number to know when to refresh.
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    venue: Mapped[Venue] = relationship()


class Scenario(Base):
    __tablename__ = "scenarios"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("scn"))
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))  # official | what_if
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | discarded | archived
    parent_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    selected_solution_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    selected_by_planner: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(200))
    created_via: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    promoted_by: Mapped[str | None] = mapped_column(String(200), nullable=True)


class Note(Base):
    """A planner's words, kept exactly as written."""

    __tablename__ = "notes"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("note"))
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    author: Mapped[str] = mapped_column(String(200))
    channel: Mapped[str] = mapped_column(String(20))  # chat | dashboard
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConstraintRow(Base):
    __tablename__ = "constraints"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("con"))
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), index=True)
    # While pending: the scenario it is meant for (None = a new what-if). Once active: the scenario it is in.
    scenario_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    note_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    copied_from: Mapped[str | None] = mapped_column(String(40), nullable=True)
    type: Mapped[str] = mapped_column(String(40))
    params: Mapped[dict[str, Any]] = mapped_column(JSON)
    interpretation: Mapped[str] = mapped_column(Text, default="")
    assumptions: Mapped[list[Any]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20))  # pending | active | rejected | removed
    proposed_by: Mapped[str] = mapped_column(String(200))
    proposed_via: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    decided_via: Mapped[str | None] = mapped_column(String(20), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ScenarioEdit(Base):
    """Undo history for what-if scenarios."""

    __tablename__ = "scenario_edits"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scenario_id: Mapped[str] = mapped_column(String(40), index=True)
    op: Mapped[str] = mapped_column(String(40))  # add_constraint | remove_constraint | select_plan
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(200))
    undone: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Run(Base):
    """One forecast + optimization pass for a scenario. Also the work queue: workers claim queued rows."""

    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_status_created", "status", "created_at"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("run"))
    scenario_id: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued | running | done | failed
    stage: Mapped[str] = mapped_column(String(40), default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    trigger: Mapped[str] = mapped_column(String(200), default="")
    claimed_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    forecast: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditEntry(Base):
    """Who did what, when, through which front door, and from which planner note."""

    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    event_id: Mapped[str] = mapped_column(String(40), index=True)
    actor: Mapped[str] = mapped_column(String(200))
    channel: Mapped[str] = mapped_column(String(20))  # chat | chat_panel | dashboard | system
    action: Mapped[str] = mapped_column(String(60))
    target_type: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[str] = mapped_column(String(40))
    note_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def make_engine(url: str):
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return engine
    # Neon suspends idle compute and drops connections; check them before use and recycle often.
    # Neon's "-pooler" host is PgBouncer in transaction mode: server-side prepared statements are not safe there.
    connect_args = {"prepare_threshold": None} if "-pooler" in url else {}
    return create_engine(url, pool_pre_ping=True, pool_recycle=300, pool_size=5, max_overflow=5,
                         connect_args=connect_args)


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def configure(url: str) -> None:
    """Point the app at another database (used by tests)."""
    global engine
    engine.dispose()
    engine = make_engine(url)
    SessionLocal.configure(bind=engine)


def init_db() -> None:
    """Bring the schema up to date by running any pending migrations."""
    from .migrate import upgrade

    upgrade(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:  # SQLite drops the zone; we only ever store UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat(timespec="seconds")
