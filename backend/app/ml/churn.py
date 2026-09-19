"""churn-svc (§5). MVP is a calibrated logistic score over RFM features, with the
45-day recency rule as the fallback that keeps the win-back playbook working if the
model is down or fails its AUC gate (principle #5, §14)."""
from __future__ import annotations

import math
from typing import Any

# Coefficients hand-set from the RFM literature and sanity-checked against datagen labels.
# Trained per-vertical in production; the interface does not change.
W = {"intercept": -2.15, "recency": 0.055, "frequency": -0.085, "ticket": -0.0000045, "gap_trend": 0.42}


def score(*, days_since_visit: int, visits_90d: int, avg_ticket_paise: int, gap_trend: float = 0.0) -> float:
    z = (W["intercept"] + W["recency"] * days_since_visit + W["frequency"] * visits_90d
         + W["ticket"] * avg_ticket_paise + W["gap_trend"] * gap_trend)
    return round(1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z)))), 4)


def fallback(days_since_visit: int, threshold: int = 45) -> float:
    """The recency rule. Blunt, explainable, and never wrong about what it claims."""
    return 1.0 if days_since_visit >= threshold else 0.0


def auc(labelled: list[tuple[float, int]]) -> float:
    """Rank-based AUC. §14 gate: churn AUC >= 0.75 before scores influence prescriptions."""
    pos = [s for s, y in labelled if y == 1]
    neg = [s for s, y in labelled if y == 0]
    if not pos or not neg:
        return 0.5
    wins = sum(1 for p in pos for n in neg if p > n) + 0.5 * sum(1 for p in pos for n in neg if p == n)
    return round(wins / (len(pos) * len(neg)), 4)


def gate(labelled: list[tuple[float, int]], min_auc: float = 0.75) -> dict[str, Any]:
    a = auc(labelled)
    return {
        "passed": a >= min_auc,
        "auc": a,
        "min_auc": min_auc,
        "n": len(labelled),
        "serving": "model" if a >= min_auc else "recency_rule_45d (fallback)",
    }
