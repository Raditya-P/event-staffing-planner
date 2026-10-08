"""Application rules shared by both front doors (MCP tools and the dashboard API).

Every change goes through here, so the rules from the design hold whichever
door a planner uses:
- the official plan changes only when a planner promotes a scenario;
- constraints from chat stay pending until a planner confirms them;
- direct dashboard edits apply to what-if scenarios and can be undone;
- every decision is written to the audit log with who, when, how and from which note.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import constraints as C
from .config import settings
from .db import AuditEntry, ConstraintRow, Event, Note, Run, Scenario, ScenarioEdit, iso, utcnow  # noqa: F401
from .engines.optimizer import explain_choice


class ServiceError(Exception):
    """A request that breaks a rule or does not fit the data. The message is meant for people (and Claude)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class NotFound(ServiceError):
    def __init__(self, what: str, ident: str):
        super().__init__(f"No {what} with id '{ident}'.", status=404)


@dataclass(frozen=True)
class Actor:
    name: str
    workspace_id: str
    channel: str  # chat | chat_panel | dashboard | system
    user_id: str | None = None


# ---------------------------------------------------------------- lookups


def _event(s: Session, actor: Actor, event_id: str) -> Event:
    ev = s.get(Event, event_id)
    if ev is None or ev.workspace_id != actor.workspace_id:
        raise NotFound("event", event_id)
    return ev


def _scenario(s: Session, actor: Actor, scenario_id: str) -> tuple[Scenario, Event]:
    sc = s.get(Scenario, scenario_id)
    if sc is None:
        raise NotFound("scenario", scenario_id)
    return sc, _event(s, actor, sc.event_id)


def _constraint(s: Session, actor: Actor, constraint_id: str) -> tuple[ConstraintRow, Event]:
    row = s.get(ConstraintRow, constraint_id)
    if row is None:
        raise NotFound("constraint", constraint_id)
    return row, _event(s, actor, row.event_id)


def official_scenario(s: Session, event_id: str) -> Scenario:
    sc = s.scalars(
        select(Scenario).where(Scenario.event_id == event_id, Scenario.kind == "official", Scenario.status == "active")
    ).first()
    if sc is None:
        raise ServiceError(f"Event {event_id} has no official plan.", status=500)
    return sc


def latest_run(s: Session, scenario_id: str) -> Run | None:
    return s.scalars(select(Run).where(Run.scenario_id == scenario_id).order_by(Run.created_at.desc(), Run.id)).first()


def latest_done_run(s: Session, scenario_id: str) -> Run | None:
    return s.scalars(
        select(Run).where(Run.scenario_id == scenario_id, Run.status == "done").order_by(Run.finished_at.desc())
    ).first()


def active_constraint_rows(s: Session, scenario_id: str) -> list[ConstraintRow]:
    return list(
        s.scalars(
            select(ConstraintRow)
            .where(ConstraintRow.scenario_id == scenario_id, ConstraintRow.status == "active")
            .order_by(ConstraintRow.created_at)
        )
    )


# ---------------------------------------------------------------- views


def event_summary(ev: Event) -> dict:
    venue = ev.venue
    return {
        "id": ev.id,
        "name": ev.name,
        "date": ev.day.isoformat(),
        "weekday": ev.day.strftime("%A"),
        "venue": venue.name,
        "venue_id": venue.id,
        "timezone": venue.timezone,
        "open_hour": venue.open_hour,
        "close_hour": venue.close_hour,
        "opening_hours": f"{venue.open_hour:02d}:00-{venue.close_hour:02d}:00",
        "service_rate_per_lane": venue.service_rate_per_lane,
        "wage_per_staff_hour": venue.wage_per_staff_hour,
        "day_type": ev.day_type,
        "weather": ev.weather,
        "show_start": None if ev.show_start is None else f"{ev.show_start:02d}:00",
        "gates": [{"id": g.id, "name": g.name, "lanes": g.lanes} for g in venue.gates],
        "revision": ev.revision,
    }


def gate_names(ev: Event) -> dict[str, str]:
    return {g.id: g.name for g in ev.venue.gates}


