"""Synthetic arrival history for a fictional theme park.

Stand-in for real gate-scan data. The generator controls the two kinds of
uncertainty on purpose, so we can check that the forecast separates them:

- volatility: each gate has its own day-to-day noise (North sits by a bus
  terminal, so buses arrive in bunches and it is the most volatile);
- missing history: East Gate opened last week and has only 6 days of data,
  and rainy days are rare, so the model has seen little of either.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

DAY_TYPES = ("weekday", "weekend", "holiday")
WEATHER = ("sunny", "cloudy", "rain")

DAY_TYPE_MULT = {"weekday": 0.6, "weekend": 1.0, "holiday": 1.25}
WEATHER_MULT = {"sunny": 1.0, "cloudy": 0.9, "rain": 0.65}
COMPETING_MULT = {0: 1.0, 1: 0.87, 2: 0.72}  # none, minor, major


@dataclass(frozen=True)
class GateSpec:
    id: str
    name: str
    lanes: int
    share: float  # share of park arrivals using this gate
    volatility: float  # sd of the gate's own multiplicative day-hour noise (log scale)
    history_days: int


@dataclass(frozen=True)
class VenueSpec:
    name: str
    timezone: str
    open_hour: int
    close_hour: int  # exclusive: last arrival slot is close_hour - 1
    base_daily_arrivals: float
    service_rate_per_lane: float  # guests one staffed lane admits per hour
    wage_per_staff_hour: float
    gates: tuple[GateSpec, ...]

    @property
    def hours(self) -> list[int]:
        return list(range(self.open_hour, self.close_hour))


DEMO_VENUE = VenueSpec(
    name="Parkland Theme Park",
    timezone="Europe/Amsterdam",
    open_hour=9,
    close_hour=23,
    base_daily_arrivals=18000,
    service_rate_per_lane=150,
    wage_per_staff_hour=22.0,
    gates=(
        GateSpec("main", "Main Gate", lanes=12, share=0.50, volatility=0.10, history_days=120),
        GateSpec("north", "North Gate (bus terminal)", lanes=8, share=0.30, volatility=0.28, history_days=120),
        GateSpec("east", "East Gate (new car park)", lanes=6, share=0.20, volatility=0.12, history_days=6),
    ),
)


def hourly_profile(hours: list[int], show_start: int | None) -> np.ndarray:
    """Share of the day's arrivals in each hour: a morning opening peak, a flat
    afternoon, and (if there is an evening show) a peak about 1.5 h before it."""
    h = np.asarray(hours, dtype=float) + 0.5
    morning = np.exp(-0.5 * ((h - 10.5) / 1.2) ** 2)
    afternoon = np.ones_like(h) * 0.12
    profile = 0.55 * morning / morning.sum() + afternoon / afternoon.sum() * 0.15
    if show_start is not None:
        evening = np.exp(-0.5 * ((h - (show_start - 1.0)) / 1.0) ** 2)
        evening[h > show_start + 1] *= 0.15  # few arrive after the show starts
        profile = profile + 0.30 * evening / evening.sum()
    else:
        profile = profile / profile.sum()
    return profile / profile.sum()


def competing_levels(hours: list[int], events: list[tuple[int, int, int]]) -> np.ndarray:
    """Per-hour competing-event level (0 none, 1 minor, 2 major).
    Each event is (start_hour, end_hour, level); it bites from one hour before start."""
    levels = np.zeros(len(hours), dtype=int)
    for start, end, level in events:
        for i, hour in enumerate(hours):
            if start - 1 <= hour < end:
                levels[i] = max(levels[i], level)
    return levels


def expected_arrivals(
    venue: VenueSpec,
    gate: GateSpec,
    day_type: str,
    weather: str,
    show_start: int | None,
    competing: np.ndarray,
) -> np.ndarray:
    """Noise-free expected arrivals per hour for one gate on one day."""
    daily = venue.base_daily_arrivals * DAY_TYPE_MULT[day_type] * WEATHER_MULT[weather]
    profile = hourly_profile(venue.hours, show_start)
    mult = np.array([COMPETING_MULT[int(level)] for level in competing])
    return daily * gate.share * profile * mult


def _day_type(d: date, holidays: set[date]) -> str:
    if d in holidays:
        return "holiday"
    return "weekend" if d.weekday() >= 5 else "weekday"


def generate_history(venue: VenueSpec, last_day: date, seed: int = 7) -> list[dict]:
    """One row per gate, day and opening hour, ending on `last_day`."""
    rng = np.random.default_rng(seed)
    max_days = max(g.history_days for g in venue.gates)
    days = [last_day - timedelta(days=offset) for offset in range(max_days - 1, -1, -1)]
    holidays = {d for d in days if (d.month, d.day) in {(8, 15), (10, 3), (6, 24), (7, 21)}}

    rows: list[dict] = []
    for d in days:
        day_type = _day_type(d, holidays)
        weather = str(rng.choice(WEATHER, p=[0.55, 0.38, 0.07]))
        if day_type == "weekday":
            show_start = None if rng.random() < 0.5 else 19
        else:
            show_start = int(rng.choice([18, 19, 20], p=[0.1, 0.7, 0.2]))
        events: list[tuple[int, int, int]] = []
        if rng.random() < 0.10:
            start = int(rng.integers(15, 20))
            events.append((start, min(start + 3, venue.close_hour), int(rng.choice([1, 2]))))
        competing = competing_levels(venue.hours, events)
        day_shock = rng.normal(0, 0.08)  # shared by all gates: the whole park is busier or quieter

        for gate in venue.gates:
            if (last_day - d).days >= gate.history_days:
                continue
            mean = expected_arrivals(venue, gate, day_type, weather, show_start, competing)
            noise = rng.normal(0, gate.volatility, size=len(mean))
            lam = mean * np.exp(day_shock + noise - 0.5 * gate.volatility**2)
            arrivals = rng.poisson(np.maximum(lam, 0))
            for hour, value, level in zip(venue.hours, arrivals, competing):
                rows.append(
                    {
                        "gate_id": gate.id,
                        "day": d,
                        "hour": hour,
                        "arrivals": int(value),
                        "day_type": day_type,
                        "weather": weather,
                        "show_start": show_start,
                        "competing": int(level),
                    }
                )
    return rows
