"""Read/write access to the Merchant Knowledge Graph (§3).

Entities are tables; the merchant-level summary and episodic memory are JSON documents.
Every function here returns plain dicts with paise integers — the agent's tools wrap these,
and the UI renders the numbers verbatim. No formatting decisions happen in the LLM.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..config import settings
from ..db import connect, demo_now, iso, jload, parse, q, q1


# ---------------------------------------------------------------- merchants

def merchant(merchant_id: int) -> dict[str, Any] | None:
    r = q1("SELECT * FROM merchants WHERE merchant_id = ?", (merchant_id,))
    return dict(r) if r else None


def all_merchants(limit: int = 500) -> list[dict[str, Any]]:
    return [dict(r) for r in q(
        "SELECT merchant_id, name, vertical, city, pincode, lang FROM merchants ORDER BY merchant_id LIMIT ?",
        (limit,),
    )]


# ---------------------------------------------------------------- sales series

IST_OFFSET = timedelta(hours=5, minutes=30)


def ist_today_start(now: datetime | None = None) -> datetime:
    """UTC instant of 00:00 IST on the clock's IST date."""
    ist = (now or demo_now()) + IST_OFFSET
    return ist.replace(hour=0, minute=0, second=0, microsecond=0) - IST_OFFSET


def window(days: int, now: datetime | None = None) -> tuple[datetime, datetime]:
    """The last `days` COMPLETE IST business days, ending at 00:00 IST today.

    Every "last 30 days" aggregate in the system goes through here, so the windows the
    detectors, the brief and the report card use are byte-identical.
    """
    end = ist_today_start(now)
    return end - timedelta(days=days), end


def daily_gmv(merchant_id: int, days: int = 90, until: datetime | None = None) -> list[dict[str, Any]]:
    """Daily GMV series over the last `days` complete IST days."""
    start, end = window(days, until)
    rows = q(
        "SELECT date(datetime(event_time, '+330 minutes')) AS d, "
        "       SUM(amount_paise) AS gmv, COUNT(*) AS txns, "
        "       COUNT(DISTINCT customer_tok) AS customers "
        "FROM transactions WHERE merchant_id = ? AND event_time >= ? AND event_time < ? "
        "GROUP BY d ORDER BY d",
        (merchant_id, iso(start), iso(end)),
    )
    return [
        {"date": r["d"], "gmv_paise": int(r["gmv"]), "txns": int(r["txns"]),
         "customers": int(r["customers"]),
         "dow": datetime.strptime(r["d"], "%Y-%m-%d").strftime("%a")}
        for r in rows
    ]


def gmv_between(merchant_id: int, start: datetime, end: datetime) -> int:
    r = q1(
        "SELECT COALESCE(SUM(amount_paise), 0) AS gmv FROM transactions "
        "WHERE merchant_id = ? AND event_time >= ? AND event_time < ?",
        (merchant_id, iso(start), iso(end)),
    )
    return int(r["gmv"]) if r else 0


def cohort_gmv(merchant_id: int, toks: list[str], start: datetime, end: datetime) -> int:
    """GMV from a specific customer set — the primitive under the diff-in-diff (§8)."""
    if not toks:
        return 0
    total = 0
    for i in range(0, len(toks), 400):          # keep SQLite's parameter count sane
        chunk = toks[i:i + 400]
        marks = ",".join("?" * len(chunk))
        r = q1(
            f"SELECT COALESCE(SUM(amount_paise),0) AS gmv FROM transactions "
            f"WHERE merchant_id = ? AND event_time >= ? AND event_time < ? AND customer_tok IN ({marks})",
            (merchant_id, iso(start), iso(end), *chunk),
        )
        total += int(r["gmv"]) if r else 0
    return total


def top_items(merchant_id: int, days: int = 30, limit: int = 5) -> list[dict[str, Any]]:
    start, _ = window(days)
    rows = q(
        "SELECT item, COUNT(*) AS n, SUM(amount_paise) AS gmv FROM transactions "
        "WHERE merchant_id = ? AND item IS NOT NULL AND event_time >= ? "
        "GROUP BY item ORDER BY n DESC LIMIT ?",
        (merchant_id, iso(start), limit),
    )
    return [{"item": r["item"], "txns": int(r["n"]), "gmv_paise": int(r["gmv"])} for r in rows]