def constraint_view(s: Session, row: ConstraintRow, names: dict[str, str]) -> dict:
    parsed = C.parse_constraint({"type": row.type, **row.params})
    note = s.get(Note, row.note_id) if row.note_id else None
    return {
        "id": row.id,
        "type": row.type,
        "acts_on": C.acts_on(row.type),
        "params": row.params,
        "readback": C.describe(parsed, names),
        "interpretation": row.interpretation,
        "assumptions": row.assumptions,
        "status": row.status,
        "scenario_id": row.scenario_id,
        "note": None
        if note is None
        else {"id": note.id, "text": note.text, "author": note.author, "channel": note.channel, "created_at": iso(note.created_at)},
        "proposed_by": row.proposed_by,
        "proposed_via": row.proposed_via,
        "created_at": iso(row.created_at),
        "decided_by": row.decided_by,
        "decided_via": row.decided_via,
        "decided_at": iso(row.decided_at),
        "decision_reason": row.decision_reason,
    }


def run_view(run: Run | None) -> dict | None:
    if run is None:
        return None
    return {
        "id": run.id,
        "status": run.status,
        "stage": run.stage,
        "progress": round(run.progress, 2),
        "trigger": run.trigger,
        "error": run.error,
        "created_at": iso(run.created_at),
        "finished_at": iso(run.finished_at),
    }


def scenario_brief(s: Session, sc: Scenario) -> dict:
    run = latest_run(s, sc.id)
    return {
        "id": sc.id,
        "name": sc.name,
        "kind": sc.kind,
        "status": sc.status,
        "parent_id": sc.parent_id,
        "selected_solution_id": sc.selected_solution_id,
        "created_by": sc.created_by,
        "created_via": sc.created_via,
        "created_at": iso(sc.created_at),
        "promoted_at": iso(sc.promoted_at),
        "run_status": run.status if run else None,
    }


def list_events(s: Session, actor: Actor) -> list[dict]:
    events = s.scalars(select(Event).where(Event.workspace_id == actor.workspace_id).order_by(Event.day))
    out = []
    for ev in events:
        summary = event_summary(ev)
        scenarios = s.scalars(
            select(Scenario).where(Scenario.event_id == ev.id, Scenario.status == "active").order_by(Scenario.created_at)
        )
        summary["scenarios"] = [scenario_brief(s, sc) for sc in scenarios]
        summary["pending_constraints"] = len(
            list(s.scalars(select(ConstraintRow.id).where(ConstraintRow.event_id == ev.id, ConstraintRow.status == "pending")))
        )
        out.append(summary)
    return out


def event_detail(s: Session, actor: Actor, event_id: str) -> dict:
    ev = _event(s, actor, event_id)
    names = gate_names(ev)
    summary = event_summary(ev)
    scenarios = s.scalars(select(Scenario).where(Scenario.event_id == ev.id).order_by(Scenario.created_at))
    summary["scenarios"] = [scenario_brief(s, sc) for sc in scenarios]
    pending = s.scalars(
        select(ConstraintRow).where(ConstraintRow.event_id == ev.id, ConstraintRow.status == "pending").order_by(ConstraintRow.created_at)
    )
    summary["pending"] = [constraint_view(s, row, names) for row in pending]
    return summary


def selected_solution(run: Run | None, solution_id: str | None) -> dict | None:
    if run is None or run.result is None or solution_id is None:
        return None
    return next((sol for sol in run.result["solutions"] if sol["id"] == solution_id), None)


def compare_with_official(s: Session, sc: Scenario, run: Run | None) -> dict | None:
    if sc.kind == "official":
        return None
    official = official_scenario(s, sc.event_id)
    off_run = latest_done_run(s, official.id)
    mine = selected_solution(run, sc.selected_solution_id)
    theirs = selected_solution(off_run, official.selected_solution_id)
    if mine is None or theirs is None:
        return None
    day_mine = run.forecast["totals"]["day"]["p50"] if run and run.forecast else None
    day_theirs = off_run.forecast["totals"]["day"]["p50"] if off_run and off_run.forecast else None
    return {
        "official_scenario_id": official.id,
        "official_solution_id": theirs["id"],
        "delta_staff_hours": mine["staff_hours"] - theirs["staff_hours"],
        "delta_staff_cost": round(mine["staff_cost"] - theirs["staff_cost"], 2),
        "delta_expected_wait": round(mine["expected_wait"] - theirs["expected_wait"], 2),
        "delta_wait_p90": round(mine["wait_p90"] - theirs["wait_p90"], 2),
        "delta_day_arrivals_p50": None if day_mine is None or day_theirs is None else day_mine - day_theirs,
        "official": {k: theirs[k] for k in ("staff_hours", "staff_cost", "expected_wait", "wait_p90")},
    }


