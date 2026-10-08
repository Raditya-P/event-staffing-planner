import pytest
from pydantic import ValidationError

from forecast_mcp.constraints import ConstraintError, check_against_event, check_combination, parse_constraint

EVENT = {
    "open_hour": 9,
    "close_hour": 23,
    "gates": [{"id": "main", "name": "Main", "lanes": 12}, {"id": "north", "name": "North", "lanes": 8}],
}


def test_valid_types_parse():
    for c in [
        {"type": "gate_closed", "gate_id": "north", "start": "17:00", "end": "20:00"},
        {"type": "gate_capacity_limit", "gate_id": "main", "max_lanes": 4, "start": "10:00", "end": "12:00"},
        {"type": "staff_limit", "max_staff": 10, "start": "09:00", "end": "23:00"},
        {"type": "competing_event", "name": "Concert", "impact": "major", "start": "19:00", "end": "23:00"},
        {"type": "schedule_shift", "show_start": "20:00"},
        {"type": "note_only", "summary": "VIP group at 11"},
    ]:
        check_against_event(parse_constraint(c), EVENT)


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "gate_closed", "gate_id": "north", "start": "17:30", "end": "20:00"},  # not on the hour
        {"type": "gate_closed", "gate_id": "north", "start": "20:00", "end": "17:00"},  # end before start
        {"type": "staff_limit", "max_staff": 0, "start": "09:00", "end": "10:00"},
        {"type": "teleport", "gate_id": "north"},
        {"type": "gate_closed", "gate_id": "north", "start": "17:00", "end": "20:00", "extra": 1},
    ],
)
def test_malformed_rejected(bad):
    with pytest.raises(ValidationError):
        parse_constraint(bad)


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "gate_closed", "gate_id": "west", "start": "17:00", "end": "20:00"},
        {"type": "gate_closed", "gate_id": "north", "start": "07:00", "end": "10:00"},
        {"type": "gate_capacity_limit", "gate_id": "north", "max_lanes": 9, "start": "10:00", "end": "11:00"},
    ],
)
def test_event_checks(bad):
    with pytest.raises(ConstraintError):
        check_against_event(parse_constraint(bad), EVENT)


def test_cannot_close_every_gate():
    cons = [parse_constraint({"type": "gate_closed", "gate_id": g, "start": "17:00", "end": "18:00"}) for g in ("main", "north")]
    with pytest.raises(ConstraintError, match="every gate"):
        check_combination(cons, EVENT)
