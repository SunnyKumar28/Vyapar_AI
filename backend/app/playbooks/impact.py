"""heuristic_winback_v1 — the ONLY source of ₹ projections in the system (§4.2, principle #2).

The agent's `estimate_impact` tool and the playbook engine both call this module. Neither
the model nor the prompts ever produce a rupee figure on their own. Priors come from
config, not from a vibe at generation time.

Bands, not points: we quote a [low, high] range from redemption priors 10.0-13.5%. The
cost is quoted as a CAP rounded up to the nearest ₹100 (what the merchant is committing to),
while impact is rounded to the nearest ₹100. Small audiences are flagged indicative.
"""
from __future__ import annotations

import math
from typing import Any

from ..config import settings

IMPACT_CFG = settings.impact


def _round_to(value: int, step: int) -> int:
    return int(round(value / step) * step)


def estimate_winback(n_audience: int, *, offer_cap_paise: int | None = None,
                     min_roi: float = 3.0) -> dict[str, Any]:
    """Projected [low, high] influenced GMV and the cost cap for a win-back campaign.

    All four inputs are config: redemption priors, average incremental ticket, WhatsApp
    rate, and the offer's discount cap. n_audience is the only merchant-specific number.
    """
    c = IMPACT_CFG
    ticket = c.avg_incremental_ticket_paise
    cap = offer_cap_paise if offer_cap_paise is not None else min(
        int(0.10 * ticket) // 100 * 100, 2000)   # 10% off, capped at ₹20
    msg_cost = n_audience * c.whatsapp_cost_paise_per_message
    expected_redemptions = math.ceil(n_audience * c.redemption_rate_high)
    redeem_cost = expected_redemptions * cap
    cost_cap = _round_to(msg_cost + redeem_cost, c.cost_cap_rounding_paise) \
        + (c.cost_cap_rounding_paise if (msg_cost + redeem_cost) % c.cost_cap_rounding_paise else 0)
    low = _round_to(int(n_audience * c.redemption_rate_low * ticket), c.impact_rounding_paise)
    high = _round_to(int(n_audience * c.redemption_rate_high * ticket), c.impact_rounding_paise)
    raw_cost = msg_cost + redeem_cost
    projected_roi_low = round((low - raw_cost) / raw_cost, 2) if raw_cost else None
    return {
        "model": "heuristic_winback_v1",
        "audience": n_audience,
        "assumptions": {
            "redemption_rate_band": [c.redemption_rate_low, c.redemption_rate_high],
            "avg_incremental_ticket_paise": ticket,
            "whatsapp_cost_paise_per_message": c.whatsapp_cost_paise_per_message,
            "offer_cap_paise": cap,
            "expected_redemptions": expected_redemptions,
        },
        "gmv_influenced_band_paise": [low, high],
        "cost_cap_paise": cost_cap,
        "raw_cost_estimate_paise": raw_cost,
        "projected_roi_low": projected_roi_low,
        "meets_min_roi": projected_roi_low is not None and projected_roi_low >= min_roi,
        "confidence": "indicative" if n_audience < 50 else "projected",
    }


def estimate_simple(n_audience: int, *, per_head_paise: int,
                    uplift_band: tuple[float, float]) -> dict[str, Any]:
    """Bands for the non-winback playbooks (stockout, festive, loyalty, udhaar).

    Deliberately cruder and labelled as such: these are planning aids, not commitments,
    and the report card never books them as measured revenue.
    """
    low = _round_to(int(n_audience * uplift_band[0] * per_head_paise), IMPACT_CFG.impact_rounding_paise)
    high = _round_to(int(n_audience * uplift_band[1] * per_head_paise), IMPACT_CFG.impact_rounding_paise)
    return {
        "model": "planning_band_v1",
        "audience": n_audience,
        "gmv_influenced_band_paise": [low, high],
        "cost_cap_paise": 0 if not n_audience else IMPACT_CFG.whatsapp_cost_paise_per_message * n_audience,
        "confidence": "indicative",
    }