def scenario_state(s: Session, actor: Actor, scenario_id: str) -> dict:
    sc, ev = _scenario(s, actor, scenario_id)
    names = gate_names(ev)
    run = latest_run(s, sc.id)
    done = latest_done_run(s, sc.id)
    pending_targeted = s.scalars(
        select(ConstraintRow).where(ConstraintRow.scenario_id == sc.id, ConstraintRow.status == "pending")
    )
    can_undo = (
        s.scalars(select(ScenarioEdit.id).where(ScenarioEdit.scenario_id == sc.id, ScenarioEdit.undone.is_(False))).first()
        is not None
    )
    selected = selected_solution(done, sc.selected_solution_id)
    return {
        "event": event_summary(ev),
        "scenario": scenario_brief(s, sc),
        "constraints": [constraint_view(s, r, names) for r in active_constraint_rows(s, sc.id)],
        "pending": [constraint_view(s, r, names) for r in pending_targeted],
        "run": run_view(run),
        "result_run_id": done.id if done else None,
        "result_is_stale": bool(run and done and run.id != done.id),
        "forecast": done.forecast if done else None,
        "result": done.result if done else None,
        "selected_solution": selected,
        "explanation": explain_choice(done.result, sc.selected_solution_id) if selected else None,
        "comparison": compare_with_official(s, sc, done),
        "can_undo": can_undo and sc.kind == "what_if" and sc.status == "active",
        "editable": sc.kind == "what_if" and sc.status == "active",
    }


def audit_log(s: Session, actor: Actor, event_id: str, limit: int = 100) -> list[dict]:
    _event(s, actor, event_id)
    rows = s.scalars(
        select(AuditEntry).where(AuditEntry.event_id == event_id).order_by(AuditEntry.id.desc()).limit(limit)
    )
    return [
        {
            "id": r.id,
            "at": iso(r.created_at),
            "actor": r.actor,
            "channel": r.channel,
            "action": r.action,
            "target_type": r.target_type,
            "target_id": r.target_id,
            "note_id": r.note_id,
            "details": r.details,
        }
        for r in rows
    ]


# ---------------------------------------------------------------- internals for changes


def _audit(s: Session, actor: Actor, ev: Event, action: str, target_type: str, target_id: str,
           note_id: str | None = None, **details) -> None:
    s.add(
        AuditEntry(
            workspace_id=ev.workspace_id,
            event_id=ev.id,
            actor=actor.name,
            channel=actor.channel,
            action=action,
            target_type=target_type,
            target_id=target_id,
            note_id=note_id,
            details=details,
        )
    )


def _bump(ev: Event) -> None:
    ev.revision = (ev.revision or 0) + 1


def request_run(s: Session, sc: Scenario, trigger: str) -> Run:
    """Queue a forecast + optimization pass. It starts once the surrounding transaction commits."""
    run = Run(scenario_id=sc.id, trigger=trigger)
    s.add(run)
    s.flush()
    s.info.setdefault("runs_to_start", []).append(run.id)
    return run


def _parse(constraint: dict, ev: Event) -> C.Constraint:
    try:
        parsed = C.parse_constraint(constraint)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'constraint'}: {err['msg']}" for err in exc.errors()
        )
        raise ServiceError(f"Invalid constraint: {problems}") from exc
    try:
        C.check_against_event(parsed, event_summary(ev))
    except C.ConstraintError as exc:
        raise ServiceError(str(exc)) from exc
    return parsed


def _params(parsed: C.Constraint) -> dict:
    return parsed.model_dump(exclude={"type"})


def _check_combination(s: Session, ev: Event, scenario_id: str | None, extra: list[C.Constraint]) -> None:
    current = []
    if scenario_id:
        current = [C.parse_constraint({"type": r.type, **r.params}) for r in active_constraint_rows(s, scenario_id)]
    try:
        C.check_combination(current + extra, event_summary(ev))
    except C.ConstraintError as exc:
        raise ServiceError(f"This would make the plan impossible: {exc}") from exc


def _require_editable(sc: Scenario) -> None:
    if sc.status != "active":
        raise ServiceError(f"Scenario '{sc.name}' is {sc.status} and can no longer change.", status=409)
    if sc.kind != "what_if":
        raise ServiceError(
            "The official plan cannot be edited directly. Make a what-if scenario, change that, and promote it.",
            status=409,
        )


