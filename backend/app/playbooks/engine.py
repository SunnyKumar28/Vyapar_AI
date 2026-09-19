"""Playbook engine (§6). Turns a signal into a prescription awaiting approval.

The state machine, verbatim from the plan:
    detected -> diagnosed -> prescribed -> awaiting_approval -> executing
             -> executed -> measuring -> closed | dismissed
This module owns the first three transitions plus approve/reject; the connectors own
executing; attribution owns measuring/closed.

Nothing here spends money or contacts a customer. The only side effects before
`awaiting_approval` are rows. That is the whole point of approval-gating (principle #3).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from ..config import settings
from ..db import connect, demo_now, iso, jdump, jload, q, q1, tx
from ..mkg import memory, repositories as repo
from ..playbooks.impact import estimate_simple, estimate_winback
from ..trust import approvals, audit

STATES = ("detected", "diagnosed", "prescribed", "awaiting_approval", "executing",
          "executed", "measuring", "closed", "dismissed")

COPY = {
    # Written the way a chai-wallah actually messages: short, Hindi, one clear ask.
    "winback_hindi": ("{name} ji, aap bahut din se nahi aaye! Kal hi aa jao — aapke liye "
                      "10% tak chhoot (max ₹20) rakhi hai. — {shop}"),
    "sales_dip_hindi": ("{name} ji, is hafte special: 10% chhoot (max ₹20) aaj-kal ke liye. "
                        "Chai ke saath samosa bhi le lo. — {shop}"),
    "loyalty_hindi": ("{name} ji, ab 5 visit par 6th free! Aapka card WhatsApp par hi "
                      "chalega. — {shop}"),
    "festive_hindi": ("{name} ji, {event} special combo aa raha hai! Advance booking par "
                      "extra ₹30 ki bachat. — {shop}"),
    "udhaar_hindi": ("{name} ji, nishchint rahe — apni suvida par udhaar clear kar sakte "
                     "hain. Jab chahein bata dijiye. — {shop}"),
    "stockout_advice_hindi": "advisory (dashboard only, no customer message)",
}


def load_playbooks(directory: Path | None = None) -> dict[str, dict[str, Any]]:
    d = directory or settings.playbook_dir
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(d.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text())
        out[doc["id"]] = doc
    return out


PLAYBOOKS: dict[str, dict[str, Any]] | None = None


def playbooks() -> dict[str, dict[str, Any]]:
    global PLAYBOOKS
    if PLAYBOOKS is None:
        PLAYBOOKS = load_playbooks()
    return PLAYBOOKS


# ---------------------------------------------------------------- audience builders

def _audience(pb: dict[str, Any], merchant_id: int, limit: int = 0) -> list[dict[str, Any]]:
    sel = pb["audience"].get("selector", "none")
    if sel in ("none", None):
        return []
    if sel == "lapsed_regulars":
        rows = repo.lapsed_customers(merchant_id)
    elif sel == "active_regulars":
        cutoff = iso(demo_now() - timedelta(days=settings.lapsed_gap_days))
        rows = [dict(r) for r in q(
            "SELECT s.customer_tok, s.visits_lifetime, s.last_visit_at, s.avg_ticket_paise,"
            " s.ltv_paise, s.churn_score, s.rfm_decile FROM customer_merchant_stats s "
            "WHERE s.merchant_id = ? AND s.visits_lifetime >= ? AND s.last_visit_at >= ? "
            "ORDER BY s.rfm_decile DESC, s.ltv_paise DESC",
            (merchant_id, settings.regular_min_visits, cutoff))]
    elif sel == "all_regulars":
        rows = [dict(r) for r in q(
            "SELECT customer_tok, visits_lifetime, last_visit_at, avg_ticket_paise, ltv_paise,"
            " churn_score, rfm_decile FROM customer_merchant_stats "
            "WHERE merchant_id = ? AND visits_lifetime >= ? ORDER BY rfm_decile DESC, ltv_paise DESC",
            (merchant_id, settings.regular_min_visits))]
    else:
        raise ValueError(f"unknown audience selector {sel!r}")
    cap = int(pb["audience"].get("max_recipients", 0) or limit or 0)
    return rows[:cap] if cap else rows


# ---------------------------------------------------------------- diagnose -> prescribe

def prescribe_from_signal(signal: dict[str, Any], *, trace_id: str | None = None,
                          merchant_id: int | None = None) -> dict[str, Any] | None:
    """Map one signal to its playbook and produce the awaiting_approval run.

    Idempotent per (merchant, playbook, signal): re-scanning does not stack duplicate runs.
    """
    trace = trace_id or audit.new_trace_id()
    mid = merchant_id or signal.get("merchant_id") or settings.demo_merchant_id
    # Accept either a live detector payload (dict with kind/title) or a DB signal row.
    payload = signal if "title" in signal or "kind" in signal else jload(signal.get("payload")) or {}
    kind = payload.get("kind")
    if kind is None:
        return None

    cands = [pb for pb in playbooks().values()
             if kind in pb["trigger"]["signal_kinds"]
             and float(payload.get("severity", 0)) >= float(pb["trigger"]["min_severity"])]
    if not cands:
        return None
    pb = cands[0]                          # triggers are disjoint by design
    pb_id = pb["id"]

    open_row = q1(
        "SELECT run_id FROM playbook_runs WHERE merchant_id = ? AND playbook = ? "
        "AND signal_id = ? AND state NOT IN ('closed','dismissed')",
        (mid, pb_id, signal.get("signal_id")),
    )
    if open_row:
        return repo.run(open_row["run_id"])

    aud = _audience(pb, mid)
    n = len(aud)
    if pb["impact_model"] == "heuristic_winback_v1":
        impact = estimate_winback(n, min_roi=float(pb["guardrails"].get("min_roi", 3.0)))
    else:
        per_head = sum(a["avg_ticket_paise"] for a in aud) // n if n else 0
        impact = estimate_simple(n, per_head_paise=per_head or 30_000,
                                 uplift_band=(0.02, 0.06))
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    now = demo_now()
    m = repo.merchant(mid) or {}
    copy_key = pb.get("copy_key", "")
    prescription = {
        "run_id": run_id,
        "playbook": pb_id,
        "playbook_version": pb["version"],
        "headline": payload.get("title"),
        "signal": {"kind": kind, "severity": payload.get("severity"),
                   "signal_id": signal.get("signal_id"),
                   "evidence": payload.get("evidence", {})},
        "goal": pb["goal"],
        "audience_size": n,
        "audience_selector": pb["audience"].get("selector"),
        "offer": pb.get("offer", {}),
        "channel": pb.get("channel", "whatsapp"),
        "lang": pb.get("lang", "hi"),
        "copy_key": copy_key,
        "copy_template": COPY.get(copy_key, ""),
        "impact": impact,
        "guardrails": {
            "min_roi": pb["guardrails"].get("min_roi", 3.0),
            "cost_cap_paise": pb["guardrails"].get("cost_cap_paise", settings.policy.spend_cap_paise_per_run),
            "frequency_cap_per_week": pb["guardrails"].get("frequency_cap_per_week", 2),
            "audience_ceiling": pb["guardrails"].get("audience_ceiling", 1000),
            "quiet_hours": [str(t) for t in pb["guardrails"].get("quiet_hours", ["21:30", "08:00"])],
        },
        "measure": pb.get("measure", {}),
        "meets_min_roi": impact.get("meets_min_roi", True),
        "created_at": iso(now),
    }
    with tx() as conn:
        conn.execute(
            "INSERT INTO playbook_runs(run_id, merchant_id, playbook, signal_id, state,"
            " prescription, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, mid, pb_id, signal.get("signal_id"), "awaiting_approval",
             jdump(prescription), iso(now), iso(now)),
        )
    audit.log("action", {"action": "playbook.prescribed", "run_id": run_id, "playbook": pb_id,
                         "audience_size": n, "signal": kind,
                         "gmv_band_paise": impact.get("gmv_influenced_band_paise"),
                         "cost_cap_paise": impact.get("cost_cap_paise")},
              trace_id=trace, merchant_id=mid, run_id=run_id)
    # The MKG learns that a prescription was proposed, before anyone approves it.
    memory.append_episode(mid, occurred_on=iso(now)[:10],
                          event=f"prescribed {pb_id} for {kind}",
                          result={"audience_size": n,
                                  "cost_cap_paise": impact.get("cost_cap_paise"),
                                  "state": "awaiting_approval"},
                          run_id=run_id)
    return repo.run(run_id)


def prescribe_all(merchant_id: int, trace_id: str | None = None) -> list[dict[str, Any]]:
    """Every open signal -> its playbook, in severity order. The dashboard's 'nudge me' path."""
    trace = trace_id or audit.new_trace_id()
    out = []
    for sig in sorted(repo.open_signals(merchant_id, 20), key=lambda s: -s["severity"]):
        row = prescribe_from_signal(sig, trace_id=trace, merchant_id=merchant_id)
        if row:
            out.append(row)
    return out


