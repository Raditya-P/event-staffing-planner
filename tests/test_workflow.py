"""The rules from the design, checked through the shared service layer both front doors use."""

import pytest

from forecast_mcp import services as S
from forecast_mcp.db import session_scope

CHAT = S.Actor("planner@local", "demo", "chat")
PANEL = S.Actor("planner@local", "demo", "chat_panel")
DASH = S.Actor("planner@local", "demo", "dashboard")
CLOSE_NORTH = {"type": "gate_closed", "gate_id": "north", "start": "17:00", "end": "20:00"}


def state(scenario_id, actor=DASH):
    with session_scope() as s:
        return S.scenario_state(s, actor, scenario_id)


def official_id(event_id):
    with session_scope() as s:
        return S.official_scenario(s, event_id).id


def test_seeded_official_plan_is_optimized(event_id):
    st = state(official_id(event_id))
    assert st["run"]["status"] == "done"
    assert st["forecast"]["contract_version"] == "1.0"
    assert st["selected_solution"]["id"] == st["result"]["recommended_id"]
    assert st["editable"] is False


def test_chat_constraint_waits_for_confirmation(event_id):
    off = official_id(event_id)
    with session_scope() as s:
        row = S.propose_constraint(s, CHAT, event_id, "Roadworks close the north road 5-8pm", CLOSE_NORTH,
                                   "North Gate closed 17-20", ["5-8pm read as 17:00-20:00"])
        cid = row.id
    assert state(off)["constraints"] == []  # nothing applied yet
    with session_scope() as s:
        pending = S.event_detail(s, DASH, event_id)["pending"]
    assert [p["id"] for p in pending] == [cid]
    assert pending[0]["note"]["text"] == "Roadworks close the north road 5-8pm"

    with session_scope() as s:
        row, sc = S.confirm_constraint(s, PANEL, cid)
        what_if = sc.id
    st = state(what_if)
    assert st["scenario"]["kind"] == "what_if"
    assert [c["id"] for c in st["constraints"]] == [cid]
    assert st["run"]["status"] == "done"
    assert st["comparison"] is not None
    assert state(off)["constraints"] == []  # the official plan is untouched

    with pytest.raises(S.ServiceError):  # cannot confirm twice
        with session_scope() as s:
            S.confirm_constraint(s, PANEL, cid)

    with session_scope() as s:
        log = S.audit_log(s, DASH, event_id)
    confirm = next(e for e in log if e["action"] == "confirm_constraint" and e["target_id"] == cid)
    assert confirm["channel"] == "chat_panel" and confirm["note_id"] == pending[0]["note"]["id"]


def test_rejected_constraint_never_applies(event_id):
    with session_scope() as s:
        cid = S.propose_constraint(s, CHAT, event_id, "maybe fewer staff", {"type": "staff_limit", "max_staff": 9,
                                   "start": "09:00", "end": "12:00"}, "At most 9 staff 9-12").id
    with session_scope() as s:
        row = S.reject_constraint(s, PANEL, cid, "We have enough staff")
        assert row.status == "rejected"
    with pytest.raises(S.ServiceError):
        with session_scope() as s:
            S.confirm_constraint(s, PANEL, cid)


def test_official_plan_cannot_be_edited_directly(event_id):
    with pytest.raises(S.ServiceError, match="official plan cannot be edited"):
        with session_scope() as s:
            S.add_constraint(s, DASH, official_id(event_id), CLOSE_NORTH)


def test_direct_edit_undo_and_plan_choice(event_id):
    with session_scope() as s:
        sid = S.create_what_if(s, DASH, event_id, "Fewer lanes test").id
    with session_scope() as s:
        cid = S.add_constraint(s, DASH, sid, {"type": "gate_capacity_limit", "gate_id": "main", "max_lanes": 5,
                                              "start": "10:00", "end": "12:00"}, "Two scanners broken").id
    st = state(sid)
    assert [c["id"] for c in st["constraints"]] == [cid] and st["can_undo"]

    other = st["result"]["solutions"][0]["id"]
    with session_scope() as s:
        S.select_plan(s, DASH, sid, other)
    assert state(sid)["scenario"]["selected_solution_id"] == other

    with session_scope() as s:
        assert S.undo_last(s, DASH, sid)["undone"] == "select_plan"
    with session_scope() as s:
        assert S.undo_last(s, DASH, sid)["undone"] == "add_constraint"
    st = state(sid)
    assert st["constraints"] == [] and st["run"]["status"] == "done"


def test_impossible_combination_is_refused(event_id):
    with session_scope() as s:
        sid = S.create_what_if(s, DASH, event_id, "All closed").id
    for gate in ("main", "north"):
        with session_scope() as s:
            S.add_constraint(s, DASH, sid, {"type": "gate_closed", "gate_id": gate, "start": "18:00", "end": "19:00"})
    with pytest.raises(S.ServiceError, match="every gate"):
        with session_scope() as s:
            S.add_constraint(s, DASH, sid, {"type": "gate_closed", "gate_id": "east", "start": "18:00", "end": "19:00"})


def test_promote_replaces_official_and_is_logged(demo_data):
    event_id = demo_data[1]
    old = official_id(event_id)
    with session_scope() as s:
        sid = S.create_what_if(s, DASH, event_id, "Show at 20:00").id
    with session_scope() as s:
        S.add_constraint(s, DASH, sid, {"type": "schedule_shift", "show_start": "20:00"})
    st = state(sid)
    assert any("20:00" in a["text"] for a in st["forecast"]["assumptions"])
    chosen = st["scenario"]["selected_solution_id"]
    with session_scope() as s:
        S.promote_scenario(s, PANEL, sid, chosen)
    assert official_id(event_id) == sid
    with session_scope() as s:
        assert S.scenario_brief(s, s.get(S.Scenario, old))["status"] == "archived"
        entry = next(e for e in S.audit_log(s, DASH, event_id) if e["action"] == "promote_scenario")
    assert entry["details"]["previous_official"] == old and entry["details"]["solution_id"] == chosen
    assert state(sid)["editable"] is False


def test_other_workspace_sees_nothing(event_id):
    stranger = S.Actor("someone@else", "other-org", "dashboard")
    with session_scope() as s:
        assert S.list_events(s, stranger) == []
        with pytest.raises(S.NotFound):
            S.event_detail(s, stranger, event_id)