def _check_what_if_limit(s: Session, ev: Event) -> None:
    active = s.scalars(
        select(Scenario.id).where(Scenario.event_id == ev.id, Scenario.kind == "what_if", Scenario.status == "active")
    ).all()
    if len(active) >= settings.max_what_ifs_per_event:
        raise ServiceError(
            f"This event already has {len(active)} open what-if scenarios (limit {settings.max_what_ifs_per_event}). "
            "Discard some first.", status=429)


def _check_text(value: str, field: str, limit: int) -> str:
    value = (value or "").strip()
    if len(value) > limit:
        raise ServiceError(f"{field} is too long ({len(value)} characters; limit {limit}).")
    return value


def _copy_scenario(s: Session, actor: Actor, ev: Event, base: Scenario, name: str) -> Scenario:
    _check_what_if_limit(s, ev)
    sc = Scenario(
        event_id=ev.id, name=name, kind="what_if", parent_id=base.id, created_by=actor.name, created_via=actor.channel
    )
    s.add(sc)
    s.flush()
    for row in active_constraint_rows(s, base.id):
        s.add(
            ConstraintRow(
                event_id=ev.id,
                scenario_id=sc.id,
                note_id=row.note_id,
                copied_from=row.id,
                type=row.type,
                params=row.params,
                interpretation=row.interpretation,
                assumptions=row.assumptions,
                status="active",
                proposed_by=row.proposed_by,
                proposed_via=row.proposed_via,
                decided_by=row.decided_by,
                decided_via=row.decided_via,
                decided_at=row.decided_at,
            )
        )
    return sc


# ---------------------------------------------------------------- changes


def create_what_if(s: Session, actor: Actor, event_id: str, name: str, base_scenario_id: str | None = None) -> Scenario:
    ev = _event(s, actor, event_id)
    base = official_scenario(s, ev.id) if base_scenario_id is None else _scenario(s, actor, base_scenario_id)[0]
    if base.event_id != ev.id or base.status != "active":
        raise ServiceError("The base scenario must be an active scenario of the same event.")
    name = _check_text(name or "", "Name", 200) or f"What-if from {base.name}"
    sc = _copy_scenario(s, actor, ev, base, name[:200])
    _audit(s, actor, ev, "create_what_if", "scenario", sc.id, base_scenario_id=base.id, name=sc.name)
    request_run(s, sc, "what-if created")
    _bump(ev)
    return sc


def propose_constraint(
    s: Session,
    actor: Actor,
    event_id: str,
    source_text: str,
    constraint: dict,
    interpretation: str = "",
    assumptions: list[str] | None = None,
    scenario_id: str | None = None,
) -> ConstraintRow:
    ev = _event(s, actor, event_id)
    source_text = _check_text(source_text, "source_text", 2000)
    if not source_text:
        raise ServiceError("source_text must hold the planner's own words.")
    interpretation = _check_text(interpretation, "interpretation", 1000)
    assumptions = [_check_text(a, "Each assumption", 300) for a in (assumptions or [])]
    if len(assumptions) > 10:
        raise ServiceError("At most 10 assumptions per constraint.")
    pending = s.scalars(
        select(ConstraintRow.id).where(ConstraintRow.event_id == ev.id, ConstraintRow.status == "pending")
    ).all()
    if len(pending) >= settings.max_pending_per_event:
        raise ServiceError(
            f"{len(pending)} constraints are already waiting for confirmation (limit {settings.max_pending_per_event}). "
            "Ask the planner to confirm or reject them first.", status=429)
    parsed = _parse(constraint, ev)
    target = None
    if scenario_id:
        target, _ = _scenario(s, actor, scenario_id)
        if target.event_id != ev.id or target.status != "active":
            raise ServiceError("scenario_id must be an active scenario of this event.")
        if target.kind == "official":
            target = None  # confirming will create a what-if from the official plan
    base_id = target.id if target else official_scenario(s, ev.id).id
    _check_combination(s, ev, base_id, [parsed])

    note = Note(event_id=ev.id, text=source_text.strip(), author=actor.name, channel=actor.channel)
    s.add(note)
    s.flush()
    row = ConstraintRow(
        event_id=ev.id,
        scenario_id=target.id if target else None,
        note_id=note.id,
        type=parsed.type,
        params=_params(parsed),
        interpretation=(interpretation or C.describe(parsed, gate_names(ev))).strip(),
        assumptions=[a for a in (assumptions or []) if a and a.strip()],
        status="pending",
        proposed_by=actor.name,
        proposed_via=actor.channel,
    )
    s.add(row)
    s.flush()
    _audit(s, actor, ev, "propose_constraint", "constraint", row.id, note_id=note.id, type=row.type, params=row.params)
    _bump(ev)
    return row