# ---------------------------------------------------------------- approval transitions

def approve(run_id: str, actor: str, token: str, reason: str | None = None) -> dict[str, Any]:
    """Record the merchant's yes. Signed token + state check make replay impossible."""
    row = repo.run(run_id)
    if not row:
        raise ValueError(f"unknown run {run_id}")
    if not approvals.verify(run_id, token, "awaiting_approval"):
        raise PermissionError("approval token invalid or stale")
    if row["state"] != "awaiting_approval":
        raise ValueError(f"run is {row['state']}, not awaiting_approval")
    now = demo_now()
    with tx() as conn:
        conn.execute(
            "UPDATE playbook_runs SET state='executing', approved_by=?, approved_at=?,"
            " updated_at=? WHERE run_id=? AND state='awaiting_approval'",
            (actor, iso(now), iso(now), run_id),
        )
    audit.log("approval", {"run_id": run_id, "decision": "approved", "actor": actor,
                          "reason": reason, "prescription": row["prescription"]},
              trace_id=None, merchant_id=row["merchant_id"], run_id=run_id)
    return repo.run(run_id)


def reject(run_id: str, actor: str, reason: str | None = None) -> dict[str, Any]:
    row = repo.run(run_id)
    if not row:
        raise ValueError(f"unknown run {run_id}")
    if row["state"] not in ("awaiting_approval", "prescribed", "detected", "diagnosed"):
        raise ValueError(f"run is {row['state']}; too late to reject")
    with tx() as conn:
        conn.execute(
            "UPDATE playbook_runs SET state='dismissed', dismissed_reason=?, approved_by=?,"
            " updated_at=? WHERE run_id=?",
            (reason or "merchant dismissed", actor, iso(demo_now()), run_id),
        )
    audit.log("approval", {"run_id": run_id, "decision": "rejected", "actor": actor,
                          "reason": reason}, trace_id=None,
              merchant_id=row["merchant_id"], run_id=run_id)
    memory.learn(row["merchant_id"], f"dismissed_{row['playbook']}",
                 {"reason": reason, "at": iso(demo_now())})
    return repo.run(run_id)


def set_state(run_id: str, state: str, trace_id: str | None = None, **extra: Any) -> None:
    if state not in STATES:
        raise ValueError(f"illegal state {state}")
    with tx() as conn:
        conn.execute("UPDATE playbook_runs SET state=?, updated_at=? WHERE run_id=?",
                     (state, iso(demo_now()), run_id))
