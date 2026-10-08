"""The forecast contract: the stable JSON shape every forecast engine must return.

The dashboard, the chat panel and the optimizer read only this shape, so the
stand-in model can later be swapped for a real one without touching them.
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from .forecast import MODEL_NAME, MODEL_VERSION, QUANTILES, EnsembleForecaster, DayInputs, mixture_quantiles

CONTRACT_VERSION = "1.0"
UNIT = "guests arriving per hour"
LABELS = {
    "volatility": "Day-to-day variation",
    "missing_history": "Limited history",
}
GUIDANCE = {
    "volatility": "Cannot be reduced by more data. Cover it with a staffing buffer: pick a plan further along the trade-off.",
    "missing_history": "Can be reduced with knowledge the model lacks. Ask the planner what they know about this gate or hour.",
}


def _q(values: np.ndarray) -> dict:
    p10, p50, p90 = np.percentile(values, [10, 50, 90])
    return {"p10": round(float(p10)), "p50": round(float(p50)), "p90": round(float(p90))}


def build_forecast(
    model: EnsembleForecaster,
    event: dict,
    inputs: DayInputs,
    assumptions: list[dict],
    *,
    scenario_id: str,
    forecast_id: str,
    seed: int = 0,
) -> tuple[dict, np.ndarray, np.ndarray]:
    """Return (contract, member_mu, member_sd). The member arrays feed the optimizer's sampling."""
    gate_ids = [g["id"] for g in event["gates"]]
    gate_names = {g["id"]: g["name"] for g in event["gates"]}
    hours = list(range(event["open_hour"], event["close_hour"]))
    mu, sd = model.member_distributions(gate_ids, hours, inputs)  # (M, G, H)
    m, g, h = mu.shape

    vol_var = (sd**2).mean(axis=0)
    miss_var = mu.var(axis=0)
    total_sd = np.sqrt(vol_var + miss_var)
    quant = np.maximum(mixture_quantiles(mu.reshape(m, -1), sd.reshape(m, -1), QUANTILES).reshape(3, g, h), 0)

    rng = np.random.default_rng(seed)
    members = rng.integers(0, m, size=4000)
    draws = np.maximum(rng.normal(mu[members], sd[members]), 0)  # (S, G, H)

    series = []
    for gi, gate in enumerate(gate_ids):
        info = model.history_info.get(gate, {"days": 0, "thin": True})
        points = []
        for hi, hour in enumerate(hours):
            share = float(miss_var[gi, hi] / max(vol_var[gi, hi] + miss_var[gi, hi], 1e-9))
            dominant = "missing_history" if share >= 0.5 else "volatility"
            points.append(
                {
                    "hour": f"{hour:02d}:00",
                    "p10": round(float(quant[0, gi, hi])),
                    "p50": round(float(quant[1, gi, hi])),
                    "p90": round(float(quant[2, gi, hi])),
                    "sd_total": round(float(total_sd[gi, hi]), 1),
                    "sd_volatility": round(float(np.sqrt(vol_var[gi, hi])), 1),
                    "sd_missing_history": round(float(np.sqrt(miss_var[gi, hi])), 1),
                    "share_missing_history": round(share, 3),
                    "dominant": dominant,
                }
            )
        series.append(
            {
                "gate_id": gate,
                "gate_name": gate_names[gate],
                "history": info,
                "day_total": _q(draws[:, gi, :].sum(axis=1)),
                "points": points,
            }
        )

    hourly_totals = [{"hour": f"{hour:02d}:00", **_q(draws[:, :, hi].sum(axis=1))} for hi, hour in enumerate(hours)]

    data_notes = []
    for gate in gate_ids:
        info = model.history_info.get(gate, {})
        if info.get("thin"):
            data_notes.append(
                {
                    "kind": "thin_history",
                    "gate_id": gate,
                    "text": f"{gate_names[gate]} has only {info.get('days', 0)} days of history "
                    f"(since {info.get('first_day')}).",
                }
            )
    weather_days = model.seen.get("weather_days", {}).get(inputs.weather, 0)
    if weather_days < 15:
        data_notes.append(
            {"kind": "rare_condition", "text": f"Only {weather_days} history days had '{inputs.weather}' weather."}
        )
    if inputs.show_start is not None and inputs.show_start not in model.seen.get("show_starts", []):
        seen = ", ".join(f"{s:02d}:00" for s in model.seen.get("show_starts", []))
        data_notes.append(
            {
                "kind": "unseen_condition",
                "text": f"History has no day with the show at {inputs.show_start:02d}:00 (seen: {seen}). "
                "The model extrapolates; its range may understate the real uncertainty.",
            }
        )
    for level, label in ((1, "minor"), (2, "major")):
        if level in inputs.competing.values():
            days = model.seen.get("competing_days", {}).get(level, 0)
            data_notes.append(
                {"kind": "rare_condition", "text": f"Only {days} history days had a {label} competing event."}
            )

    contract = {
        "contract_version": CONTRACT_VERSION,
        "forecast_id": forecast_id,
        "event_id": event["id"],
        "scenario_id": scenario_id,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_cutoff": model.data_cutoff.isoformat() if model.data_cutoff else None,
        "model": {"name": MODEL_NAME, "version": MODEL_VERSION, "members": m},
        "unit": UNIT,
        "timezone": event["timezone"],
        "date": event["date"],
        "interval": {"lower": "p10", "upper": "p90", "level": 0.8},
        "inputs": {
            "day_type": inputs.day_type,
            "weather": inputs.weather,
            "show_start": None if inputs.show_start is None else f"{inputs.show_start:02d}:00",
            "competing_by_hour": {f"{k:02d}:00": v for k, v in sorted(inputs.competing.items())},
        },
        "assumptions": assumptions,
        "uncertainty_labels": LABELS,
        "uncertainty_guidance": GUIDANCE,
        "series": series,
        "totals": {"day": _q(draws.sum(axis=(1, 2))), "hourly": hourly_totals},
        "data_notes": data_notes,
        "backtest": model.backtest,
    }
    return contract, mu, sd


