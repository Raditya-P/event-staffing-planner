"""Forecast stand-in: a bootstrapped ensemble of quantile models.

Each ensemble member is trained on a different resample of history *days* and
predicts the 10th, 50th and 90th percentile of arrivals per gate and hour.
Treating member i's prediction as a normal distribution N(mu_i, sd_i), the
spread of the combined forecast splits exactly (law of total variance) into:

    total variance = mean_i(sd_i^2)  +  var_i(mu_i)
                     "volatility"       "missing history"

- volatility: scatter each member sees in the data itself, which more data
  would not remove;
- missing history: disagreement between members, which is large where the
  resamples differ a lot, i.e. where there were few days to learn from.

This is the standard ensemble decomposition (Lakshminarayanan et al. 2017;
Depeweg et al. 2018). How much lands in each part depends on the model, so the
split is a model-based estimate, not a property of the world.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date

import numpy as np
from scipy.special import ndtr
from sklearn.ensemble import HistGradientBoostingRegressor
from threadpoolctl import threadpool_limits

from .synthetic import DAY_TYPES, WEATHER

QUANTILES = (0.1, 0.5, 0.9)
Z90 = 1.2815515655446004  # standard normal 90th percentile
THIN_HISTORY_DAYS = 30
MODEL_NAME = "bootstrap-quantile-ensemble"
MODEL_VERSION = "0.1.0"


@dataclass(frozen=True)
class DayInputs:
    """What the model is told about the day being forecast."""

    day_type: str
    weather: str
    show_start: int | None
    competing: dict[int, int] = field(default_factory=dict)  # hour -> level (1 minor, 2 major)


def _features(gate_codes, hours, day_type, weather, show_start, competing) -> np.ndarray:
    hours = np.asarray(hours, dtype=float)
    n = len(hours)
    to_show = np.full(n, np.nan) if show_start is None else hours - float(show_start)
    cols = [
        hours,
        np.asarray(gate_codes, dtype=float),
        np.full(n, DAY_TYPES.index(day_type), dtype=float) if isinstance(day_type, str) else np.asarray(day_type, float),
        np.full(n, WEATHER.index(weather), dtype=float) if isinstance(weather, str) else np.asarray(weather, float),
        to_show,
        np.asarray(competing, dtype=float),
    ]
    return np.column_stack(cols)


CATEGORICAL = [False, True, True, True, False, False]


def _rows_to_xy(rows: list[dict], gate_index: dict[str, int]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    hours = np.array([r["hour"] for r in rows], dtype=float)
    gates = np.array([gate_index[r["gate_id"]] for r in rows], dtype=float)
    day_types = np.array([DAY_TYPES.index(r["day_type"]) for r in rows], dtype=float)
    weather = np.array([WEATHER.index(r["weather"]) for r in rows], dtype=float)
    to_show = np.array([np.nan if r["show_start"] is None else r["hour"] - r["show_start"] for r in rows], dtype=float)
    competing = np.array([r["competing"] for r in rows], dtype=float)
    x = np.column_stack([hours, gates, day_types, weather, to_show, competing])
    y = np.array([r["arrivals"] for r in rows], dtype=float)
    days = np.array([r["day"].toordinal() for r in rows])
    return x, y, days


def mixture_quantiles(mu: np.ndarray, sd: np.ndarray, qs: tuple[float, ...], grid: int = 400) -> np.ndarray:
    """Quantiles of an equal-weight mixture of normals. mu, sd: (members, n) -> (len(qs), n)."""
    lo = (mu - 4.5 * sd).min(axis=0)
    hi = (mu + 4.5 * sd).max(axis=0)
    steps = np.linspace(0.0, 1.0, grid)[:, None]
    xs = lo[None, :] + steps * (hi - lo)[None, :]  # (grid, n)
    cdf = ndtr((xs[None, :, :] - mu[:, None, :]) / sd[:, None, :]).mean(axis=0)  # (grid, n)
    out = np.empty((len(qs), mu.shape[1]))
    for k, q in enumerate(qs):
        idx = np.clip((cdf < q).sum(axis=0), 1, grid - 1)
        cols = np.arange(mu.shape[1])
        c0, c1 = cdf[idx - 1, cols], cdf[idx, cols]
        x0, x1 = xs[idx - 1, cols], xs[idx, cols]
        w = np.where(c1 > c0, (q - c0) / np.maximum(c1 - c0, 1e-12), 0.5)
        out[k] = x0 + np.clip(w, 0, 1) * (x1 - x0)
    return out


class EnsembleForecaster:
    def __init__(self, members: int = 10, boost_iters: int = 80, seed: int = 7):
        self.members = members
        self.boost_iters = boost_iters
        self.seed = seed
        self.models: list[list[HistGradientBoostingRegressor]] = []
        self.gate_index: dict[str, int] = {}
        self.history_info: dict[str, dict] = {}
        self.seen: dict[str, object] = {}
        self.backtest: dict | None = None
        self.data_cutoff: date | None = None

    # ---- training -------------------------------------------------------
    def _fit_members(self, x: np.ndarray, y: np.ndarray, days: np.ndarray, seed: int):
        """Train each member on a bootstrap resample of whole days. Returns the members
        and, per member, the set of days it never saw (used for the out-of-bag backtest)."""
        rng = np.random.default_rng(seed)
        unique_days = np.unique(days)
        rows_by_day = {d: np.flatnonzero(days == d) for d in unique_days}
        members, unseen = [], []
        # Small data: one thread per model is faster than OpenMP's thread start-up.
        with threadpool_limits(1):
            for _ in range(self.members):
                picked = rng.choice(unique_days, size=len(unique_days), replace=True)
                idx = np.concatenate([rows_by_day[d] for d in picked])
                member = []
                for q in QUANTILES:
                    model = HistGradientBoostingRegressor(
                        loss="quantile",
                        quantile=q,
                        max_iter=self.boost_iters,
                        learning_rate=0.15,
                        max_leaf_nodes=15,
                        min_samples_leaf=8,
                        categorical_features=CATEGORICAL,
                        random_state=int(rng.integers(1_000_000)),
                    )
                    model.fit(x[idx], y[idx])
                    member.append(model)
                members.append(member)
                unseen.append(set(unique_days) - set(picked))
        return members, unseen

    def fit(self, rows: list[dict], gate_ids: list[str]) -> "EnsembleForecaster":
        self.gate_index = {g: i for i, g in enumerate(gate_ids)}
        x, y, days = _rows_to_xy(rows, self.gate_index)
        self.data_cutoff = max(r["day"] for r in rows)

        for gate in gate_ids:
            gate_days = sorted({r["day"] for r in rows if r["gate_id"] == gate})
            self.history_info[gate] = {
                "days": len(gate_days),
                "first_day": gate_days[0].isoformat() if gate_days else None,
                "last_day": gate_days[-1].isoformat() if gate_days else None,
                "thin": len(gate_days) < THIN_HISTORY_DAYS,
            }
        day_rows = {r["day"]: r for r in rows}
        self.seen = {
            "weather_days": {w: sum(1 for r in day_rows.values() if r["weather"] == w) for w in WEATHER},
            "show_starts": sorted({r["show_start"] for r in day_rows.values() if r["show_start"] is not None}),
            "competing_days": {
                lvl: len({r["day"] for r in rows if r["competing"] == lvl}) for lvl in (1, 2)
            },
        }

        self.models, unseen = self._fit_members(x, y, days, self.seed)
        self.backtest = self._out_of_bag_backtest(x, y, days, unseen)
        return self

    def _out_of_bag_backtest(self, x, y, days, unseen) -> dict:
        """Score each history hour using only the members that never saw its day
        (out-of-bag validation, Breiman 1996), so no extra training is needed."""
        with threadpool_limits(1):
            preds = np.array([[m.predict(x) for m in member] for member in self.models])  # (M, 3, n)
        preds = np.sort(preds, axis=1)
        mask = np.array([[d in u for d in days] for u in unseen])  # (M, n): member did not see this row's day
        usable = mask.sum(axis=0) >= 2
        q10 = np.empty(len(y)); q50 = np.empty(len(y)); q90 = np.empty(len(y))
        for k in np.flatnonzero(usable):
            ms = mask[:, k]
            mu = preds[ms, 1, k][:, None]
            sd = np.maximum((preds[ms, 2, k] - preds[ms, 0, k]) / (2 * Z90), 1.0)[:, None]
            q10[k], q50[k], q90[k] = np.maximum(mixture_quantiles(mu, sd, QUANTILES, grid=200)[:, 0], 0)
        idx = np.flatnonzero(usable)
        y_t, lo, mid, hi = y[idx], q10[idx], q50[idx], q90[idx]
        gates_t = x[idx, 1].astype(int)
        per_gate = {}
        for gate, gi in self.gate_index.items():
            m = gates_t == gi
            if m.sum() == 0:
                continue
            per_gate[gate] = {
                "coverage_p10_p90": round(float(((y_t[m] >= lo[m]) & (y_t[m] <= hi[m])).mean()), 3),
                "wape": round(float(np.abs(y_t[m] - mid[m]).sum() / max(y_t[m].sum(), 1)), 3),
                "hours_scored": int(m.sum()),
            }
        return {
            "method": "Out-of-bag: each history hour is predicted only by ensemble members that never saw its day.",
            "hours_scored": int(len(idx)),
            "coverage_p10_p90": round(float(((y_t >= lo) & (y_t <= hi)).mean()), 3),
            "target_coverage": 0.8,
            "wape": round(float(np.abs(y_t - mid).sum() / max(y_t.sum(), 1)), 3),
            "per_gate": per_gate,
        }

    # ---- prediction -----------------------------------------------------
    def _member_dists(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        with threadpool_limits(1):
            preds = np.array([[model.predict(x) for model in member] for member in self.models])  # (M, 3, n)
        preds = np.sort(preds, axis=1)  # guard against crossing quantiles
        mu = preds[:, 1, :]
        sd = np.maximum((preds[:, 2, :] - preds[:, 0, :]) / (2 * Z90), 1.0)
        return mu, sd

    def member_distributions(self, gate_ids: list[str], hours: list[int], inputs: DayInputs):
        """(M, G, H) arrays of member means and standard deviations."""
        g_codes = np.repeat([self.gate_index[g] for g in gate_ids], len(hours))
        h_all = np.tile(hours, len(gate_ids))
        competing = [inputs.competing.get(int(h), 0) for h in h_all]
        x = _features(g_codes, h_all, inputs.day_type, inputs.weather, inputs.show_start, competing)
        mu, sd = self._member_dists(x)
        shape = (len(self.models), len(gate_ids), len(hours))
        return mu.reshape(shape), sd.reshape(shape)


def sample_arrivals(mu: np.ndarray, sd: np.ndarray, n: int, rng: np.random.Generator) -> np.ndarray:
    """Draw n plausible days: pick a member per day (shared by all gates and hours,
    so model uncertainty moves gates together), then add independent noise."""
    members = rng.integers(0, mu.shape[0], size=n)
    draws = rng.normal(mu[members], sd[members])
    return np.maximum(draws, 0.0)


_cache_lock = threading.Lock()
_cache: "OrderedDict[tuple, EnsembleForecaster]" = OrderedDict()
CACHE_SIZE = 6  # trained models kept in memory (each venue with distinct history needs one)


def get_forecaster(key: tuple, rows_loader, gate_ids: list[str], members: int, boost_iters: int, seed: int):
    """Train once per distinct history and reuse; training takes a few seconds."""
    with _cache_lock:
        model = _cache.get(key)
        if model is None:
            model = EnsembleForecaster(members=members, boost_iters=boost_iters, seed=seed).fit(rows_loader(), gate_ids)
            _cache[key] = model
            while len(_cache) > CACHE_SIZE:
                _cache.popitem(last=False)
        else:
            _cache.move_to_end(key)
        return model


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