def _short(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def confirm_constraint(s: Session, actor: Actor, constraint_id: str) -> tuple[ConstraintRow, Scenario]:
    row, ev = _constraint(s, actor, constraint_id)
    if row.status != "pending":
        raise ServiceError(f"This constraint is already {row.status}.", status=409)
    parsed = C.parse_constraint({"type": row.type, **row.params})
    target = s.get(Scenario, row.scenario_id) if row.scenario_id else None
    if target is None or target.status != "active" or target.kind != "what_if":
        official = official_scenario(s, ev.id)
        target = _copy_scenario(s, actor, ev, official, f"What-if: {_short(C.short_label(parsed, gate_names(ev)))}")
        _audit(s, actor, ev, "create_what_if", "scenario", target.id, base_scenario_id=official.id,
               name=target.name, reason=f"to hold confirmed constraint {row.id}")
    _check_combination(s, ev, target.id, [parsed])
    row.status = "active"
    row.scenario_id = target.id
    row.decided_by, row.decided_via, row.decided_at = actor.name, actor.channel, utcnow()
    s.add(ScenarioEdit(scenario_id=target.id, op="add_constraint", payload={"constraint_id": row.id}, actor=actor.name))
    _audit(s, actor, ev, "confirm_constraint", "constraint", row.id, note_id=row.note_id, scenario_id=target.id)
    request_run(s, target, f"constraint {row.id} confirmed")
    _bump(ev)
    return row, target


def reject_constraint(s: Session, actor: Actor, constraint_id: str, reason: str | None = None) -> ConstraintRow:
    row, ev = _constraint(s, actor, constraint_id)
    if row.status != "pending":
        raise ServiceError(f"This constraint is already {row.status}.", status=409)
    row.status = "rejected"
    row.decided_by, row.decided_via, row.decided_at = actor.name, actor.channel, utcnow()
    row.decision_reason = _check_text(reason or "", "Reason", 1000) or None
    _audit(s, actor, ev, "reject_constraint", "constraint", row.id, note_id=row.note_id, reason=row.decision_reason)
    _bump(ev)
    return row


def add_constraint(s: Session, actor: Actor, scenario_id: str, constraint: dict, note_text: str | None = None) -> ConstraintRow:
    """Direct edit by a planner (dashboard): applies at once to a what-if and can be undone."""
    sc, ev = _scenario(s, actor, scenario_id)
    _require_editable(sc)
    parsed = _parse(constraint, ev)
    _check_combination(s, ev, sc.id, [parsed])
    note = None
    note_text = _check_text(note_text or "", "Note", 2000)
    if note_text:
        note = Note(event_id=ev.id, text=note_text, author=actor.name, channel=actor.channel)
        s.add(note)
        s.flush()
    now = utcnow()
    row = ConstraintRow(
        event_id=ev.id,
        scenario_id=sc.id,
        note_id=note.id if note else None,
        type=parsed.type,
        params=_params(parsed),
        interpretation=C.describe(parsed, gate_names(ev)),
        assumptions=[],
        status="active",
        proposed_by=actor.name,
        proposed_via=actor.channel,
        decided_by=actor.name,
        decided_via=actor.channel,
        decided_at=now,
    )
    s.add(row)
    s.flush()
    s.add(ScenarioEdit(scenario_id=sc.id, op="add_constraint", payload={"constraint_id": row.id}, actor=actor.name))
    _audit(s, actor, ev, "add_constraint", "constraint", row.id, note_id=row.note_id, scenario_id=sc.id,
           type=row.type, params=row.params)
    request_run(s, sc, f"constraint {row.id} added")
    _bump(ev)
    return row


def remove_constraint(s: Session, actor: Actor, constraint_id: str) -> ConstraintRow:
    row, ev = _constraint(s, actor, constraint_id)
    if row.status != "active" or not row.scenario_id:
        raise ServiceError(f"Only active constraints can be removed; this one is {row.status}.", status=409)
    sc = s.get(Scenario, row.scenario_id)
    _require_editable(sc)
    row.status = "removed"
    s.add(ScenarioEdit(scenario_id=sc.id, op="remove_constraint", payload={"constraint_id": row.id}, actor=actor.name))
    _audit(s, actor, ev, "remove_constraint", "constraint", row.id, note_id=row.note_id, scenario_id=sc.id)
    request_run(s, sc, f"constraint {row.id} removed")
    _bump(ev)
    return row


def select_plan(s: Session, actor: Actor, scenario_id: str, solution_id: str) -> Scenario:
    sc, ev = _scenario(s, actor, scenario_id)
    _require_editable(sc)
    run = latest_done_run(s, sc.id)
    if selected_solution(run, solution_id) is None:
        raise ServiceError(f"Plan '{solution_id}' is not in this scenario's latest result.")
    previous = sc.selected_solution_id
    sc.selected_solution_id = solution_id
    sc.selected_by_planner = True
    s.add(ScenarioEdit(scenario_id=sc.id, op="select_plan",
                       payload={"from": previous, "to": solution_id, "run_id": run.id}, actor=actor.name))
    _audit(s, actor, ev, "select_plan", "scenario", sc.id, solution_id=solution_id, previous=previous, run_id=run.id)
    _bump(ev)
    return sc


def undo_last(s: Session, actor: Actor, scenario_id: str) -> dict:
    sc, ev = _scenario(s, actor, scenario_id)
    _require_editable(sc)
    edit = s.scalars(
        select(ScenarioEdit)
        .where(ScenarioEdit.scenario_id == sc.id, ScenarioEdit.undone.is_(False))
        .order_by(ScenarioEdit.id.desc())
    ).first()
    if edit is None:
        raise ServiceError("Nothing to undo in this scenario.", status=409)
    rerun = False
    if edit.op == "add_constraint":
        row = s.get(ConstraintRow, edit.payload["constraint_id"])
        row.status = "removed"
        rerun = True
    elif edit.op == "remove_constraint":
        row = s.get(ConstraintRow, edit.payload["constraint_id"])
        _check_combination(s, ev, sc.id, [C.parse_constraint({"type": row.type, **row.params})])
        row.status = "active"
        rerun = True
    elif edit.op == "select_plan":
        sc.selected_solution_id = edit.payload.get("from")
    edit.undone = True
    _audit(s, actor, ev, "undo", "scenario", sc.id, undone_op=edit.op, payload=edit.payload)
    if rerun:
        request_run(s, sc, f"undo {edit.op}")
    _bump(ev)
    return {"undone": edit.op, "payload": edit.payload}


def discard_scenario(s: Session, actor: Actor, scenario_id: str) -> Scenario:
    sc, ev = _scenario(s, actor, scenario_id)
    _require_editable(sc)
    sc.status = "discarded"
    _audit(s, actor, ev, "discard_scenario", "scenario", sc.id)
    _bump(ev)
    return sc


def promote_scenario(s: Session, actor: Actor, scenario_id: str, solution_id: str) -> Scenario:
    sc, ev = _scenario(s, actor, scenario_id)
    _require_editable(sc)
    run, done = latest_run(s, sc.id), latest_done_run(s, sc.id)
    if done is None or run is None or run.id != done.id:
        raise ServiceError("This scenario is still being optimized; promote it when the run has finished.", status=409)
    chosen = selected_solution(done, solution_id)
    if chosen is None:
        raise ServiceError(f"Plan '{solution_id}' is not in this scenario's latest result.")
    old = official_scenario(s, ev.id)
    old.status = "archived"
    sc.kind = "official"
    sc.selected_solution_id = solution_id
    sc.promoted_at, sc.promoted_by = utcnow(), actor.name
    rows = active_constraint_rows(s, sc.id)
    _audit(
        s, actor, ev, "promote_scenario", "scenario", sc.id,
        previous_official=old.id,
        solution_id=solution_id,
        run_id=done.id,
        staff_hours=chosen["staff_hours"],
        staff_cost=chosen["staff_cost"],
        expected_wait=chosen["expected_wait"],
        constraints=[{"id": r.id, "type": r.type, "note_id": r.note_id} for r in rows],
    )
    _bump(ev)
    return sc
