"""forecast-svc (§5). MVP is a ridge-free linear model on lags + day-of-week dummies,
gated against the 4-week moving-average baseline it must beat (§14 model gates)."""
from __future__ import annotations

from typing import Any

DOWS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def moving_average(series: list[dict[str, Any]], weeks: int = 4) -> dict[str, float]:
    """Baseline: mean of the same weekday over the last `weeks` weeks."""
    out: dict[str, float] = {}
    for dow in DOWS:
        vals = [float(p["gmv_paise"]) for p in series if p["dow"] == dow][-weeks:]
        if vals:
            out[dow] = sum(vals) / len(vals)
    return out


def _fit_dow_lag(series: list[dict[str, Any]]) -> dict[str, Any]:
    """Weekday mean plus a lag-7 correction learned by least squares on one coefficient."""
    base = moving_average(series, weeks=8)
    num = den = 0.0
    for i in range(7, len(series)):
        p, prev = series[i], series[i - 7]
        b = base.get(p["dow"])
        if not b:
            continue
        x = float(prev["gmv_paise"]) - b
        y = float(p["gmv_paise"]) - b
        num += x * y
        den += x * x
    beta = (num / den) if den else 0.0
    return {"base": base, "beta": round(beta, 4)}


def predict(series: list[dict[str, Any]], horizon: int = 7) -> dict[str, Any]:
    if len(series) < 21:
        base = moving_average(series)
        return {"model": "moving_average_4w (insufficient history)", "beta": 0.0,
                "forecast": [{"dow": d, "gmv_paise": int(v)} for d, v in base.items()]}
    model = _fit_dow_lag(series[:-7])
    last_week = {p["dow"]: float(p["gmv_paise"]) for p in series[-7:]}
    fc = []
    for d in DOWS:
        b = model["base"].get(d)
        if b is None:
            continue
        pred = b + model["beta"] * (last_week.get(d, b) - b)
        fc.append({"dow": d, "gmv_paise": int(round(pred))})
    return {"model": "dow_mean + lag7 correction", "beta": model["beta"],
            "forecast": fc[:horizon], "next_week_gmv_paise": int(sum(f["gmv_paise"] for f in fc))}


def gate(series: list[dict[str, Any]]) -> dict[str, Any]:
    """§14: the model ships only if its MAE beats the moving-average baseline on a holdout."""
    if len(series) < 35:
        return {"passed": False, "reason": "need >= 35 days of history"}
    train, test = series[:-7], series[-7:]
    model = _fit_dow_lag(train)
    base = moving_average(train)
    prev = {p["dow"]: float(p["gmv_paise"]) for p in train[-7:]}
    m_err = b_err = 0.0
    for p in test:
        b = base.get(p["dow"])
        if b is None:
            continue
        m_err += abs(float(p["gmv_paise"]) - (b + model["beta"] * (prev.get(p["dow"], b) - b)))
        b_err += abs(float(p["gmv_paise"]) - b)
    n = max(1, len(test))
    model_mae, base_mae = m_err / n, b_err / n
    return {
        "passed": model_mae <= base_mae,
        "model_mae_paise": int(model_mae),
        "baseline_mae_paise": int(base_mae),
        "improvement_pct": round((base_mae - model_mae) / base_mae * 100, 2) if base_mae else 0.0,
        "serving": "model" if model_mae <= base_mae else "moving_average_4w (fallback)",
    }
