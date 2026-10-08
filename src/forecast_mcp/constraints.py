"""The one shared definition of a planner constraint.

Both front doors (Claude through MCP, and the dashboard) send constraints in this
shape and both go through the same validation. The `type` field decides where a
constraint acts:

- gate_closed, gate_capacity_limit, staff_limit -> bound the optimizer
- competing_event, schedule_shift -> change the forecast's inputs
- note_only -> stored with the scenario, changes nothing
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

# Whole hours in 24-hour local venue time, e.g. "17:00".
HourOfDay = Annotated[
    str,
    Field(
        pattern=r"^([01]\d|2[0-3]):00$",
        description="Local time at the venue, on the hour, 24-hour clock, e.g. '17:00'.",
        examples=["17:00"],
    ),
]


def hour_of(value: str) -> int:
    return int(value[:2])


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Window(_Base):
    start: HourOfDay = Field(description="First hour affected (inclusive), local venue time.")
    end: HourOfDay = Field(description="Hour the effect stops (exclusive), local venue time. Must be after start.")

    @model_validator(mode="after")
    def _end_after_start(self):
        if hour_of(self.end) <= hour_of(self.start):
            raise ValueError(f"end ({self.end}) must be later than start ({self.start})")
        return self


class GateClosed(_Window):
    """A gate cannot admit anyone during a time window (roadworks, maintenance, security).
    Its staff is fixed at zero and its arrivals are assumed to go to the other open gates."""

    type: Literal["gate_closed"]
    gate_id: str = Field(description="Gate id as returned by list_events, e.g. 'north'.")


class GateCapacityLimit(_Window):
    """Fewer lanes can open at a gate than normal (narrowed entrance, broken scanners)."""

    type: Literal["gate_capacity_limit"]
    gate_id: str = Field(description="Gate id as returned by list_events.")
    max_lanes: int = Field(ge=1, le=50, description="Most staffed lanes that can open at this gate during the window.")


class StaffLimit(_Window):
    """At most this many gate staff can be on duty at once, across all gates."""

    type: Literal["staff_limit"]
    max_staff: int = Field(ge=1, le=200, description="Maximum gate staff on duty at the same time, all gates together.")


class CompetingEvent(_Window):
    """Another event nearby draws visitors away during a time window (concert, match, festival).
    Arrivals are expected to drop from one hour before start until end."""

    type: Literal["competing_event"]
    name: str = Field(min_length=1, max_length=120, description="Name of the competing event, as the planner said it.")
    impact: Literal["minor", "major"] = Field(
        description="'minor' for a smaller event (roughly 10-15% fewer arrivals in history), "
        "'major' for a large one (roughly 25-30% fewer). If the planner gave no size, choose and say so in assumptions."
    )


class ScheduleShift(_Base):
    """The evening show moves to a new start time. Arrivals peak before the show, so the peak moves too."""

    type: Literal["schedule_shift"]
    show_start: HourOfDay = Field(description="New start time of the evening show, local venue time.")


class NoteOnly(_Base):
    """Planner knowledge that matters but maps onto none of the other types. Stored with the scenario, changes nothing."""

    type: Literal["note_only"]
    summary: str = Field(min_length=1, max_length=500, description="One-sentence summary of the note.")


Constraint = Annotated[
    Union[GateClosed, GateCapacityLimit, StaffLimit, CompetingEvent, ScheduleShift, NoteOnly],
    Field(discriminator="type"),
]

constraint_adapter: TypeAdapter[Constraint] = TypeAdapter(Constraint)

OPTIMIZER_TYPES = {"gate_closed", "gate_capacity_limit", "staff_limit"}
FORECAST_TYPES = {"competing_event", "schedule_shift"}


def parse_constraint(data: dict) -> Constraint:
    return constraint_adapter.validate_python(data)


def acts_on(constraint_type: str) -> str:
    if constraint_type in OPTIMIZER_TYPES:
        return "optimizer"
    if constraint_type in FORECAST_TYPES:
        return "forecast"
    return "none"


class ConstraintError(ValueError):
    """A constraint is well-formed but does not fit this event (unknown gate, outside opening hours, ...)."""


def check_against_event(constraint: Constraint, event: dict) -> None:
    """Checks that need the event: gate ids, opening hours, lane counts.

    `event` is the plain dict from services.event_summary (gates, open_hour, close_hour).
    """
    gates = {g["id"]: g for g in event["gates"]}
    open_hour, close_hour = event["open_hour"], event["close_hour"]

    gate_id = getattr(constraint, "gate_id", None)
    if gate_id is not None and gate_id not in gates:
        raise ConstraintError(f"Unknown gate_id '{gate_id}'. Valid gate ids: {', '.join(sorted(gates))}.")

    if isinstance(constraint, _Window):
        start, end = hour_of(constraint.start), hour_of(constraint.end)
        if constraint.type == "competing_event":
            # A competing event may run past closing; only its overlap with opening hours matters.
            if end <= open_hour or start >= close_hour:
                raise ConstraintError(
                    f"The window {constraint.start}-{constraint.end} does not overlap opening hours "
                    f"{open_hour:02d}:00-{close_hour:02d}:00."
                )
        elif start < open_hour or end > close_hour:
            raise ConstraintError(
                f"The window {constraint.start}-{constraint.end} is outside opening hours "
                f"{open_hour:02d}:00-{close_hour:02d}:00."
            )

    if isinstance(constraint, GateCapacityLimit):
        lanes = gates[constraint.gate_id]["lanes"]
        if constraint.max_lanes > lanes:
            raise ConstraintError(
                f"{gates[constraint.gate_id]['name']} has only {lanes} lanes; max_lanes {constraint.max_lanes} "
                "would not limit anything."
            )

    if isinstance(constraint, ScheduleShift):
        show = hour_of(constraint.show_start)
        if not open_hour <= show < close_hour:
            raise ConstraintError(
                f"Show start {constraint.show_start} is outside opening hours {open_hour:02d}:00-{close_hour:02d}:00."
            )


def check_combination(constraints: list[Constraint], event: dict) -> None:
    """Checks across all constraints of a scenario, so the optimizer always has a feasible problem."""
    open_hour, close_hour = event["open_hour"], event["close_hour"]
    gate_ids = [g["id"] for g in event["gates"]]
    for hour in range(open_hour, close_hour):
        closed = {
            c.gate_id
            for c in constraints
            if isinstance(c, GateClosed) and hour_of(c.start) <= hour < hour_of(c.end)
        }
        open_gates = [g for g in gate_ids if g not in closed]
        if not open_gates:
            raise ConstraintError(f"At {hour:02d}:00 every gate would be closed.")
        limits = [c.max_staff for c in constraints if isinstance(c, StaffLimit) and hour_of(c.start) <= hour < hour_of(c.end)]
        if limits and min(limits) < len(open_gates):
            raise ConstraintError(
                f"At {hour:02d}:00 the staff limit ({min(limits)}) is below one person per open gate ({len(open_gates)})."
            )
    shifts = [c for c in constraints if isinstance(c, ScheduleShift)]
    if len(shifts) > 1:
        raise ConstraintError("A scenario can hold only one schedule_shift; remove the old one first.")


def describe(constraint: Constraint, gate_names: dict[str, str] | None = None) -> str:
    """Plain-language read-back of a constraint."""
    names = gate_names or {}
    gate = names.get(getattr(constraint, "gate_id", ""), getattr(constraint, "gate_id", ""))
    match constraint:
        case GateClosed():
            return f"{gate} closed {constraint.start}-{constraint.end}; its visitors use the other gates."
        case GateCapacityLimit():
            return f"{gate} limited to {constraint.max_lanes} lanes {constraint.start}-{constraint.end}."
        case StaffLimit():
            return f"At most {constraint.max_staff} gate staff on duty at once {constraint.start}-{constraint.end}."
        case CompetingEvent():
            return f"Competing event '{constraint.name}' ({constraint.impact} impact) {constraint.start}-{constraint.end}."
        case ScheduleShift():
            return f"Evening show starts at {constraint.show_start}."
        case NoteOnly():
            return f"Note: {constraint.summary}"
    raise TypeError(type(constraint))


def short_label(constraint: Constraint, gate_names: dict[str, str] | None = None) -> str:
    """A few words, for scenario names."""
    names = gate_names or {}
    gate = names.get(getattr(constraint, "gate_id", ""), getattr(constraint, "gate_id", ""))
    gate = gate.split(" (")[0]
    match constraint:
        case GateClosed():
            return f"{gate} closed {constraint.start}-{constraint.end}"
        case GateCapacityLimit():
            return f"{gate} max {constraint.max_lanes} lanes {constraint.start}-{constraint.end}"
        case StaffLimit():
            return f"Max {constraint.max_staff} staff {constraint.start}-{constraint.end}"
        case CompetingEvent():
            return f"{constraint.name} ({constraint.impact})"
        case ScheduleShift():
            return f"Show at {constraint.show_start}"
        case NoteOnly():
            return constraint.summary[:40]
    raise TypeError(type(constraint))
