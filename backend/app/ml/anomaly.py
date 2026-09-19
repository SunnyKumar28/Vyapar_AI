"""anomaly-svc (§5): day-of-week aware MAD z-score on daily GMV.

Deterministic, cheap, and runs nightly for every merchant — the LLM is never in the
detection path (principle #1). Fallback: a fixed +/-2 sigma band.
"""
from __future__ import annotations

from statistics import median
from typing import Any


def _mad(values: list[float], centre: float) -> float:
    if not values:
        return 0.0
    return median([abs(v - centre) for v in values]) or 0.0


def dow_baseline(series: list[dict[str, Any]], dow: str, weeks: int = 4, exclude_last: bool = True) -> list[int]:
    """The same weekday over the previous `weeks` weeks — the comparison a shopkeeper makes."""
    same = [p for p in series if p["dow"] == dow]
    if exclude_last and same:
        same = same[:-1]
    return [int(p["gmv_paise"]) for p in same[-weeks:]]


def detect(series: list[dict[str, Any]], *, weeks: int = 4, z_threshold: float = 2.0) -> list[dict[str, Any]]:
    """One anomaly record per day whose GMV deviates from its own weekday baseline."""
    out: list[dict[str, Any]] = []
    for idx, point in enumerate(series):
        history = series[:idx + 1]
        baseline_vals = dow_baseline(history, point["dow"], weeks=weeks, exclude_last=True)
        if len(baseline_vals) < weeks:
            continue
        baseline = sum(baseline_vals) / len(baseline_vals)
        if baseline <= 0:
            continue
        actual = int(point["gmv_paise"])
        delta_pct = (actual - baseline) / baseline
        spread = _mad([float(v) for v in baseline_vals], baseline)
        # 1.4826 scales MAD to a sigma estimate for normal data; fall back to a 12% band.
        sigma = spread * 1.4826 if spread else baseline * 0.12
        z = (actual - baseline) / sigma if sigma else 0.0
        if abs(z) < z_threshold:
            continue
        out.append({
            "date": point["date"],
            "dow": point["dow"],
            "direction": "drop" if actual < baseline else "spike",
            "actual_gmv_paise": actual,
            "baseline_gmv_paise": int(round(baseline)),
            "delta_paise": actual - int(round(baseline)),
            "delta_pct": round(delta_pct, 4),
            "z_score": round(z, 2),
            "severity": round(min(1.0, abs(z) / 6.0), 3),
            "method": f"dow_mad_z (baseline = mean of last {weeks} {point['dow']}s)",
            "baseline_samples_paise": baseline_vals,
            "txns": point["txns"],
        })
    return out


def fallback_band(series: list[dict[str, Any]], sigmas: float = 2.0) -> list[dict[str, Any]]:
    """Heuristic fallback when the weekday history is too short (principle #5)."""
    vals = [float(p["gmv_paise"]) for p in series]
    if len(vals) < 8:
        return []
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    sd = var ** 0.5
    return [
        {"date": p["date"], "dow": p["dow"], "direction": "drop" if p["gmv_paise"] < mean else "spike",
         "actual_gmv_paise": int(p["gmv_paise"]), "baseline_gmv_paise": int(mean),
         "delta_pct": round((p["gmv_paise"] - mean) / mean, 4), "z_score": round((p["gmv_paise"] - mean) / sd, 2),
         "severity": 0.4, "method": f"fixed {sigmas} sigma band (fallback)"}
        for p in series if sd and abs(p["gmv_paise"] - mean) > sigmas * sd
    ]
