"""Optimizer stand-in: NSGA-II over gate staffing, staff cost vs. guest waiting.

Decision: how many lanes to staff at each gate in each hour (integers).
Objectives (both minimized):
  1. staff cost  = staff-hours x hourly wage
  2. expected waiting time per guest, averaged over sampled forecast days
     (so a plan is scored on how it holds up when arrivals differ from the median)
Planner constraints become variable bounds (gate closed, fewer lanes) or
inequality constraints (staff on duty across gates).

The waiting model is a deliberately simple, standard approximation:
  - a fluid queue carries any backlog from hour to hour when arrivals exceed capacity;
  - within an hour, random congestion uses Sakasegawa's (1977) M/M/c approximation
    Lq ~ rho^sqrt(2(c+1)) / (1 - rho), applied hour by hour (pointwise stationary,
    cf. Green, Kolesar & Whitt 2007).
Final plans are re-scored on a fresh, larger set of sampled days, so the reported
numbers are not tuned to the samples the optimizer saw.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
from pymoo.algorithms.moo.nsga2 import NSGA2
from pymoo.core.callback import Callback
from pymoo.core.problem import Problem
from pymoo.operators.crossover.sbx import SBX
from pymoo.operators.mutation.pm import PM
from pymoo.operators.repair.rounding import RoundingRepair
from pymoo.optimize import minimize

from ..constraints import GateCapacityLimit, GateClosed, StaffLimit, describe, hour_of

ALGORITHM = {"name": "NSGA-II", "library": "pymoo 0.6.2"}
OBJECTIVES = [
    {"key": "staff_cost", "label": "Staff cost", "unit": "EUR", "direction": "min"},
    {"key": "expected_wait", "label": "Expected wait", "unit": "minutes per guest", "direction": "min"},
]
WAIT_THRESHOLD_MIN = 10.0
MIN_WAIT_GAIN_MIN = 0.05


@dataclass
class StaffingProblemSpec:
    gate_ids: list[str]
    hours: list[int]
    lanes: np.ndarray  # (G,)
    service_rate: float  # guests per staffed lane per hour
    wage: float
    xl: np.ndarray  # (G, H)
    xu: np.ndarray  # (G, H)
    closed: np.ndarray  # (G, H) bool
    staff_limits: list[tuple[int, int]]  # (hour index, max staff on duty)
    effects: list[dict]


def build_spec(event: dict, constraints: list, constraint_ids: list[str]) -> StaffingProblemSpec:
    gate_ids = [g["id"] for g in event["gates"]]
    names = {g["id"]: g["name"] for g in event["gates"]}
    hours = list(range(event["open_hour"], event["close_hour"]))
    lanes = np.array([g["lanes"] for g in event["gates"]])
    G, H = len(gate_ids), len(hours)
    # Assumption: every open gate keeps at least one lane staffed while the park is open.
    xl = np.ones((G, H), dtype=int)
    xu = np.repeat(lanes[:, None], H, axis=1).astype(int)
    closed = np.zeros((G, H), dtype=bool)
    limits: dict[int, int] = {}
    effects = []

    for cid, c in zip(constraint_ids, constraints):
        if isinstance(c, (GateClosed, GateCapacityLimit, StaffLimit)):
            window = [i for i, h in enumerate(hours) if hour_of(c.start) <= h < hour_of(c.end)]
        else:
            continue
        if isinstance(c, GateClosed):
            gi = gate_ids.index(c.gate_id)
            closed[gi, window] = True
            effects.append({"constraint_id": cid, "type": c.type, "effect": describe(c, names) + " Staff fixed at 0."})
        elif isinstance(c, GateCapacityLimit):
            gi = gate_ids.index(c.gate_id)
            xu[gi, window] = np.minimum(xu[gi, window], c.max_lanes)
            effects.append({"constraint_id": cid, "type": c.type, "effect": describe(c, names) + " Upper bound on lanes."})
        elif isinstance(c, StaffLimit):
            for i in window:
                limits[i] = min(limits.get(i, c.max_staff), c.max_staff)
            effects.append({"constraint_id": cid, "type": c.type, "effect": describe(c, names) + " Sum across gates capped."})

    xl[closed] = 0
    xu[closed] = 0
    xl = np.minimum(xl, xu)
    return StaffingProblemSpec(
        gate_ids=gate_ids,
        hours=hours,
        lanes=lanes,
        service_rate=float(event["service_rate_per_lane"]),
        wage=float(event["wage_per_staff_hour"]),
        xl=xl,
        xu=xu,
        closed=closed,
        staff_limits=sorted(limits.items()),
        effects=effects,
    )


def reroute(arrivals: np.ndarray, closed: np.ndarray) -> np.ndarray:
    """Send arrivals for a closed gate-hour to the open gates, in proportion to their usual volume."""
    out = arrivals.copy()  # (S, G, H)
    if not closed.any():
        return out
    typical = arrivals.mean(axis=0)  # (G, H)
    for h in np.flatnonzero(closed.any(axis=0)):
        open_g = ~closed[:, h]
        weights = typical[open_g, h]
        weights = weights / weights.sum() if weights.sum() > 0 else np.full(open_g.sum(), 1 / open_g.sum())
        moved = out[:, closed[:, h], h].sum(axis=1)  # (S,)
        out[:, closed[:, h], h] = 0
        out[:, open_g, h] += moved[:, None] * weights[None, :]
    return out


def simulate_queues(staff: np.ndarray, arrivals: np.ndarray, rate: float) -> tuple[np.ndarray, np.ndarray]:
    """staff: (P, G, H) lanes; arrivals: (S, G, H).
    Returns (guest-hours spent waiting in each hour, minutes waited by a guest arriving in each hour),
    both shaped (P, S, G, H)."""
    staff = staff[:, None, :, :].astype(float)
    arrivals = arrivals[None, :, :, :]
    P, S, G, H = staff.shape[0], arrivals.shape[1], staff.shape[2], staff.shape[3]
    cap = staff * rate
    queue = np.zeros((P, S, G))
    waited = np.zeros((P, S, G, H))
    arrival_wait = np.zeros((P, S, G, H))
    for h in range(H):
        lam = arrivals[..., h]
        c = staff[..., h]
        k = cap[..., h]
        q_end = np.maximum(queue + lam - k, 0.0)
        backlog = 0.5 * (queue + q_end)  # average backlog during the hour
        rho = np.minimum(np.where(k > 0, lam / np.maximum(k, 1e-9), 0.0), 0.97)
        congestion = np.where(c > 0, rho ** np.sqrt(2.0 * (c + 1.0)) / (1.0 - rho), 0.0)  # mean queue (Lq)
        waited[..., h] = backlog + congestion
        # A guest arriving now waits for the backlog ahead to clear, plus the random congestion delay (Lq / lambda).
        arrival_wait[..., h] = 60.0 * np.where(
            k > 0, backlog / np.maximum(k, 1e-9) + congestion / np.maximum(lam, 1e-9), 0.0
        )
        queue = q_end
    last_k = np.broadcast_to(cap[..., -1], queue.shape)
    # Anyone still queuing at closing time is admitted by the last hour's lanes.
    waited[..., -1] += np.where(last_k > 0, queue**2 / (2 * np.maximum(last_k, 1e-9)), queue)
    return waited, arrival_wait


def score(staff: np.ndarray, arrivals: np.ndarray, spec: StaffingProblemSpec) -> dict[str, np.ndarray]:
    """Objectives and robustness numbers for a batch of plans."""
    waited, arrival_wait = simulate_queues(staff, arrivals, spec.service_rate)  # (P, S, G, H)
    guests = arrivals.sum(axis=(1, 2))  # (S,)
    # Little's law: average wait per guest = total guest-hours waiting / guests.
    avg_wait = 60.0 * waited.sum(axis=(2, 3)) / np.maximum(guests[None, :], 1.0)  # (P, S) minutes
    a = arrivals[None, :, :, :]
    hourly = (arrival_wait * a).sum(axis=2) / np.maximum(a.sum(axis=2), 1.0)  # (P, S, H) arrival-weighted
    per_gate = 60.0 * waited.sum(axis=3) / np.maximum(arrivals.sum(axis=2)[None, :, :], 1.0)  # (P, S, G)
    staff_hours = staff.sum(axis=(1, 2))
    hourly_mean = hourly.mean(axis=1)  # (P, H)
    return {
        "staff_hours": staff_hours,
        "staff_cost": staff_hours * spec.wage,
        "expected_wait": avg_wait.mean(axis=1),
        "wait_p90": np.percentile(avg_wait, 90, axis=1),
        "prob_wait_over_threshold": (avg_wait > WAIT_THRESHOLD_MIN).mean(axis=1),
        "peak_hour_wait": hourly_mean.max(axis=1),
        "peak_hour_index": hourly_mean.argmax(axis=1),
        "hourly_wait": hourly_mean,
        "gate_wait": per_gate.mean(axis=1),  # (P, G)
    }


class _StaffingProblem(Problem):
    def __init__(self, spec: StaffingProblemSpec, arrivals: np.ndarray, free: np.ndarray, max_wait: float | None):
        self.spec = spec
        self.max_wait = max_wait
        self.arrivals = arrivals
        self.free = free
        G, H = spec.xl.shape
        self.hours_of_free = np.tile(np.arange(H), G)[free]
        super().__init__(
            n_var=int(free.sum()),
            n_obj=2,
            n_ieq_constr=len(spec.staff_limits) + (1 if max_wait is not None else 0),
            xl=spec.xl.ravel()[free],
            xu=spec.xu.ravel()[free],
            vtype=int,
        )

    def to_schedule(self, x: np.ndarray) -> np.ndarray:
        full = np.repeat(self.spec.xl.ravel()[None, :], x.shape[0], axis=0)
        full[:, self.free] = np.round(x).astype(int)
        return full.reshape(x.shape[0], *self.spec.xl.shape)

    def _evaluate(self, x, out, *args, **kwargs):
        staff = self.to_schedule(np.atleast_2d(x))
        s = score(staff, self.arrivals, self.spec)
        out["F"] = np.column_stack([s["staff_cost"], s["expected_wait"]])
        g = [staff[:, :, h].sum(axis=1) - cap for h, cap in self.spec.staff_limits]
        if self.max_wait is not None:
            g.append(s["expected_wait"] - self.max_wait)
        if g:
            out["G"] = np.column_stack(g)


def _seed_schedules(spec: StaffingProblemSpec, mean_arrivals: np.ndarray) -> list[np.ndarray]:
    """Square-root safety staffing (Halfin & Whitt 1981) at several quality levels:
    lanes = load + beta * sqrt(load). Gives the search good starting points across the trade-off."""
    load = mean_arrivals / spec.service_rate
    seeds = []
    for beta in (-0.5, 0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0):
        lanes = np.ceil(load + beta * np.sqrt(load))
        seeds.append(np.clip(lanes, spec.xl, spec.xu).astype(int))
    seeds.append(spec.xl.copy())
    seeds.append(spec.xu.copy())
    return seeds


def _repair_staff_limits(schedule: np.ndarray, spec: StaffingProblemSpec) -> np.ndarray:
    s = schedule.copy()
    for h, cap in spec.staff_limits:
        while s[:, h].sum() > cap:
            reducible = np.flatnonzero(s[:, h] > spec.xl[:, h])
            if len(reducible) == 0:
                break
            g = reducible[np.argmax(s[reducible, h])]
            s[g, h] -= 1
    return s


class _Progress(Callback):
    def __init__(self, total: int, report: Callable[[float], None] | None):
        super().__init__()
        self.total = total
        self.report = report

    def notify(self, algorithm):
        if self.report and algorithm.n_gen % 10 == 0:
            self.report(min(algorithm.n_gen / self.total, 1.0))


def _knee_index(cost: np.ndarray, wait: np.ndarray) -> int:
    """Point furthest below the straight line joining the cheapest and the shortest-wait plan."""
    if len(cost) < 3:
        return int(np.argmin((cost - cost.min()) / max(np.ptp(cost), 1e-9) + (wait - wait.min()) / max(np.ptp(wait), 1e-9)))
    c = (cost - cost.min()) / max(np.ptp(cost), 1e-9)
    w = (wait - wait.min()) / max(np.ptp(wait), 1e-9)
    # line from (0, 1) [cheapest] to (1, 0) [shortest wait]: distance = (1 - c - w) / sqrt(2)
    return int(np.argmax(1 - c - w))


def _non_dominated(f: np.ndarray) -> np.ndarray:
    keep = np.ones(len(f), dtype=bool)
    for i in range(len(f)):
        if keep[i]:
            dominated = np.all(f[i] <= f, axis=1) & np.any(f[i] < f, axis=1)
            keep &= ~dominated
    return keep


def optimize(
    event: dict,
    constraints: list,
    constraint_ids: list[str],
    member_mu: np.ndarray,
    member_sd: np.ndarray,
    *,
    pop_size: int = 80,
    generations: int = 120,
    samples: int = 40,
    seed: int = 7,
    max_solutions: int = 30,
    max_expected_wait: float = 30.0,
    progress: Callable[[float], None] | None = None,
) -> dict:
    from .forecast import sample_arrivals

    spec = build_spec(event, constraints, constraint_ids)
    rng = np.random.default_rng(seed)
    search_days = reroute(sample_arrivals(member_mu, member_sd, samples, rng), spec.closed)
    check_days = reroute(sample_arrivals(member_mu, member_sd, 300, np.random.default_rng(seed + 1000)), spec.closed)

    free = (spec.xu > spec.xl).ravel()
    G, H = spec.xl.shape

    def search(max_wait: float | None) -> np.ndarray:
        problem = _StaffingProblem(spec, search_days, free, max_wait)
        if problem.n_var == 0:
            return spec.xl[None, :, :].copy()
        seeds = [_repair_staff_limits(s, spec) for s in _seed_schedules(spec, search_days.mean(axis=0))]
        init = np.array([s.ravel()[free] for s in seeds])
        rand = rng.integers(problem.xl, problem.xu + 1, size=(max(pop_size - len(init), 0), problem.n_var))
        initial = np.vstack([init, rand])[:pop_size]
        algorithm = NSGA2(
            pop_size=pop_size,
            sampling=initial.astype(float),
            crossover=SBX(prob=0.9, eta=15, vtype=float, repair=RoundingRepair()),
            mutation=PM(eta=20, vtype=float, repair=RoundingRepair()),
            eliminate_duplicates=True,
        )
        res = minimize(problem, algorithm, ("n_gen", generations), seed=seed, verbose=False,
                       callback=_Progress(generations, progress))
        feasible = res.X is not None and (res.CV is None or np.all(np.atleast_1d(res.CV) <= 0))
        if not feasible:
            return np.empty((0, G, H), dtype=int)
        return problem.to_schedule(np.atleast_2d(res.X))

    # Plans whose expected wait exceeds the service ceiling are not worth showing a planner.
    schedules = search(max_expected_wait)
    service_cap_met = len(schedules) > 0
    if not service_cap_met:
        schedules = search(None)
    if len(schedules) == 0:
        raise RuntimeError("The optimizer found no plan that satisfies all constraints.")

    schedules = np.unique(schedules, axis=0)
    s = score(schedules, check_days, spec)
    keep = _non_dominated(np.column_stack([s["staff_cost"], s["expected_wait"]]))
    order = np.flatnonzero(keep)[np.argsort(s["staff_cost"][keep])]
    # Drop plans that cost more but cut the expected wait by less than 3 seconds: practically the same.
    meaningful = [order[0]]
    for idx in order[1:]:
        if s["expected_wait"][meaningful[-1]] - s["expected_wait"][idx] >= MIN_WAIT_GAIN_MIN:
            meaningful.append(idx)
    order = np.array(meaningful)
    if len(order) > max_solutions:  # thin evenly along the front
        order = order[np.unique(np.linspace(0, len(order) - 1, max_solutions).round().astype(int))]

    cost, wait = s["staff_cost"][order], s["expected_wait"][order]
    knee = _knee_index(cost, wait)
    solutions = []
    for rank, idx in enumerate(order):
        label = None
        if rank == 0:
            label = "cheapest"
        if rank == len(order) - 1:
            label = "shortest_wait"
        if rank == knee:
            label = "balanced"
        solutions.append(
            {
                "id": f"s{rank}",
                "label": label,
                "staff_hours": int(s["staff_hours"][idx]),
                "staff_cost": round(float(s["staff_cost"][idx]), 2),
                "expected_wait": round(float(s["expected_wait"][idx]), 2),
                "wait_p90": round(float(s["wait_p90"][idx]), 2),
                "prob_wait_over_threshold": round(float(s["prob_wait_over_threshold"][idx]), 3),
                "peak_hour": f"{spec.hours[int(s['peak_hour_index'][idx])]:02d}:00",
                "peak_hour_wait": round(float(s["peak_hour_wait"][idx]), 2),
                "gate_wait": {g: round(float(v), 2) for g, v in zip(spec.gate_ids, s["gate_wait"][idx])},
                "hourly_wait": [round(float(v), 2) for v in s["hourly_wait"][idx]],
                "schedule": {g: schedules[idx, gi].tolist() for gi, g in enumerate(spec.gate_ids)},
            }
        )

    return {
        "algorithm": {**ALGORITHM, "pop_size": pop_size, "generations": generations, "search_samples": samples,
                      "check_samples": 300, "seed": seed},
        "objectives": OBJECTIVES,
        "wait_threshold_min": WAIT_THRESHOLD_MIN,
        "max_expected_wait": max_expected_wait,
        "service_cap_met": service_cap_met,
        "hours": [f"{h:02d}:00" for h in spec.hours],
        "gates": spec.gate_ids,
        "bounds": {
            "min_lanes": {g: spec.xl[gi].tolist() for gi, g in enumerate(spec.gate_ids)},
            "max_lanes": {g: spec.xu[gi].tolist() for gi, g in enumerate(spec.gate_ids)},
            "staff_limits": [{"hour": f"{spec.hours[h]:02d}:00", "max_staff": cap} for h, cap in spec.staff_limits],
        },
        "constraints_applied": spec.effects,
        "model_assumptions": [
            f"Each staffed lane admits about {spec.service_rate:.0f} guests per hour.",
            "Every open gate keeps at least one lane staffed while the park is open.",
            "Visitors of a closed gate use the other open gates, in proportion to their usual volume.",
            "Waiting combines a backlog carried between hours with random congestion within each hour "
            "(Sakasegawa's M/M/c approximation).",
            "Plans are scored on 300 sampled days drawn from the forecast, not only its median.",
            f"Only plans with an expected wait of at most {max_expected_wait:.0f} minutes are searched "
            "(if none exists under the constraints, the best possible plans are shown and flagged).",
        ],
        "solutions": solutions,
        "recommended_id": solutions[knee]["id"],
    }


def explain_choice(result: dict, solution_id: str) -> str:
    sols = {s["id"]: s for s in result["solutions"]}
    chosen = sols[solution_id]
    cheapest = result["solutions"][0]
    fastest = result["solutions"][-1]
    thr = result["wait_threshold_min"]
    parts = [
        f"Plan {chosen['id']} ({chosen['label'] or 'custom'}): {chosen['staff_hours']} staff-hours "
        f"(EUR {chosen['staff_cost']:,.0f}), expected wait {chosen['expected_wait']:.1f} min per guest; "
        f"on 9 of 10 simulated days the average wait stays under {chosen['wait_p90']:.1f} min; "
        f"busiest hour {chosen['peak_hour']} at {chosen['peak_hour_wait']:.1f} min; "
        f"chance the day's average wait exceeds {thr:.0f} min: {chosen['prob_wait_over_threshold']:.0%}."
    ]
    if chosen["id"] != cheapest["id"]:
        extra = chosen["staff_hours"] - cheapest["staff_hours"]
        parts.append(
            f"Versus the cheapest plan: +{extra} staff-hours (EUR {chosen['staff_cost'] - cheapest['staff_cost']:,.0f}) "
            f"buys {cheapest['expected_wait'] - chosen['expected_wait']:.1f} min less expected wait."
        )
    if chosen["id"] != fastest["id"]:
        parts.append(
            f"The shortest-wait plan would cost EUR {fastest['staff_cost'] - chosen['staff_cost']:,.0f} more for "
            f"{chosen['expected_wait'] - fastest['expected_wait']:.1f} min less wait."
        )
    return " ".join(parts)

