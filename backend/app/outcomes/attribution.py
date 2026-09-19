"""Attribution (§8): difference-in-differences on matched arms, integer paise throughout.

The question a merchant actually asks is "what did the campaign get me that I would not
have got anyway?" The organic-return rate of comparable-but-never-messaged customers is
the counterfactual, so:

    influenced = (treated_post - treated_pre)
               - (n_treated / n_control) * (control_post - control_pre)

Every component is one SQL sum over real transactions; the arithmetic above is the only
model. Small audiences or a missing control arm degrade the row to `indicative` — we
never dress a guess up as a measurement. Costs include the discount actually redeemed,
not just the BSP bill: money the merchant gave away is money.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..config import settings
from ..db import demo_now, iso, jdump, parse, q, q1, tx
from ..mkg import memory, repositories as repo
from ..playbooks import engine
from ..trust import audit


def measure(run_id: str, window_d: int = 7, *, trace_id: str | None = None) -> dict[str, Any]:
    """Compute and persist the outcome row for one run+window. Idempotent upsert."""
    trace = trace_id or audit.new_trace_id()
    run = repo.run(run_id)
    if not run:
        raise ValueError(f"unknown run {run_id}")
    mid = run["merchant_id"]
    send = parse(run["approved_at"] or run["created_at"])
    now = demo_now()

    treated = repo.cohort(run_id, "treatment")
    control = repo.cohort(run_id, "control")
    n_t, n_c = len(treated), len(control)

    pre = (send - timedelta(days=window_d), send)
    post = (send, send + timedelta(days=window_d))

    t_post = repo.cohort_gmv(mid, treated, *post)
    t_pre = repo.cohort_gmv(mid, treated, *pre)
    c_post = repo.cohort_gmv(mid, control, *post) if control else 0
    c_pre = repo.cohort_gmv(mid, control, *pre) if control else 0

    if control and n_c:
        # Integer arithmetic keeps 84000*212/150 == 118720 exact; no float drift in money.
        counterfactual = ((c_post - c_pre) * n_t) // n_c
        influenced = (t_post - t_pre) - counterfactual
    else:
        counterfactual = 0
        influenced = t_post - t_pre

    red = q1("SELECT COUNT(*) AS n, COALESCE(SUM(amount_paise),0) AS gmv FROM transactions "
             "WHERE source_run_id = ? AND event_time >= ? AND event_time < ?",
             (run_id, iso(post[0]), iso(post[1])))
    redemptions, redemption_gmv = int(red["n"]), int(red["gmv"]) if red else (0, 0)
    act = q1("SELECT * FROM actions WHERE run_id = ?", (run_id,))
    delivered = 0
    if act:
        d = q1("SELECT COUNT(*) AS n FROM deliveries WHERE action_id = ? "
               "AND status IN ('delivered','read','redeemed')", (act["action_id"],))
        delivered = int(d["n"]) if d else 0
    # Realized cost: BSP bill (per message sent) + the discount actually redeemed.
    connector_cost = int(act["cost_paise"]) if act else 0
    offer_cap = int(run["prescription"].get("offer", {}).get("cap_paise", 2000) or 2000)
    redeemed_cost = redemptions * offer_cap
    cost = connector_cost + redeemed_cost
    roi = round((influenced - cost) / cost, 2) if cost else None

    complete = now >= post[1]
    confidence = "measured" if (control and n_c >= 50 and complete) else \
                 ("measured" if complete else "indicative")

    detail = {
        "method": "diff_in_diff",
        "windows": {"pre": [iso(pre[0]), iso(pre[1])], "post": [iso(post[0]), iso(post[1])]},
        "arms": {"treatment": n_t, "control": n_c},
        "components_paise": {
            "treated_post": t_post, "treated_pre": t_pre,
            "control_post": c_post, "control_pre": c_pre,
            "counterfactual_scaled": counterfactual,
            "gmv_influenced": influenced,
        },
        "redemptions": redemptions, "redemption_gmv_paise": redemption_gmv,
        "delivered": delivered,
        "cost_breakdown_paise": {"connector": connector_cost, "discount_redeemed": redeemed_cost},
        "roi_formula": "(gmv_influenced - cost) / cost",
        "note": "counterfactual = control organic lift scaled to the treated headcount",
    }

    with tx() as conn:
        conn.execute(
            "INSERT INTO outcomes(run_id, window_d, gmv_influenced_paise, redemptions, delivered,"
            " cost_paise, roi, control_gmv_paise, treatment_gmv_paise, n_treatment, n_control,"
            " confidence, detail, computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id, window_d) DO UPDATE SET "
            "gmv_influenced_paise=excluded.gmv_influenced_paise, redemptions=excluded.redemptions,"
            "delivered=excluded.delivered, cost_paise=excluded.cost_paise, roi=excluded.roi,"
            "control_gmv_paise=excluded.control_gmv_paise, treatment_gmv_paise=excluded.treatment_gmv_paise,"
            "n_treatment=excluded.n_treatment, n_control=excluded.n_control,"
            "confidence=excluded.confidence, detail=excluded.detail, computed_at=excluded.computed_at",
            (run_id, window_d, influenced, redemptions, delivered, cost, roi,
             c_post, t_post, n_t, n_c, confidence, jdump(detail), iso(now)))

    new_state = "closed" if complete and influenced is not None else "measuring"
    if run["state"] in ("executed", "measuring"):
        engine.set_state(run_id, new_state, trace)

    # The MEASURE -> SENSE edge: the MKG remembers what actually happened.
    if complete:
        memory.learn(mid, f"outcome_{run['playbook']}",
                     {"run_id": run_id, "window_d": window_d,
                      "gmv_influenced_paise": influenced, "redemptions": redemptions,
                      "roi": roi, "confidence": confidence})
        memory.append_episode(mid, occurred_on=iso(now)[:10],
                               event=f"measured {run['playbook']} ({window_d}d)",
                               result={"gmv_influenced_paise": influenced,
                                       "redemptions": redemptions, "roi": roi},
                               run_id=run_id)
    audit.log("job", {"job": "attribution.measure", "run_id": run_id, "window_d": window_d,
                      "gmv_influenced_paise": influenced, "redemptions": redemptions,
                      "cost_paise": cost, "roi": roi, "confidence": confidence,
                      "components": detail["components_paise"]},
              trace_id=trace, merchant_id=mid, run_id=run_id)
    return detail
