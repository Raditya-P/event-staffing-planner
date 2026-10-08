"""Demo data: one fictional theme park, its synthetic history and two event days."""

from __future__ import annotations

from datetime import date

from sqlalchemy import insert, select

from .db import Event, Gate, Observation, Scenario, Venue, Workspace, session_scope
from .engines.synthetic import DEMO_VENUE, generate_history
from .services import request_run

HISTORY_LAST_DAY = date(2026, 10, 6)
# Every demo copy has identical history, so they can share one trained model.
DEMO_FINGERPRINT = f"demo-synthetic-v1-{HISTORY_LAST_DAY.isoformat()}"
DEMO_EVENTS = [
    {"name": "Halloween Night", "day": date(2026, 10, 31), "weather": "cloudy", "show_start": 19},
    {"name": "Halloween Sunday", "day": date(2026, 11, 1), "weather": "sunny", "show_start": 19},
]


def ensure_demo_data(workspace_id: str = "demo", actor: str = "system", create_workspace: bool = True) -> bool:
    """Put the demo theme park into a workspace (creating the workspace unless told it exists).
    Returns True if anything was created."""
    with session_scope() as s:
        if create_workspace:
            if s.get(Workspace, workspace_id) is not None:
                return False
            s.add(Workspace(id=workspace_id, name="Demo workspace"))
            s.flush()
        elif s.scalars(select(Venue.id).where(Venue.workspace_id == workspace_id)).first() is not None:
            return False
        venue = Venue(
            workspace_id=workspace_id,
            name=DEMO_VENUE.name,
            timezone=DEMO_VENUE.timezone,
            open_hour=DEMO_VENUE.open_hour,
            close_hour=DEMO_VENUE.close_hour,
            service_rate_per_lane=DEMO_VENUE.service_rate_per_lane,
            wage_per_staff_hour=DEMO_VENUE.wage_per_staff_hour,
            history_fingerprint=DEMO_FINGERPRINT,
        )
        s.add(venue)
        s.flush()
        for pos, g in enumerate(DEMO_VENUE.gates):
            s.add(Gate(venue_id=venue.id, id=g.id, name=g.name, lanes=g.lanes, position=pos))
        rows = generate_history(DEMO_VENUE, HISTORY_LAST_DAY)
        s.execute(insert(Observation), [{"venue_id": venue.id, **r} for r in rows])

        for spec in DEMO_EVENTS:
            day_type = "weekend" if spec["day"].weekday() >= 5 else "weekday"
            ev = Event(
                workspace_id=workspace_id,
                venue_id=venue.id,
                name=spec["name"],
                day=spec["day"],
                day_type=day_type,
                weather=spec["weather"],
                show_start=spec["show_start"],
            )
            s.add(ev)
            s.flush()
            official = Scenario(event_id=ev.id, name="Official plan", kind="official", created_by=actor, created_via="system")
            s.add(official)
            s.flush()
            request_run(s, official, "initial plan")
    return True


def demo_event_ids(workspace_id: str = "demo") -> list[str]:
    with session_scope() as s:
        return list(s.scalars(select(Event.id).where(Event.workspace_id == workspace_id).order_by(Event.day)))


def reset_workspace_demo(workspace_id: str) -> None:
    """Delete everything in a workspace's events and venues and load a fresh demo copy."""
    from sqlalchemy import delete

    from .db import AuditEntry, ConstraintRow, Note, Run, ScenarioEdit

    with session_scope() as s:
        event_ids = list(s.scalars(select(Event.id).where(Event.workspace_id == workspace_id)))
        scenario_ids = list(s.scalars(select(Scenario.id).where(Scenario.event_id.in_(event_ids)))) if event_ids else []
        venue_ids = list(s.scalars(select(Venue.id).where(Venue.workspace_id == workspace_id)))
        if scenario_ids:
            s.execute(delete(Run).where(Run.scenario_id.in_(scenario_ids)))
            s.execute(delete(ScenarioEdit).where(ScenarioEdit.scenario_id.in_(scenario_ids)))
        if event_ids:
            for model in (ConstraintRow, Note, Scenario):
                s.execute(delete(model).where(model.event_id.in_(event_ids)))
            s.execute(delete(AuditEntry).where(AuditEntry.event_id.in_(event_ids)))
            s.execute(delete(Event).where(Event.id.in_(event_ids)))
        if venue_ids:
            s.execute(delete(Observation).where(Observation.venue_id.in_(venue_ids)))
            s.execute(delete(Gate).where(Gate.venue_id.in_(venue_ids)))
            s.execute(delete(Venue).where(Venue.id.in_(venue_ids)))
    ensure_demo_data(workspace_id, create_workspace=False)
