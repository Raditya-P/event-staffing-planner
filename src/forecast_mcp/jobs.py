"""Background runs: forecast, then optimize, whenever a scenario changes.

The `runs` table is the work queue. A worker thread in each server instance
claims queued runs (on Postgres with FOR UPDATE SKIP LOCKED, so several
instances never take the same run), executes them one at a time, and writes
status and progress back, so any view (the dashboard, the chat panel, Claude)
can poll it. A request never waits for the optimizer.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from datetime import timedelta

from sqlalchemy import event as sa_event
from sqlalchemy import select

from . import constraints as C
from .config import settings
from .db import Event, Observation, Run, Scenario, SessionLocal, Venue, session_scope, utcnow
from .engines.contract import build_forecast
from .engines.forecast import DayInputs, get_forecaster
from .engines.optimizer import explain_choice, optimize
from .services import active_constraint_rows, event_summary, gate_names

log = logging.getLogger(__name__)

# Tests set this to run jobs inline, right after the commit that queued them.
RUN_INLINE = False
WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"
STALE_AFTER = timedelta(minutes=10)  # a "running" run older than this belongs to a worker that died

_wake = threading.Event()
_stop = threading.Event()
_thread: threading.Thread | None = None
_lock = threading.Lock()


@sa_event.listens_for(SessionLocal, "after_commit")
def _start_queued_runs(session) -> None:
    run_ids = session.info.pop("runs_to_start", [])
    if not run_ids:
        return
    if RUN_INLINE:
        for run_id in run_ids:
            if claim(run_id):
                execute_run(run_id)
    else:
        _wake.set()


def claim(run_id: str | None = None) -> str | None:
    """Mark one queued run as ours. Returns its id, or None if there is nothing to take."""
    with session_scope() as s:
        q = select(Run).where(Run.status == "queued")
        if run_id:
            q = q.where(Run.id == run_id)
        run = s.scalars(q.order_by(Run.created_at).limit(1).with_for_update(skip_locked=True)).first()
        if run is None:
            return None
        run.status, run.stage, run.started_at, run.claimed_by = "running", "starting", utcnow(), WORKER_ID
        return run.id


def _worker() -> None:
    while not _stop.is_set():
        try:
            run_id = claim()
        except Exception:
            log.exception("could not claim a run")
            run_id = None
        if run_id:
            execute_run(run_id)
            continue
        _wake.wait(timeout=2.0)
        _wake.clear()


def start_worker() -> None:
    global _thread
    with _lock:
        if RUN_INLINE or (_thread is not None and _thread.is_alive()):
            return
        _stop.clear()
        _thread = threading.Thread(target=_worker, name="run-worker", daemon=True)
        _thread.start()


def _history_loader(venue_id: str):
    def load() -> list[dict]:
        with session_scope() as s:
            rows = s.execute(
                select(
                    Observation.gate_id, Observation.day, Observation.hour, Observation.arrivals,
                    Observation.day_type, Observation.weather, Observation.show_start, Observation.competing,
                ).where(Observation.venue_id == venue_id)
            ).all()
        keys = ("gate_id", "day", "hour", "arrivals", "day_type", "weather", "show_start", "competing")
        return [dict(zip(keys, r)) for r in rows]

    return load


def forecaster_for(venue_id: str, history_version: int, gate_ids: list[str], fingerprint: str | None = None):
    # Venues with the same history fingerprint (every demo copy) share one model.
    key = (fingerprint or venue_id, history_version, settings.forecast_members, settings.forecast_boost_iters,
           settings.seed)
    return get_forecaster(key, _history_loader(venue_id), gate_ids, settings.forecast_members,
                          settings.forecast_boost_iters, settings.seed)


def day_inputs(ev: Event, parsed: list, ids: list[str], notes: dict[str, str | None], rows) -> tuple[DayInputs, list[dict]]:
    """Event facts plus forecast-type constraints -> model inputs, with every assumption written down."""
    venue = ev.venue
    hours = range(venue.open_hour, venue.close_hour)
    show_start = ev.show_start
    competing: dict[int, int] = {}
    assumptions = [
        {"text": f"Day type '{ev.day_type}' ({ev.day.strftime('%A')}).", "source": "event"},
        {"text": f"Weather '{ev.weather}', as entered for the event.", "source": "event"},
        {"text": "Evening show at " + (f"{ev.show_start:02d}:00." if ev.show_start is not None else "none."), "source": "event"},
    ]
    for cid, c, row in zip(ids, parsed, rows):
        if isinstance(c, C.ScheduleShift):
            show_start = C.hour_of(c.show_start)
        elif isinstance(c, C.CompetingEvent):
            level = 1 if c.impact == "minor" else 2
            for h in hours:
                if C.hour_of(c.start) - 1 <= h < C.hour_of(c.end):
                    competing[h] = max(competing.get(h, 0), level)
        else:
            continue
        text = C.describe(c, gate_names(ev))
        if notes.get(cid):
            text += f' From the planner\'s note: "{notes[cid]}"'
        assumptions.append({"text": text, "source": "constraint", "constraint_id": cid, "note_id": row.note_id})
        for extra in row.assumptions or []:
            assumptions.append({"text": f"Interpretation assumption: {extra}", "source": "constraint",
                                "constraint_id": cid, "note_id": row.note_id})
    return DayInputs(ev.day_type, ev.weather, show_start, competing), assumptions


def _set(run_id: str, **fields) -> None:
    with session_scope() as s:
        run = s.get(Run, run_id)
        for k, v in fields.items():
            setattr(run, k, v)


def execute_run(run_id: str) -> None:
    try:
        _execute(run_id)
    except Exception as exc:  # recorded, never raised into the worker
        log.exception("run %s failed", run_id)
        _set(run_id, status="failed", stage="failed", error=f"{type(exc).__name__}: {exc}", finished_at=utcnow())
        _bump_event_of(run_id)


def _bump_event_of(run_id: str) -> None:
    with session_scope() as s:
        run = s.get(Run, run_id)
        sc = s.get(Scenario, run.scenario_id)
        ev = s.get(Event, sc.event_id)
        ev.revision += 1


def _execute(run_id: str) -> None:
    from .db import Note

    with session_scope() as s:
        run = s.get(Run, run_id)
        if run is None or run.status != "running" or run.claimed_by != WORKER_ID:
            return
        newer = s.scalars(
            select(Run.id).where(Run.scenario_id == run.scenario_id, Run.created_at > run.created_at)
        ).first()
        if newer:  # a later change already queued a fresher run
            run.status, run.stage, run.finished_at = "superseded", "superseded", utcnow()
            return
        run.stage = "forecast"
        sc = s.get(Scenario, run.scenario_id)
        ev = s.get(Event, sc.event_id)
        event = event_summary(ev)
        rows = active_constraint_rows(s, sc.id)
        ids = [r.id for r in rows]
        parsed = [C.parse_constraint({"type": r.type, **r.params}) for r in rows]
        notes = {r.id: (s.get(Note, r.note_id).text if r.note_id else None) for r in rows}
        inputs, assumptions = day_inputs(ev, parsed, ids, notes, rows)
        venue_key = (ev.venue.id, ev.venue.history_version, [g.id for g in ev.venue.gates],
                     ev.venue.history_fingerprint)
        scenario_id, scenario_name = sc.id, sc.name

    # Outside the transaction: the first call trains the model, which takes a few seconds.
    model = forecaster_for(*venue_key)

    contract, mu, sd = build_forecast(
        model, event, inputs, assumptions, scenario_id=scenario_id, forecast_id=f"fc_{run_id[4:]}", seed=settings.seed
    )
    _set(run_id, forecast=contract, stage="optimize", progress=0.0)

    last = [0.0]

    def progress(fraction: float) -> None:
        now = time.monotonic()
        if now - last[0] > 0.5:
            last[0] = now
            _set(run_id, progress=fraction)

    result = optimize(
        event, parsed, ids, mu, sd,
        pop_size=settings.optimizer_pop_size,
        generations=settings.optimizer_generations,
        samples=settings.optimizer_samples,
        seed=settings.seed,
        progress=progress,
    )
    result["explanation"] = explain_choice(result, result["recommended_id"])

    with session_scope() as s:
        run = s.get(Run, run_id)
        run.result = result
        run.status, run.stage, run.progress, run.finished_at = "done", "done", 1.0, utcnow()
        sc = s.get(Scenario, scenario_id)
        if sc.status == "active" and sc.kind == "what_if" or sc.selected_solution_id is None:
            # Plan ids refer to this run's trade-off; start from the balanced plan again.
            sc.selected_solution_id = result["recommended_id"]
            sc.selected_by_planner = False
        ev = s.get(Event, sc.event_id)
        ev.revision += 1
    log.info("run %s for %s done (%s)", run_id, scenario_name, result["recommended_id"])


def warm_up() -> None:
    """Train the forecast model for each distinct venue history in the background at start-up."""
    def train_all():
        with session_scope() as s:
            keys = {}
            for v in s.scalars(select(Venue)):
                keys.setdefault(v.history_fingerprint or v.id, (v.id, v.history_version, [g.id for g in v.gates],
                                                                v.history_fingerprint))
        for key in list(keys.values())[:4]:
            try:
                forecaster_for(*key)
            except Exception:
                log.exception("warm-up failed for venue %s", key[0])

    if RUN_INLINE:
        train_all()
    else:
        threading.Thread(target=train_all, name="warm-up", daemon=True).start()


def resume_unfinished() -> None:
    """Re-queue runs whose worker died (a restart, a crash) so another worker picks them up."""
    cutoff = utcnow() - STALE_AFTER
    with session_scope() as s:
        stuck = s.scalars(select(Run).where(Run.status == "running"))
        for run in stuck:
            started = run.started_at
            if started is not None and started.tzinfo is None:
                started = started.replace(tzinfo=cutoff.tzinfo)
            if run.claimed_by == WORKER_ID or started is None or started < cutoff:
                run.status, run.stage, run.claimed_by = "queued", "queued", None
    if RUN_INLINE:
        while (run_id := claim()) is not None:
            execute_run(run_id)
    else:
        _wake.set()


def wait_for(scenario_id: str, timeout: float = 30.0) -> str | None:
    """Block until the scenario's latest run finishes (or timeout). Returns its status."""
    deadline = time.monotonic() + timeout
    while True:
        with session_scope() as s:
            run = s.scalars(select(Run).where(Run.scenario_id == scenario_id).order_by(Run.created_at.desc())).first()
            status = run.status if run else None
        if status in (None, "done", "failed") or time.monotonic() > deadline:
            return status
        time.sleep(0.3)


def shutdown() -> None:
    _stop.set()
    _wake.set()
