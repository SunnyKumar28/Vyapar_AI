"""The world simulator (§13 deviation, clearly labelled in the UI and audit log).

In production, redemptions arrive through the payment-event stream and the WhatsApp
webhook. There is no payment rail in the sandbox, so this module plays that rail: when
the demo clock advances, it materializes the events that "happened" in the elapsed time —
redemptions by delivered customers, organic control-arm returns, and follow-on visits.

What it is NOT: it is not a fixture that writes outcomes. Every number it produces is a
transaction with an event_time, and attribution reads those transactions through the same
query path it would use on real data. `python -m eval.verify_demo` proves the chain.

Owns a private `sim_state` table (module-owned, like pii_vault — demo-world
infrastructure, not part of the §11 production schema) so re-advancing the clock never
double-writes an event.
"""
from __future__ import annotations

import random
from datetime import timedelta
from typing import Any

from ...config import settings
from ...db import connect, demo_now, init_db, iso, jdump, parse, q, q1, tx
from ...mkg import repositories as repo
from ...trust import audit

_DDL = """
CREATE TABLE IF NOT EXISTS sim_state (
  run_id    TEXT NOT NULL,
  window_d  INTEGER NOT NULL,
  done_at   TEXT NOT NULL,
  PRIMARY KEY (run_id, window_d)
);
"""


def ensure() -> None:
    connect().executescript(_DDL)          # outside any transaction; idempotent


def _apportion(total: int, weights: list[float]) -> list[int]:
    n = len(weights)
    if n == 0:
        return []
    s = float(sum(weights)) or 1.0
    exact = [total * w / s for w in weights]
    base = [int(x) for x in exact]
    order = sorted(range(n), key=lambda i: (-(exact[i] - base[i]), i))
    for i in range(total - sum(base)):
        base[order[i % n]] += 1
    return base


def _scenario_post() -> dict[str, Any]:
    import json
    sc = json.loads(settings.scenario_path.read_text())
    return sc.get("post_campaign", {})