def hourly_profile(merchant_id: int, days: int = 14) -> list[dict[str, Any]]:
    start, _ = window(days)
    rows = q(
        "SELECT CAST(strftime('%H', datetime(event_time, '+330 minutes')) AS INTEGER) AS hr, "
        "       SUM(amount_paise) AS gmv, COUNT(*) AS txns FROM transactions "
        "WHERE merchant_id = ? AND event_time >= ? GROUP BY hr ORDER BY hr",
        (merchant_id, iso(start)),
    )
    return [{"hour": int(r["hr"]), "gmv_paise": int(r["gmv"]), "txns": int(r["txns"])} for r in rows]


def channel_mix(merchant_id: int, days: int = 30) -> list[dict[str, Any]]:
    """Payment-channel mix from the transaction ledger, not a fixed demo constant."""
    start, _ = window(days)
    rows = q(
        "SELECT channel, COUNT(*) AS txns, SUM(amount_paise) AS gmv FROM transactions "
        "WHERE merchant_id = ? AND event_time >= ? GROUP BY channel ORDER BY gmv DESC",
        (merchant_id, iso(start)),
    )
    total = sum(int(r["gmv"] or 0) for r in rows)
    return [{
        "channel": r["channel"], "txns": int(r["txns"]), "gmv_paise": int(r["gmv"] or 0),
        "share": round(int(r["gmv"] or 0) / total, 4) if total else 0.0,
    } for r in rows]


def inventory_status(merchant_id: int) -> list[dict[str, Any]]:
    """Latest generated inventory snapshot, ordered by operational risk."""
    rows = q(
        "SELECT item, units_sold_7d, units_on_hand, reorder_point_units, unit_cost_paise, "
        "supplier, restock_days, status, as_of FROM inventory_snapshots "
        "WHERE merchant_id = ? AND as_of = (SELECT MAX(as_of) FROM inventory_snapshots "
        "WHERE merchant_id = ?) ORDER BY CASE status WHEN 'at_risk' THEN 0 "
        "WHEN 'reorder_soon' THEN 1 ELSE 2 END, item",
        (merchant_id, merchant_id),
    )
    return [{
        "item": r["item"], "units_sold_7d": int(r["units_sold_7d"]),
        "units_on_hand": int(r["units_on_hand"]),
        "reorder_point_units": int(r["reorder_point_units"]),
        "unit_cost_paise": int(r["unit_cost_paise"]), "supplier": r["supplier"],
        "restock_days": int(r["restock_days"]), "status": r["status"], "as_of": r["as_of"],
    } for r in rows]


# ---------------------------------------------------------------- customers / RFM

def repeat_rate(merchant_id: int, days: int = 30, until: datetime | None = None) -> dict[str, Any]:
    """Share of identified customers in the window who transacted at least twice."""
    start, end = window(days, until)
    rows = q(
        "SELECT customer_tok, COUNT(*) AS n FROM transactions "
        "WHERE merchant_id = ? AND event_time >= ? AND event_time < ? GROUP BY customer_tok",
        (merchant_id, iso(start), iso(end)),
    )
    distinct = len(rows)
    repeaters = sum(1 for r in rows if int(r["n"]) >= 2)
    return {
        "window_days": days,
        "distinct_customers": distinct,
        "repeat_customers": repeaters,
        "repeat_rate": round(repeaters / distinct, 4) if distinct else 0.0,
    }


