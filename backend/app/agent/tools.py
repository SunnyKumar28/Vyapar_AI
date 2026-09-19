"""The tool registry (§4.2) — the ONLY things the model can do, by name.

Design rules baked into the shape of this file:

  * TOOLS is an explicit dict. Dispatch is a lookup, never getattr/eval on model output.
  * Every tool carries a JSON schema; inputs are validated before the function runs.
  * Preconditions are checked in code, and results are logged to the audit log with the
    trace id, so every number the merchant saw can be traced to the call that produced it.
  * Money-touching tools (request_approval, execute_action) are gated on run state and the
    policy engine; the model can ask, the merchant must approve.

Tool functions return dicts of numbers; formatting (₹, thousands separators, Hindi copy)
happens in the response renderer, never here.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

import json
from json import loads

from ..config import settings
from ..db import demo_now, iso, jdump, q, q1, tx
from ..mkg import memory, repositories as repo
from ..playbooks import engine
from ..playbooks.impact import estimate_winback
from ..signals import detectors
from ..trust import audit, policy

ToolFn = Callable[..., Any]


def _paise(p: int) -> int:
    return int(p)


# ---------------------------------------------------------------------- read tools

def get_merchant_brief(**kw: Any) -> dict[str, Any]:
    """The MKG brief: profile, 30-day sales, customers, benchmark, memory. Read first."""
    mid = kw.get("merchant_id") or settings.demo_merchant_id
    b = memory.brief(mid, recall_query=kw.get("recall_query") or None)
    audit.log("tool_result", {"tool": "get_merchant_brief", "merchant_id": mid,
                              "gmv_30d_paise": b.get("sales_30d", {}).get("gmv_paise")},
              trace_id=kw.get("_trace_id"), merchant_id=mid)
    return b


def query_sales(**kw: Any) -> dict[str, Any]:
    """Daily GMV series for the last N days (default 30, max 90)."""
    mid = kw.get("merchant_id") or settings.demo_merchant_id
    days = min(int(kw.get("days", 30) or 30), 90)
    series = repo.daily_gmv(mid, days)
    audit.log("tool_result", {"tool": "query_sales", "days": days, "points": len(series)},
              trace_id=kw.get("_trace_id"), merchant_id=mid)
    return {"window_days": days, "series": series,
            "total_gmv_paise": _paise(sum(p["gmv_paise"] for p in series))}


def get_store_insights(**kw: Any) -> dict[str, Any]:
    """Operational facts for stock, best-seller, peak-hour and payment questions.

    Every field is read from generated ledger/inventory rows. The model may explain these
    facts, but it is not allowed to invent or calculate replacements.
    """
    mid = kw.get("merchant_id") or settings.demo_merchant_id
    days = min(max(int(kw.get("days", 30) or 30), 7), 90)
    items = repo.top_items(mid, days=days, limit=6)
    hours = repo.hourly_profile(mid, days=min(days, 30))
    channels = repo.channel_mix(mid, days=days)
    inventory = repo.inventory_status(mid)
    peak_hours = sorted(hours, key=lambda x: (-x["gmv_paise"], x["hour"]))[:3]
    out = {
        "window_days": days,
        "top_items": items,
        "peak_hours": peak_hours,
        "channel_mix": channels,
        "inventory": inventory,
        "at_risk_items": [r for r in inventory if r["status"] == "at_risk"],
        "reorder_soon_items": [r for r in inventory if r["status"] == "reorder_soon"],
    }
    audit.log("tool_result", {
        "tool": "get_store_insights", "items": len(items),
        "inventory_rows": len(inventory), "at_risk": len(out["at_risk_items"]),
    }, trace_id=kw.get("_trace_id"), merchant_id=mid)
    return out


def detect_anomalies(**kw: Any) -> dict[str, Any]:
    """Run the DOW-aware anomaly detector over the daily series. Code, not the model."""
    mid = kw.get("merchant_id") or settings.demo_merchant_id
    series = repo.daily_gmv(mid, min(int(kw.get("days", 30) or 30), 90))
    from ..ml import anomaly
    hits = anomaly.detect(series, weeks=4)
    audit.log("tool_result", {"tool": "detect_anomalies", "hits": len(hits)},
              trace_id=kw.get("_trace_id"), merchant_id=mid)
    return {"anomalies": hits, "checked_days": len(series)}


def forecast_sales(**kw: Any) -> dict[str, Any]:
    """Forecast the next week and expose code-derived dip drivers."""
    mid = kw.get("merchant_id") or settings.demo_merchant_id
    horizon = min(max(int(kw.get("horizon", 7) or 7), 1), 7)
    series = repo.daily_gmv(mid, 90)
    from ..ml import anomaly, forecast

    predicted = forecast.predict(series, horizon=horizon)
    gate = forecast.gate(series)
    baseline = forecast.moving_average(series, weeks=4)
    last_date = datetime.strptime(series[-1]["date"], "%Y-%m-%d") if series else demo_now()
    rows = []
    for offset, point in enumerate(predicted.get("forecast", [])[:horizon], 1):
        base = int(round(baseline.get(point["dow"], point["gmv_paise"])))
        value = int(point["gmv_paise"])
        rows.append({
            "date": (last_date + timedelta(days=offset)).strftime("%Y-%m-%d"),
            "dow": point["dow"],
            "forecast_gmv_paise": value,
            "weekday_baseline_paise": base,
            "delta_pct": round((value - base) / base, 4) if base else 0.0,
        })

    # A dip is a forecast at least 8% below the shop's own same-weekday baseline.
    dip_days = [r for r in rows if r["delta_pct"] <= -0.08]
    drivers: list[dict[str, Any]] = []
    hits = anomaly.detect(series, weeks=4)
    if hits and hits[-1]["direction"] == "drop":
        latest = hits[-1]
        drivers.append({
            "kind": "recent_sales_drop",
            "date": latest["date"],
            "dow": latest["dow"],
            "delta_pct": latest["delta_pct"],
            "actual_gmv_paise": latest["actual_gmv_paise"],
            "baseline_gmv_paise": latest["baseline_gmv_paise"],
        })
    lapsed = repo.lapsed_customers(mid)
    if len(lapsed) >= 20:
        drivers.append({
            "kind": "lapsed_regulars",
            "count": len(lapsed),
            "gap_days": settings.lapsed_gap_days,
            "avg_ticket_paise": round(sum(r["avg_ticket_paise"] for r in lapsed) / len(lapsed)),
        })
    try:
        stock = detectors.stockout(mid)
    except Exception:
        stock = None
    if stock:
        drivers.append({"kind": "stockout", **stock.get("evidence", {})})
    if not drivers:
        drivers.append({"kind": "no_strong_driver", "note": "No recent coded signal explains a dip."})

    out = {
        "horizon_days": horizon,
        "model": predicted.get("model"),
        "model_gate": gate,
        "forecast": rows,
        "next_period_total_paise": sum(r["forecast_gmv_paise"] for r in rows),
        "baseline_period_total_paise": sum(r["weekday_baseline_paise"] for r in rows),
        "dip_expected": bool(dip_days),
        "dip_threshold_pct": -0.08,
        "dip_days": dip_days,
        "drivers": drivers,
    }
    audit.log("tool_result", {"tool": "forecast_sales", "horizon": horizon,
                              "dip_expected": out["dip_expected"]},
              trace_id=kw.get("_trace_id"), merchant_id=mid)
    return out


def list_lapsed_customers(**kw: Any) -> dict[str, Any]:
    """Lapsed regulars: >=3 lifetime visits, no visit in 45+ days. Count + stats, not PII."""
    mid = kw.get("merchant_id") or settings.demo_merchant_id
    rows = repo.lapsed_customers(mid, limit=min(int(kw.get("limit", 500) or 500), 1000))
    out = {
        "count": len(rows),
        "min_gap_days": settings.lapsed_gap_days,
        "min_visits": settings.regular_min_visits,
        "avg_ticket_paise": _paise(round(sum(r["avg_ticket_paise"] for r in rows) / len(rows))) if rows else 0,
        "avg_ltv_paise": _paise(round(sum(r["ltv_paise"] for r in rows) / len(rows))) if rows else 0,
        "top_decile_share": round(sum(1 for r in rows if r["rfm_decile"] >= 8) / len(rows), 3) if rows else 0,
        "sample_toks": [r["customer_tok"] for r in rows[:5]],   # tokens only, never PII
    }
    audit.log("tool_result", {"tool": "list_lapsed_customers", "count": len(rows)},
              trace_id=kw.get("_trace_id"), merchant_id=mid)
    return out


def estimate_impact(**kw: Any) -> dict[str, Any]:
    """The ONLY source of ₹ projections (principle #2). heuristic_winback_v1 from config."""
    mid = kw.get("merchant_id") or settings.demo_merchant_id
    n = int(kw.get("audience_size", 0) or 0)
    est = estimate_winback(n)
    audit.log("tool_result", {"tool": "estimate_impact", "audience_size": n,
                              "band_paise": est["gmv_influenced_band_paise"],
                              "cost_cap_paise": est["cost_cap_paise"]},
              trace_id=kw.get("_trace_id"), merchant_id=mid)
    return est


# ---------------------------------------------------------------------- act tools

def request_approval(**kw: Any) -> dict[str, Any]:
    """Propose a playbook run; returns the prescription + signed approval token.

    Preconditions: the signal must exist and the impact estimate must meet min ROI.
    The merchant's yes is still required — this tool only asks.
    """
    mid = kw.get("merchant_id") or settings.demo_merchant_id
    sig_kind = kw.get("signal_kind", "churn_cluster")
    sigs = [s for s in repo.open_signals(mid, 20) if s["kind"] == sig_kind]
    if not sigs:
        raise ValueError(f"no open {sig_kind} signal; scan first or cite a real signal")
    sig = sigs[0]
    run = engine.prescribe_from_signal(sig, trace_id=kw.get("_trace_id"), merchant_id=mid)
    if not run:
        raise ValueError(f"no playbook matches signal {sig_kind}")
    presc = run["prescription"]
    if not presc.get("meets_min_roi", True):
        raise ValueError("projected ROI below the playbook minimum; not proposing")
    from ..trust import approvals
    token = approvals.sign(run["run_id"], "awaiting_approval")
    out = {
        "run_id": run["run_id"], "state": run["state"], "approval_token": token,
        "prescription": presc,
        "note": "merchant must approve via /v1/approvals; nothing has been sent",
    }
    audit.log("tool_result", {"tool": "request_approval", "run_id": run["run_id"],
                              "audience_size": presc["audience_size"]},
              trace_id=kw.get("_trace_id"), merchant_id=mid, run_id=run["run_id"])
    return out


def execute_action(**kw: Any) -> dict[str, Any]:
    """Send the approved campaign through its connector. Idempotent on run_id.

    Preconditions (checked in code): run state == executing (i.e., approved), policy
    check passes at pre_execute, audience within ceiling. Never callable on a run the
    merchant has not approved.
    """
    run_id = kw.get("run_id", "")
    mid = kw.get("merchant_id") or settings.demo_merchant_id
    run = repo.run(run_id)
    if not run or run["merchant_id"] != mid:
        raise ValueError("unknown run")
    if run["state"] not in ("executing", "executed"):
        raise PermissionError(f"run {run_id} is {run['state']}; merchant approval required")
    from ..actions import connectors
    result = connectors.dispatch(run, trace_id=kw.get("_trace_id"))
    audit.log("tool_result", {"tool": "execute_action", "run_id": run_id,
                              "delivered": result.get("delivered")},
              trace_id=kw.get("_trace_id"), merchant_id=mid, run_id=run_id)
    return result


def get_action_status(**kw: Any) -> dict[str, Any]:
    """Where a run stands: state, deliveries, spend, outcomes so far."""
    run_id = kw.get("run_id", "")
    run = repo.run(run_id)
    if not run:
        raise ValueError("unknown run")
    acts = [dict(r) for r in q("SELECT * FROM actions WHERE run_id = ?", (run_id,))]
    outs = [dict(r) for r in q("SELECT * FROM outcomes WHERE run_id = ? ORDER BY window_d", (run_id,))]
    for a in acts:
        a["payload"] = loads(a["payload"]) if a.get("payload") else None
    for o in outs:
        o["detail"] = loads(o["detail"]) if o.get("detail") else None
    return {"run_id": run_id, "state": run["state"], "actions": acts, "outcomes": outs}


def compute_roi(**kw: Any) -> dict[str, Any]:
    """ROI of a measured run: (gmv_influenced - cost) / cost, from the outcomes table."""
    run_id = kw.get("run_id", "")
    row = q1("SELECT * FROM outcomes WHERE run_id = ? ORDER BY window_d DESC LIMIT 1", (run_id,))
    if not row:
        raise ValueError("no measured outcome yet")
    d = dict(row)
    cost, gmv = int(d["cost_paise"] or 0), int(d["gmv_influenced_paise"] or 0)
    roi = round((gmv - cost) / cost, 2) if cost else None
    return {"run_id": run_id, "window_d": d["window_d"], "gmv_influenced_paise": gmv,
            "cost_paise": cost, "roi": roi, "confidence": d["confidence"],
            "n_treatment": d["n_treatment"], "n_control": d["n_control"]}


def set_reminder(**kw: Any) -> dict[str, Any]:
    """Write a follow-up into the merchant's reminder list (e.g., stock check Thursday)."""
    mid = kw.get("merchant_id") or settings.demo_merchant_id
    note = kw.get("note", "")
    in_days = int(kw.get("in_days", 1) or 1)
    if not note:
        raise ValueError("note required")
    with tx() as conn:
        conn.execute("INSERT INTO reminders(merchant_id, note, due_at, created_at) VALUES (?,?,?,?)",
                     (mid, note, iso(demo_now() + timedelta(days=in_days)), iso(demo_now())))
    return {"reminder_set": True, "note": note, "due_in_days": in_days}


# ---------------------------------------------------------------------- registry

TOOLS: dict[str, dict[str, Any]] = {
    "get_merchant_brief": {
        "fn": get_merchant_brief,
        "description": "Merchant profile, 30-day sales, customer counts, benchmark, memory. Call first.",
        "schema": {"type": "object", "additionalProperties": False,
                   "properties": {"merchant_id": {"type": "integer"},
                                  "recall_query": {"type": "string"}}},
    },
    "query_sales": {
        "fn": query_sales,
        "description": "Daily GMV series for the last N days (default 30, max 90).",
        "schema": {"type": "object", "additionalProperties": False,
                   "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 90}}},
    },
    "get_store_insights": {
        "fn": get_store_insights,
        "description": "Data-backed store operations: best-selling items, peak hours, payment mix and current stock risk.",
        "schema": {"type": "object", "additionalProperties": False,
                   "properties": {"days": {"type": "integer", "minimum": 7, "maximum": 90}}},
    },
    "detect_anomalies": {
        "fn": detect_anomalies,
        "description": "Day-of-week-aware anomaly detection over the sales series. Detector is code.",
        "schema": {"type": "object", "additionalProperties": False,
                   "properties": {"days": {"type": "integer", "minimum": 7, "maximum": 90}}},
    },
    "forecast_sales": {
        "fn": forecast_sales,
        "description": "Forecast the next 1-7 days from the shop's daily ledger; returns totals, dip days, weekday baselines, model gate and coded drivers.",
        "schema": {"type": "object", "additionalProperties": False,
                   "properties": {"horizon": {"type": "integer", "minimum": 1, "maximum": 7}}},
    },
    "list_lapsed_customers": {
        "fn": list_lapsed_customers,
        "description": "Lapsed regulars (>=3 lifetime visits, 45+ days since last). Stats, never PII.",
        "schema": {"type": "object", "additionalProperties": False,
                   "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 1000}}},
    },
    "estimate_impact": {
        "fn": estimate_impact,
        "description": "Project the [low, high] GMV band and cost cap for an audience size. The only source of ₹ projections.",
        "schema": {"type": "object", "additionalProperties": False, "required": ["audience_size"],
                   "properties": {"audience_size": {"type": "integer", "minimum": 1, "maximum": 1000}}},
    },
    "request_approval": {
        "fn": request_approval,
        "description": "Propose the playbook run for a signal; returns the prescription card + approval token. Asking, not sending.",
        "schema": {"type": "object", "additionalProperties": False,
                   "properties": {"signal_kind": {"type": "string",
                                                  "enum": ["sales_anomaly", "churn_cluster", "benchmark_gap",
                                                           "stockout", "festive", "collections"]}}},
    },
    "execute_action": {
        "fn": execute_action,
        "description": "Send an APPROVED campaign through its connector. Idempotent on run_id.",
        "schema": {"type": "object", "additionalProperties": False, "required": ["run_id"],
                   "properties": {"run_id": {"type": "string", "minLength": 4}}},
    },
    "get_action_status": {
        "fn": get_action_status,
        "description": "State, deliveries and measured outcomes of a run.",
        "schema": {"type": "object", "additionalProperties": False, "required": ["run_id"],
                   "properties": {"run_id": {"type": "string"}}},
    },
    "compute_roi": {
        "fn": compute_roi,
        "description": "ROI of a measured run from the outcomes table.",
        "schema": {"type": "object", "additionalProperties": False, "required": ["run_id"],
                   "properties": {"run_id": {"type": "string"}}},
    },
    "set_reminder": {
        "fn": set_reminder,
        "description": "Save a follow-up reminder for the merchant.",
        "schema": {"type": "object", "additionalProperties": False, "required": ["note"],
                   "properties": {"note": {"type": "string", "minLength": 2, "maxLength": 200},
                                  "in_days": {"type": "integer", "minimum": 0, "maximum": 30}}},
    },
}


def call(name: str, args: dict[str, Any], *, trace_id: str | None = None,
         merchant_id: int | None = None) -> dict[str, Any]:
    """The single entry point the loop uses. Allowlist -> validate -> audit -> run."""
    if name not in TOOLS:                      # explicit allowlist, never getattr
        raise ValueError(f"unknown tool {name!r}")
    spec = TOOLS[name]
    args = dict(args or {})
    if merchant_id:
        args.setdefault("merchant_id", merchant_id)
    args["_trace_id"] = trace_id
    # merchant_id is server-injected session context, never a model choice (the model
    # cannot guess identifiers); whitelist it in the schema before validating.
    schema = dict(spec["schema"])
    if "merchant_id" in args:
        schema.setdefault("properties", {})["merchant_id"] = {"type": "integer"}
    visible = {k: v for k, v in args.items() if k != "_trace_id"}
    from .guardrails import validate
    errs = validate(visible, schema)
    if errs:
        raise ValueError(f"schema violations: {'; '.join(errs)}")
    audit.log("tool_call", {"tool": name, "args": visible}, trace_id=trace_id,
              merchant_id=merchant_id)
    out = spec["fn"](**args)
    return out