def materialize(run_id: str, window_d: int, *, trace_id: str | None = None) -> dict[str, Any]:
    """Write the post-period transactions for one executed run, once per window."""
    ensure()
    if q1("SELECT 1 FROM sim_state WHERE run_id = ? AND window_d = ?", (run_id, window_d)):
        return {"run_id": run_id, "window_d": window_d, "already_materialized": True}
    run = repo.run(run_id)
    if not run:
        raise ValueError(f"unknown run {run_id}")
    if not q1("SELECT 1 FROM actions WHERE run_id = ?", (run_id,)):
        return {"run_id": run_id, "window_d": window_d,
                "skipped": "no action executed for this run (seeded history)"}
    send = parse(run["approved_at"] or run["created_at"])
    now = demo_now()
    if now < send + timedelta(days=window_d):
        return {"run_id": run_id, "window_d": window_d,
                "skipped": f"window completes at {iso(send + timedelta(days=window_d))}; clock is {iso(now)}"}

    mid = run["merchant_id"]
    post = _scenario_post()
    rng = random.Random(f"{settings.seed}:{run_id}:{window_d}")
    rows: list[tuple] = []
    kind = "redemptions"

    # ---- treatment arm: redemptions land only for DELIVERED recipients ---------------
    delivered = [r["customer_tok"] for r in q(
        "SELECT customer_tok FROM deliveries d JOIN actions a ON a.action_id = d.action_id "
        "WHERE a.run_id = ? AND d.status IN ('delivered','read','redeemed')", (run_id,))]
    treated = [r["customer_tok"] for r in q(
        "SELECT customer_tok FROM run_cohorts WHERE run_id = ? AND arm = 'treatment'", (run_id,))]
    if window_d <= 7:
        n_redeem = min(post["redemptions_7d"], len(delivered))
        redeemers = rng.sample(delivered, n_redeem)
        amounts = _apportion(post["redemption_gmv_paise_7d"],
                             [rng.uniform(0.8, 1.25) for _ in redeemers])
        for tok, amt in zip(redeemers, amounts):
            t = send + timedelta(days=rng.uniform(0.05, window_d - 0.2))
            rows.append((mid, tok, amt, "upi", "winback-visit", iso(t), iso(t + timedelta(seconds=45)), run_id))
        kind = "redemptions"
    else:
        # ---- the 30-day follow-on: 66 of the treated cohort come back, redeemers first ----
        target = post["treated_repeat_customers_30d"]
        base = [r["customer_tok"] for r in q(
            "SELECT DISTINCT t.customer_tok FROM transactions t WHERE t.source_run_id = ?",
            (run_id,))]
        rest = [t for t in treated if t not in set(base)]
        comeback = list(base) + rng.sample(rest, max(0, target - len(base)))
        comeback = comeback[:target]
        for tok in comeback:
            n_visits = 2 if tok in set(base) else 1          # a redemption + a follow-on
            for _ in range(n_visits):
                t = send + timedelta(days=rng.uniform(7.05, window_d - 0.5))
                amt = _apportion(post["followon_avg_ticket_paise"],
                                 [rng.uniform(0.7, 1.3)])[0]
                rows.append((mid, tok, amt, "upi", "follow-on", iso(t),
                             iso(t + timedelta(seconds=60)), None))
        kind = "follow_on_visits"

    # ---- control arm: organic returns, no campaign touched them -----------------------
    control = [r["customer_tok"] for r in q(
        "SELECT customer_tok FROM run_cohorts WHERE run_id = ? AND arm = 'control'", (run_id,))]
    if window_d <= 7 and control:
        returns = rng.sample(control, min(post["control_returners_7d"], len(control)))
        amounts = _apportion(post["control_gmv_paise_7d"], [rng.uniform(0.8, 1.2) for _ in returns])
        for tok, amt in zip(returns, amounts):
            t = send + timedelta(days=rng.uniform(0.1, window_d - 0.2))
            rows.append((mid, tok, amt, "upi", "organic", iso(t), iso(t + timedelta(seconds=90)), None))

    with tx() as conn:
        conn.executemany(
            "INSERT INTO transactions(merchant_id, customer_tok, amount_paise, channel, item,"
            " event_time, ingest_time, source_run_id) VALUES (?,?,?,?,?,?,?,?)", rows)
        conn.execute("INSERT INTO sim_state(run_id, window_d, done_at) VALUES (?,?,?)",
                     (run_id, window_d, iso(now)))
    audit.log("job", {"job": "world_sim.materialize", "run_id": run_id, "window_d": window_d,
                      "kind": kind, "events": len(rows),
                      "note": "sandbox payment-rail stand-in; events are real transactions"},
              trace_id=trace_id, merchant_id=mid, run_id=run_id)
    return {"run_id": run_id, "window_d": window_d, "kind": kind, "events": len(rows)}


def advance(days: int, *, trace_id: str | None = None) -> dict[str, Any]:
    """The demo's clock control: move T forward and let the elapsed world happen."""
    ensure()
    from ...playbooks import engine
    trace = trace_id or audit.new_trace_id()
    old = demo_now()
    new = old + timedelta(days=days)
    with tx() as conn:
        conn.execute("UPDATE clock SET now = ? WHERE id = 1", (iso(new),))
    audit.log("job", {"job": "world_sim.advance", "days": days, "from": iso(old), "to": iso(new),
                      "note": "demo clock control"}, trace_id=trace)
    done: list[dict[str, Any]] = []
    # Materialize every executed run's completed windows, oldest first.
    for r in q("SELECT run_id FROM playbook_runs WHERE state IN ('executed','measuring','closed')"
               " ORDER BY created_at"):
        for window_d in (7, 30):
            done.append(materialize(r["run_id"], window_d, trace_id=trace))
    # Runs whose 7d window completed but were never executed stay as they are.
    for d in done:
        if d.get("events"):
            from ...outcomes import attribution
            attribution.measure(d["run_id"], d["window_d"], trace_id=trace)
    return {"advanced_days": days, "now": iso(new), "materialized": done}


def reset_clock(days: int = 0) -> None:
    """Snap the clock back (demo reset). Day 0 = the datagen pin."""
    ensure()
    with tx() as conn:
        conn.execute("UPDATE clock SET now = ? WHERE id = 1",
                     (iso(demo_now() - timedelta(days=days)),))
