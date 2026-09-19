"""The weekly report card (§8): one screen, numbers the merchant can act on.

Every figure is computed from outcomes + transactions at render time. Seeded history is
summed but always labelled separately from what the loop measured live — the card never
blends a demo fixture into a measurement.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..config import settings
from ..db import demo_now, iso, jload, q
from ..mkg import repositories as repo
from ..trust import policy


def report(merchant_id: int) -> dict[str, Any]:
    now = demo_now()
    # One row per run: the outcome for the playbook's own measure window (7d for win-back,
    # 30d for loyalty), else the widest window. A run never counts twice on this card.
    rows = q(
        "SELECT o.*, r.playbook, r.state, r.approved_at, r.prescription FROM outcomes o "
        "JOIN playbook_runs r ON r.run_id = o.run_id WHERE r.merchant_id = ? "
        "ORDER BY o.computed_at", (merchant_id,))
    picked: dict[str, Any] = {}
    for r in rows:
        presc = jload(r["prescription"] or "{}") or {}
        primary = int((presc.get("measure") or {}).get("window_days") or 0)
        cur = picked.get(r["run_id"])
        if cur is None:
            picked[r["run_id"]] = r
        elif int(r["window_d"]) == primary:
            picked[r["run_id"]] = r          # the playbook's own window always wins
    rows = list(picked.values())
    live = [r for r in rows if jload(r["detail"] or "{}").get("source") != "seeded_history"]
    seeded = [r for r in rows if jload(r["detail"] or "{}").get("source") == "seeded_history"]
    live_recovered = sum(int(r["gmv_influenced_paise"] or 0) for r in live)
    seeded_recovered = sum(int(r["gmv_influenced_paise"] or 0) for r in seeded)
    total_cost = sum(int(r["cost_paise"] or 0) for r in rows)

    treated = None
    for r in sorted(live, key=lambda r: r["approved_at"] or ""):
        toks = repo.cohort(r["run_id"], "treatment")
        send = parse_safe(r["approved_at"] or "")
        if toks and send and demo_now() >= send + timedelta(days=30):
            rr = repo.cohort_repeat_rate(merchant_id, toks, send, send + timedelta(days=30))
            # The "before" side of the comparison is the 30 days BEFORE the campaign —
            # measuring the post-campaign window against itself would flatter the result.
            before = repo.repeat_rate(merchant_id, 30, until=send)
            treated = {"cohort": rr["cohort_size"], "returned": rr["returned"],
                       "rate": round(rr["returned"] / rr["cohort_size"], 4),
                       "metric": "share of treated cohort that transacted within 30 days",
                       "baseline": before["repeat_rate"],
                       "baseline_note": "merchant-wide repeat rate over the 30d before send"}

    return {
        "as_of": iso(now),
        "headline": {
            "actions_run": len(rows),
            "recovered_revenue_paise": {"live_measured": live_recovered,
                                        "seeded_history": seeded_recovered,
                                        "total": live_recovered + seeded_recovered},
            "total_cost_paise": total_cost,
            "blended_roi": round(((live_recovered + seeded_recovered) - total_cost) / total_cost, 2)
            if total_cost else None,
        },
        "repeat_rate": {
            "merchant_30d": repo.repeat_rate(merchant_id, 30),
            "treated_cohort_30d": treated,
            "note": "treated cohort = customers messaged by the live win-back run; "
                    "rate = share who came back within 30 days",
        },
        "guardrails": {
            "spend_this_week_paise": policy.spend_this_week(merchant_id, now),
            "spend_cap_week_paise": settings.policy.spend_cap_paise_per_week,
            "campaigns_this_week": policy.campaigns_this_week(merchant_id, now),
            "campaigns_cap_week": settings.policy.campaigns_per_week,
        },
        "runs": [
            {"run_id": r["run_id"], "playbook": r["playbook"], "state": r["state"],
             "window_d": r["window_d"], "gmv_influenced_paise": int(r["gmv_influenced_paise"] or 0),
             "cost_paise": int(r["cost_paise"] or 0), "roi": r["roi"],
             "redemptions": int(r["redemptions"] or 0), "confidence": r["confidence"],
             "source": (jload(r["detail"] or "{}") or {}).get("source",
                       "live" if jload(r["detail"] or "{}").get("method") else "live")}
            for r in rows
        ],
        "reminders": [dict(r) for r in q(
            "SELECT note, due_at FROM reminders WHERE merchant_id = ? AND due_at >= ? "
            "ORDER BY due_at LIMIT 5", (merchant_id, iso(now - timedelta(days=1))))],
    }


def parse_safe(ts: str):
    from ..db import parse
    try:
        return parse(ts) if ts else None
    except Exception:
        return None
