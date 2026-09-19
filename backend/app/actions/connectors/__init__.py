"""Connectors (§7): the only code allowed to touch a customer.

Each connector is idempotent on the run's idempotency_key — a retry can never double-send.
Sending happens ONLY for runs the merchant approved, after the policy engine's
pre_execute check (spend caps, frequency cap, audience ceiling, quiet hours). Cost is
booked per message SENT (the BSP charges on dispatch), which is why realized cost sits
below the quoted cap but never above it.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from ...config import settings
from ...db import demo_now, iso, jdump, q, q1, tx
from ...mkg import memory, repositories as repo
from ...playbooks import engine
from ...trust import audit, policy


def _audience_rows(merchant_id: int, selector: str | None) -> list[dict[str, Any]]:
    """Re-derive the audience deterministically at send time — same selector, same cap."""
    cutoff = iso(demo_now() - timedelta(days=settings.lapsed_gap_days))
    if selector == "active_regulars":
        sql = ("SELECT customer_tok, rfm_decile FROM customer_merchant_stats WHERE merchant_id = ? "
               "AND visits_lifetime >= ? AND last_visit_at >= ? ORDER BY rfm_decile DESC, ltv_paise DESC")
        params = (merchant_id, settings.regular_min_visits, cutoff)
    elif selector == "all_regulars":
        sql = ("SELECT customer_tok, rfm_decile FROM customer_merchant_stats WHERE merchant_id = ? "
               "AND visits_lifetime >= ? ORDER BY rfm_decile DESC, ltv_paise DESC")
        params = (merchant_id, settings.regular_min_visits)
    else:                                    # lapsed_regulars (default)
        sql = ("SELECT customer_tok, rfm_decile FROM customer_merchant_stats WHERE merchant_id = ? "
               "AND visits_lifetime >= ? AND last_visit_at < ? ORDER BY rfm_decile DESC, ltv_paise DESC")
        params = (merchant_id, settings.regular_min_visits, cutoff)
    return [dict(r) for r in q(sql, params)]


def dispatch(run: dict[str, Any], *, trace_id: str | None = None) -> dict[str, Any]:
    """Route an approved run to its connector. The single execution entry point."""
    trace = trace_id or audit.new_trace_id()
    run_id, presc = run["run_id"], run["prescription"]
    mid = run["merchant_id"]
    channel = presc.get("channel", "whatsapp")

    existing = q1("SELECT * FROM actions WHERE idempotency_key = ?", (run_id,))
    if existing:
        delivered = q1("SELECT COUNT(*) AS n FROM deliveries WHERE action_id = ? "
                        "AND status IN ('sent','delivered','read','redeemed')",
                        (existing["action_id"],))
        return {"run_id": run_id, "action_id": existing["action_id"],
                "audience": int(existing["audience_size"]),
                "delivered": int(delivered["n"]) if delivered else 0,
                "cost_paise": int(existing["cost_paise"]), "already_executed": True,
                "connector": existing["connector"]}

    audience = _audience_rows(mid, presc.get("audience_selector"))
    cap = int(presc.get("guardrails", {}).get("audience_ceiling", 1000))
    audience = audience[:cap]
    n = len(audience)
    now = demo_now()

    # ---- the pre_execute gate: the last code check before money moves -----------------
    decision = policy.check(mid, audience_size=n, cost_paise=presc["impact"]["cost_cap_paise"],
                            channel=channel, at=now, stage="pre_execute",
                            trace_id=trace, run_id=run_id)
    if not decision.allowed:
        audit.log("action", {"action": "execute.blocked", "run_id": run_id,
                             "policy": decision.as_dict()},
                  trace_id=trace, merchant_id=mid, run_id=run_id)
        raise PermissionError("policy blocked: " + "; ".join(v.message for v in decision.violations))

    action_id = f"act_{run_id[4:]}"
    cost = n * settings.impact.whatsapp_cost_paise_per_message
    with tx() as conn:
        conn.execute(
            "INSERT INTO actions(action_id, run_id, connector, idempotency_key, status,"
            " audience_size, cost_paise, payload, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (action_id, run_id, channel, run_id, "sent", n, cost,
             jdump({"connector": channel, "cost_model": "per_message_sent",
                    "policy_decision": decision.as_dict()}), iso(now)))

    if channel == "whatsapp":
        from . import mock_whatsapp
        sent = mock_whatsapp.send(run, audience, action_id, trace_id=trace)
    else:
        sent = {"delivered": n, "failed": 0}          # dashboard-only channels reach everyone

    with tx() as conn:
        conn.execute("UPDATE actions SET status='sent' WHERE action_id=?", (action_id,))
        conn.execute("UPDATE playbook_runs SET state='executed', updated_at=? WHERE run_id=?",
                     (iso(now), run_id))
        # ---- freeze the DiD arms: who was treated, who was never messaged --------------
        for a in audience:
            conn.execute("INSERT OR IGNORE INTO run_cohorts(run_id, customer_tok, arm, rfm_decile)"
                         " VALUES (?,?,?,?)",
                         (run_id, a["customer_tok"], "treatment", int(a.get("rfm_decile", 0))))
        if presc.get("measure", {}).get("control") == "matched_rfm_decile":
            lapsed = repo.lapsed_customers(mid)
            ctrl = repo.matched_controls(mid, lapsed)
            for tok in ctrl["customer_toks"]:
                conn.execute("INSERT OR IGNORE INTO run_cohorts(run_id, customer_tok, arm, rfm_decile)"
                             " VALUES (?,?,?,?)", (run_id, tok, "control", 0))
            decs = {r["customer_tok"]: int(r["rfm_decile"]) for r in q(
                "SELECT customer_tok, rfm_decile FROM customer_merchant_stats WHERE merchant_id = ?",
                (mid,))}
            for tok in ctrl["customer_toks"]:
                conn.execute("UPDATE run_cohorts SET rfm_decile=? WHERE run_id=? AND customer_tok=? "
                             "AND arm='control'", (decs.get(tok, 0), run_id, tok))

    audit.log("action", {"action": "execute", "run_id": run_id, "connector": channel,
                         "audience": n, "cost_paise": cost, "delivered": sent["delivered"],
                         "failed": sent["failed"]},
              trace_id=trace, merchant_id=mid, run_id=run_id)
    memory.append_episode(mid, occurred_on=iso(now)[:10],
                          event=f"executed {presc['playbook']} via {channel}",
                          result={"audience": n, "delivered": sent["delivered"],
                                  "cost_paise": cost}, run_id=run_id)
    return {"run_id": run_id, "action_id": action_id, "audience": n,
            "delivered": sent["delivered"], "failed": sent["failed"],
            "cost_paise": cost, "connector": channel}