def cohort_repeat_rate(merchant_id: int, toks: list[str], start: datetime, end: datetime) -> dict[str, Any]:
    """Repeat rate *within a named cohort* — used for the treated-cohort figure on the report card."""
    if not toks:
        return {"cohort_size": 0, "repeat_customers": 0, "repeat_rate": 0.0}
    counts: dict[str, int] = {}
    for i in range(0, len(toks), 400):
        chunk = toks[i:i + 400]
        marks = ",".join("?" * len(chunk))
        for r in q(
            f"SELECT customer_tok, COUNT(*) AS n FROM transactions WHERE merchant_id = ? "
            f"AND event_time >= ? AND event_time < ? AND customer_tok IN ({marks}) GROUP BY customer_tok",
            (merchant_id, iso(start), iso(end), *chunk),
        ):
            counts[r["customer_tok"]] = int(r["n"])
    repeaters = sum(1 for n in counts.values() if n >= 2)
    return {
        "cohort_size": len(toks),
        "returned": len(counts),
        "repeat_customers": repeaters,
        "repeat_rate": round(repeaters / len(toks), 4),
    }


def lapsed_customers(
    merchant_id: int,
    min_gap_days: int | None = None,
    min_visits: int | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Lapsed *regulars*: >= min_visits lifetime visits, last seen >= min_gap_days ago.

    The min_visits filter is what separates a regular from a one-time walk-in — without it
    every passer-by counts as 'lapsed' and the audience is meaningless.
    """
    gap = settings.lapsed_gap_days if min_gap_days is None else min_gap_days
    visits = settings.regular_min_visits if min_visits is None else min_visits
    cutoff = iso(demo_now() - timedelta(days=gap))
    sql = (
        "SELECT customer_tok, visits_lifetime, last_visit_at, avg_ticket_paise, ltv_paise, "
        "       churn_score, rfm_decile FROM customer_merchant_stats "
        "WHERE merchant_id = ? AND visits_lifetime >= ? AND last_visit_at < ? "
        "ORDER BY ltv_paise DESC"
    )
    params: list[Any] = [merchant_id, visits, cutoff]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    out = []
    for r in q(sql, params):
        gap_days = (demo_now() - parse(r["last_visit_at"])).days
        out.append({
            "customer_tok": r["customer_tok"],
            "visits_lifetime": int(r["visits_lifetime"]),
            "last_visit_at": r["last_visit_at"],
            "days_since_visit": gap_days,
            "avg_ticket_paise": int(r["avg_ticket_paise"]),
            "ltv_paise": int(r["ltv_paise"]),
            "churn_score": round(float(r["churn_score"]), 3),
            "rfm_decile": int(r["rfm_decile"]),
        })
    return out


def matched_controls(
    merchant_id: int, treatment: list[dict[str, Any]], lo: int = 31, hi: int = 44
) -> dict[str, Any]:
    """Control arm for the diff-in-diff (§8).

    Regulars who lapsed lo..hi days ago: just inside the 45-day trigger, so they are
    behaviourally comparable to the treated cohort, and genuinely never messaged. We take the
    whole eligible pool (deterministic) and return the RFM-decile balance of both arms so the
    match can be audited rather than asserted.
    """
    now = demo_now()
    rows = q(
        "SELECT customer_tok, rfm_decile FROM customer_merchant_stats "
        "WHERE merchant_id = ? AND visits_lifetime >= ? AND last_visit_at < ? AND last_visit_at >= ? "
        "ORDER BY customer_tok",
        (merchant_id, settings.regular_min_visits,
         iso(now - timedelta(days=lo)), iso(now - timedelta(days=hi + 1))),
    )
    toks = [r["customer_tok"] for r in rows]

    def mix(pairs: list[int]) -> dict[str, float]:
        n = max(1, len(pairs))
        counts: dict[int, int] = {}
        for d in pairs:
            counts[d] = counts.get(d, 0) + 1
        return {str(k): round(v / n, 3) for k, v in sorted(counts.items())}

    return {
        "customer_toks": toks,
        "n": len(toks),
        "lapsed_window_days": [lo, hi],
        "decile_mix_control": mix([int(r["rfm_decile"]) for r in rows]),
        "decile_mix_treatment": mix([int(t.get("rfm_decile", 0)) for t in treatment]),
    }


def customer_stats(merchant_id: int, tok: str) -> dict[str, Any] | None:
    r = q1("SELECT * FROM customer_merchant_stats WHERE merchant_id = ? AND customer_tok = ?", (merchant_id, tok))
    return dict(r) if r else None


# ---------------------------------------------------------------- benchmarks

def benchmark(vertical: str, pincode: str, k_min: int = 20) -> dict[str, Any] | None:
    """Anonymised vertical x pincode aggregate. Suppressed below the k-anonymity floor (§15)."""
    r = q1("SELECT * FROM benchmarks WHERE vertical = ? AND pincode = ?", (vertical, pincode))
    if not r:
        r = q1(
            "SELECT vertical, 'ALL' AS pincode, AVG(repeat_rate) AS repeat_rate, "
            "AVG(avg_daily_gmv_paise) AS avg_daily_gmv_paise, AVG(avg_ticket_paise) AS avg_ticket_paise, "
            "SUM(cohort_size) AS cohort_size, MAX(computed_at) AS computed_at "
            "FROM benchmarks WHERE vertical = ? GROUP BY vertical",
            (vertical,),
        )
    if not r:
        return None
    if int(r["cohort_size"]) < k_min:
        return {"suppressed": True, "reason": f"cohort of {int(r['cohort_size'])} is below the k-anonymity floor of {k_min}"}
    return {
        "vertical": r["vertical"],
        "pincode": r["pincode"],
        "repeat_rate": round(float(r["repeat_rate"]), 4),
        "avg_daily_gmv_paise": int(r["avg_daily_gmv_paise"]),
        "avg_ticket_paise": int(r["avg_ticket_paise"]),
        "cohort_size": int(r["cohort_size"]),
        "computed_at": r["computed_at"],
    }


# ---------------------------------------------------------------- runs / signals

def open_signals(merchant_id: int, limit: int = 10) -> list[dict[str, Any]]:
    rows = q(
        "SELECT signal_id, kind, severity, payload, detected_at FROM signals "
        "WHERE merchant_id = ? ORDER BY severity DESC, signal_id DESC LIMIT ?",
        (merchant_id, limit),
    )
    return [{"signal_id": r["signal_id"], "kind": r["kind"], "severity": round(float(r["severity"]), 3),
             "payload": jload(r["payload"]), "detected_at": r["detected_at"]} for r in rows]


def run(run_id: str) -> dict[str, Any] | None:
    r = q1("SELECT * FROM playbook_runs WHERE run_id = ?", (run_id,))
    if not r:
        return None
    d = dict(r)
    d["prescription"] = jload(d["prescription"])
    return d


def runs_for(merchant_id: int, states: tuple[str, ...] | None = None, limit: int = 50) -> list[dict[str, Any]]:
    sql = "SELECT * FROM playbook_runs WHERE merchant_id = ?"
    params: list[Any] = [merchant_id]
    if states:
        sql += f" AND state IN ({','.join('?' * len(states))})"
        params.extend(states)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    out = []
    for r in q(sql, params):
        d = dict(r)
        d["prescription"] = jload(d["prescription"])
        out.append(d)
    return out


def cohort(run_id: str, arm: str) -> list[str]:
    return [r["customer_tok"] for r in q(
        "SELECT customer_tok FROM run_cohorts WHERE run_id = ? AND arm = ?", (run_id, arm))]


def regulars_count(merchant_id: int, within_days: int | None = None) -> dict[str, Any]:
    """Regulars = >= regular_min_visits lifetime visits. Split by whether they are still active.

    "Still active" uses the same 45-day gap as the win-back trigger, so `active + lapsed`
    always reconciles with what `lapsed_customers()` returns — the brief and the audience
    can never disagree.
    """
    gap = settings.lapsed_gap_days if within_days is None else within_days
    cutoff = iso(demo_now() - timedelta(days=gap))
    r = q1(
        "SELECT COUNT(*) AS n, "
        "       SUM(CASE WHEN last_visit_at >= ? THEN 1 ELSE 0 END) AS active "
        "FROM customer_merchant_stats WHERE merchant_id = ? AND visits_lifetime >= ?",
        (cutoff, merchant_id, settings.regular_min_visits),
    )
    total = int(r["n"] or 0)
    active = int(r["active"] or 0)
    return {"regulars": active, "lapsed": total - active, "regulars_lifetime": total,
            "gap_days": gap, "min_visits": settings.regular_min_visits}