def summarize_forecast(contract: dict, event: dict, scenario_name: str) -> str:
    """Plain-text summary for Claude (tool `content`): the panel shows the chart, this is what Claude reads."""
    lines = [
        f"Forecast for {event['name']} on {contract['date']} ({event['venue']}), scenario '{scenario_name}'.",
        f"Unit: {contract['unit']}, local time {contract['timezone']}. Ranges are 80% (p10-p90).",
        f"Whole day: median {contract['totals']['day']['p50']:,} guests "
        f"(range {contract['totals']['day']['p10']:,}-{contract['totals']['day']['p90']:,}).",
    ]
    for s in contract["series"]:
        peak = max(s["points"], key=lambda p: p["p50"])
        widest = max(s["points"], key=lambda p: p["sd_total"])
        missing_hours = [p["hour"] for p in s["points"] if p["dominant"] == "missing_history"]
        avg_share = sum(p["share_missing_history"] for p in s["points"]) / len(s["points"])
        source = (
            f"limited history dominates at {', '.join(missing_hours)}"
            if missing_hours
            else f"mostly day-to-day variation (limited history explains {avg_share:.0%} on average)"
        )
        thin = " THIN HISTORY." if s["history"].get("thin") else ""
        lines.append(
            f"- {s['gate_name']} [{s['gate_id']}]: peak {peak['hour']} median {peak['p50']:,} "
            f"({peak['p10']:,}-{peak['p90']:,}); widest range at {widest['hour']}; uncertainty {source}; "
            f"{s['history'].get('days', 0)} history days.{thin}"
        )
    for note in contract["data_notes"]:
        lines.append(f"Data note: {note['text']}")
    thin_gates = [s["gate_name"] for s in contract["series"] if s["history"].get("thin")]
    if thin_gates:
        lines.append(
            "Ask the planner: what do they know about "
            + " and ".join(thin_gates)
            + " that a few days of data cannot show (parking capacity, shuttle times, signage, expected share of "
            "visitors)? If their answer changes arrivals or staffing, record it with propose_constraint; otherwise "
            "as note_only."
        )
    bt = contract.get("backtest") or {}
    if bt:
        lines.append(
            f"Backtest: the 80% ranges contained {bt['coverage_p10_p90']:.0%} of held-out hours "
            f"(target 80%); typical error {bt['wape']:.0%} of volume."
        )
    if contract["assumptions"]:
        lines.append("Assumptions in this forecast:")
        lines.extend(f"  * {a['text']}" for a in contract["assumptions"])
    return "\n".join(lines)
