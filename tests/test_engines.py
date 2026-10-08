from datetime import date

import numpy as np
import pytest

from forecast_mcp.constraints import parse_constraint
from forecast_mcp.engines.contract import build_forecast
from forecast_mcp.engines.forecast import DayInputs, EnsembleForecaster, mixture_quantiles
from forecast_mcp.engines.optimizer import optimize, reroute
from forecast_mcp.engines.synthetic import DEMO_VENUE, generate_history

GATES = [g.id for g in DEMO_VENUE.gates]
EVENT = {
    "id": "evt_test", "name": "Test", "date": "2026-10-31", "timezone": DEMO_VENUE.timezone,
    "open_hour": DEMO_VENUE.open_hour, "close_hour": DEMO_VENUE.close_hour,
    "service_rate_per_lane": DEMO_VENUE.service_rate_per_lane, "wage_per_staff_hour": DEMO_VENUE.wage_per_staff_hour,
    "gates": [{"id": g.id, "name": g.name, "lanes": g.lanes} for g in DEMO_VENUE.gates],
}


@pytest.fixture(scope="module")
def model():
    rows = generate_history(DEMO_VENUE, date(2026, 10, 6))
    return EnsembleForecaster(members=4, boost_iters=40).fit(rows, GATES)


def test_history_has_thin_gate():
    rows = generate_history(DEMO_VENUE, date(2026, 10, 6))
    days = {g: len({r["day"] for r in rows if r["gate_id"] == g}) for g in GATES}
    assert days["east"] == 6 and days["main"] == 120


def test_mixture_quantiles_of_one_normal():
    q = mixture_quantiles(np.array([[100.0]]), np.array([[10.0]]), (0.1, 0.5, 0.9))
    assert q[:, 0] == pytest.approx([100 - 12.8155, 100, 100 + 12.8155], abs=0.3)


def test_forecast_contract_shape_and_decomposition(model):
    contract, mu, sd = build_forecast(model, EVENT, DayInputs("weekend", "cloudy", 19), [], scenario_id="s", forecast_id="f")
    assert contract["contract_version"] == "1.0"
    assert contract["interval"] == {"lower": "p10", "upper": "p90", "level": 0.8}
    assert [s["gate_id"] for s in contract["series"]] == GATES
    for s in contract["series"]:
        assert len(s["points"]) == 14
        for p in s["points"]:
            assert p["p10"] <= p["p50"] <= p["p90"]
            # Law of total variance: the two parts add up to the total.
            assert p["sd_volatility"] ** 2 + p["sd_missing_history"] ** 2 == pytest.approx(p["sd_total"] ** 2, rel=0.02, abs=2)
    east = next(s for s in contract["series"] if s["gate_id"] == "east")
    assert east["history"]["thin"] is True
    assert any(n["kind"] == "thin_history" for n in contract["data_notes"])
    t = contract["totals"]["day"]
    assert t["p10"] < t["p50"] < t["p90"]
    assert 0 < contract["backtest"]["coverage_p10_p90"] <= 1


def test_competing_event_lowers_evening_arrivals(model):
    hours = DEMO_VENUE.hours
    base, _ = model.member_distributions(GATES, hours, DayInputs("weekend", "cloudy", 19))
    comp, _ = model.member_distributions(GATES, hours, DayInputs("weekend", "cloudy", 19, {h: 2 for h in range(17, 21)}))
    evening = [hours.index(h) for h in (17, 18, 19)]
    assert comp.mean(axis=0)[:, evening].sum() < 0.95 * base.mean(axis=0)[:, evening].sum()


def test_unseen_show_time_is_flagged(model):
    contract, _, _ = build_forecast(model, EVENT, DayInputs("weekend", "cloudy", 21), [], scenario_id="s", forecast_id="f")
    assert any(n["kind"] == "unseen_condition" for n in contract["data_notes"])


def test_reroute_moves_closed_gate_arrivals():
    arrivals = np.ones((5, 3, 4)) * np.array([100.0, 50.0, 50.0])[None, :, None]
    closed = np.zeros((3, 4), dtype=bool)
    closed[1, 2] = True
    out = reroute(arrivals, closed)
    assert out[:, 1, 2].sum() == 0
    assert out.sum() == pytest.approx(arrivals.sum())


def test_optimizer_respects_constraints(model):
    _, mu, sd = build_forecast(model, EVENT, DayInputs("weekend", "cloudy", 19), [], scenario_id="s", forecast_id="f")
    cons = [
        parse_constraint({"type": "gate_closed", "gate_id": "north", "start": "17:00", "end": "20:00"}),
        parse_constraint({"type": "staff_limit", "max_staff": 14, "start": "09:00", "end": "12:00"}),
        parse_constraint({"type": "gate_capacity_limit", "gate_id": "main", "max_lanes": 6, "start": "10:00", "end": "11:00"}),
    ]
    result = optimize(EVENT, cons, ["a", "b", "c"], mu, sd, pop_size=30, generations=25, samples=15)
    sols = result["solutions"]
    assert sols and result["recommended_id"] in {s["id"] for s in sols}
    costs = [s["staff_cost"] for s in sols]
    waits = [s["expected_wait"] for s in sols]
    assert costs == sorted(costs)
    assert all(b < a for a, b in zip(waits, waits[1:])), "each dearer plan must cut the wait"
    for s in sols:
        sched = np.array([s["schedule"][g] for g in GATES])
        assert (sched[1, 8:11] == 0).all()  # north closed 17-20
        assert (sched[:, 0:3].sum(axis=0) <= 14).all()  # staff limit 9-12
        assert sched[0, 1] <= 6  # main at most 6 lanes at 10:00
        assert (sched[[0, 2], :] >= 1).all()  # open gates keep a lane
    assert len(result["constraints_applied"]) == 3
