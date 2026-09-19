"""Signal detectors — the SENSE edge of the loop (§5).

All deterministic code; the LLM is never in the detection path (principle #1). Each
detector returns None when it has nothing to say, or a payload of *evidence*: numbers the
agent reads verbatim. `scan` upserts them into `signals` and logs the run to the audit
log, so a judge can diff what fired against what the data says.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from ..config import settings
from ..db import connect, demo_now, iso, jdump, q1, tx
from ..mkg import repositories as repo
from ..ml import anomaly, churn, forecast
from ..trust import audit

Detector = Callable[[int], dict[str, Any] | None]


def sales_anomaly(merchant_id: int) -> dict[str, Any] | None:
    """Yesterday vs its own weekday, DOW-aware MAD z-score (§5 anomaly-svc)."""
    series = repo.daily_gmv(merchant_id, 30)
    if not series:
        return None
    hits = anomaly.detect(series, weeks=4)
    if not hits:
        return None
    last = hits[-1]
    return {
        "kind": "sales_anomaly", "severity": last["severity"],
        "title": f"Yesterday ({last['dow']}) sales {last['direction']} {abs(last['delta_pct']) * 100:.0f}%",
        "evidence": {
            "date": last["date"], "dow": last["dow"], "direction": last["direction"],
            "actual_gmv_paise": last["actual_gmv_paise"],
            "baseline_gmv_paise": last["baseline_gmv_paise"],
            "delta_paise": last["delta_paise"], "delta_pct": last["delta_pct"],
            "z_score": last["z_score"], "method": last["method"],
            "baseline_samples_paise": last["baseline_samples_paise"],
            "txns": last["txns"],
        },
    }


def churn_cluster(merchant_id: int) -> dict[str, Any] | None:
    """Lapsed regulars, scored by churn-svc behind its AUC gate (principle #5)."""
    lapsed = repo.lapsed_customers(merchant_id)
    if len(lapsed) < 20:
        return None
    labelled = [(c["churn_score"], 1 if c["days_since_visit"] >= settings.lapsed_gap_days else 0)
                for c in repo.lapsed_customers(merchant_id, min_gap_days=10)]
    gate = churn.gate(labelled)
    return {
        "kind": "churn_cluster", "severity": round(min(1.0, len(lapsed) / 300), 3),
        "title": f"{len(lapsed)} regulars haven't been back in {settings.lapsed_gap_days}+ days",
        "evidence": {
            "lapsed_regulars": len(lapsed),
            "gap_days": settings.lapsed_gap_days,
            "churn_model": gate,
            "avg_ticket_paise": round(sum(c["avg_ticket_paise"] for c in lapsed) / len(lapsed)),
            "avg_ltv_paise": round(sum(c["ltv_paise"] for c in lapsed) / len(lapsed)),
            "top_decile_share": round(
                sum(1 for c in lapsed if c["rfm_decile"] >= 8) / len(lapsed), 3),
        },
    }


def benchmark_gap(merchant_id: int) -> dict[str, Any] | None:
    """Your repeat rate vs anonymised peers in the same vertical x pincode (§5, k>=20)."""
    m = repo.merchant(merchant_id)
    if not m:
        return None
    bench = repo.benchmark(m["vertical"], m["pincode"])
    if not bench or bench.get("suppressed"):
        return None
    mine = repo.repeat_rate(merchant_id, 30)["repeat_rate"]
    gap = mine - bench["repeat_rate"]
    if abs(gap) < 0.03:
        return None
    return {
        "kind": "benchmark_gap", "severity": round(min(1.0, abs(gap) * 4), 3),
        "title": (f"Repeat rate {gap * 100:+.0f}pp vs {bench['cohort_size']} peer "
                  f"{m['vertical']}s nearby"),
        "evidence": {
            "my_repeat_rate_30d": mine, "peer_repeat_rate": bench["repeat_rate"],
            "gap_pp": round(gap * 100, 1), "peer_cohort_size": bench["cohort_size"],
            "peer_definition": f"anonymised {m['vertical']} x {m['pincode']}, k>=20",
        },
    }


def stockout(merchant_id: int) -> dict[str, Any] | None:
    """A previously steady item simply disappears from the ledger — proxy for stockout."""
    items = repo.top_items(merchant_id, 30, limit=8)
    if len(items) < 3:
        return None
    rows = connect().execute(
        "SELECT item, COUNT(*) AS n, SUM(amount_paise) AS gmv FROM transactions "
        "WHERE merchant_id = ? AND item IS NOT NULL AND event_time >= ? AND event_time < ? "
        "GROUP BY item ORDER BY gmv DESC",
        (merchant_id, iso(demo_now() - timedelta(days=30)), iso(demo_now() - timedelta(days=7))),
    ).fetchall()
    prior = {r["item"]: int(r["gmv"]) for r in rows}
    for it in items:
        if prior.get(it["item"], 0) and it["gmv_paise"] < 0.3 * prior[it["item"]]:
            return {
                "kind": "stockout", "severity": 0.5,
                "title": f"'{it['item']}' sales fell sharply in the last week",
                "evidence": {
                    "item": it["item"], "gmv_last_7d_paise": it["gmv_paise"],
                    "gmv_prior_23d_paise": prior[it["item"]],
                    "share_of_drop": round(1 - it["gmv_paise"] / prior[it["item"]], 3),
                },
            }
    return None


def festive(merchant_id: int) -> dict[str, Any] | None:
    """Calendar-driven: demand ramps in the N days before a big shopping window (§6)."""
    m = repo.merchant(merchant_id)
    if not m:
        return None
    now = demo_now()
    upcoming = [   # 2026 dates: Shardiya Navratri opens Oct 11, Diwali falls Nov 8
        {"name": "Navratri", "on": "2026-10-11"},
        {"name": "Diwali", "on": "2026-11-08"},
    ]
    for ev in upcoming:
        days_to = (datetime.fromisoformat(ev["on"] + "T00:00:00+05:30").replace(tzinfo=None)
                   - (now + timedelta(hours=5, minutes=30)).replace(tzinfo=None)).days
        if 10 <= days_to <= 28:
            series = repo.daily_gmv(merchant_id, 60)
            fc = forecast.predict(series, horizon=7)
            fc_gate = forecast.gate(series)
            return {
                "kind": "festive", "severity": 0.45,
                "title": f"{ev['name']} in {days_to} days — plan stock and staff now",
                "evidence": {
                    "event": ev["name"], "days_to_event": days_to,
                    "next_week_gmv_paise": fc.get("next_week_gmv_paise"),
                    "forecast_model": fc.get("model"), "forecast_gate": fc_gate,
                    "weekly_gmv_paise": fc.get("forecast"),
                },
            }
    return None


def collections(merchant_id: int) -> dict[str, Any] | None:
    """Udhaar: credit extended at the counter that hasn't come back (§6)."""
    row = q1("SELECT COUNT(*) AS n, MIN(due_at) AS oldest FROM reminders "
             "WHERE merchant_id = ? AND due_at < ?", (merchant_id, iso(demo_now())))
    if not row or not int(row["n"]):
        return None
    return {
        "kind": "collections", "severity": 0.5,
        "title": f"{int(row['n'])} udhaar follow-ups are past due",
        "evidence": {"past_due": int(row["n"]), "oldest_due_at": row["oldest"],
                      "note": "amounts stay in the merchant's bahi khata; we track follow-ups"},
    }


DETECTORS: dict[str, Detector] = {
    "sales_anomaly": sales_anomaly,
    "churn_cluster": churn_cluster,
    "benchmark_gap": benchmark_gap,
    "stockout": stockout,
    "festive": festive,
    "collections": collections,
}


def scan(merchant_id: int | None = None, trace_id: str | None = None) -> list[dict[str, Any]]:
    """POST /v1/jobs/signals/scan. Runs every detector, upserts, audits. Idempotent per day."""
    trace = trace_id or audit.new_trace_id()
    mids = [merchant_id] if merchant_id else [m["merchant_id"] for m in repo.all_merchants(500)]
    now = demo_now()
    fired: list[dict[str, Any]] = []
    with tx() as conn:
        for mid in mids:
            for name, fn in DETECTORS.items():
                try:
                    sig = fn(mid)
                except Exception as exc:  # a broken detector must never break the scan
                    audit.log("job", {"job": "signals.scan", "detector": name,
                                      "merchant_id": mid, "error": repr(exc)}, trace_id=trace)
                    continue
                if not sig:
                    continue
                payload = {**sig, "detected_at": iso(now), "merchant_id": mid}
                conn.execute(
                    "INSERT INTO signals(merchant_id, kind, severity, payload, detected_at) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(merchant_id, kind, detected_at) "
                    "DO UPDATE SET severity=excluded.severity, payload=excluded.payload",
                    (mid, sig["kind"], sig["severity"], jdump(payload), iso(now)),
                )
                fired.append(payload)
                audit.log("job", {"job": "signals.scan", "signal": sig["kind"],
                                  "merchant_id": mid, "title": sig["title"]},
                          trace_id=trace, merchant_id=mid)
    return fired
