#!/usr/bin/env python3
"""Deterministic world generator (BUILD_PLAN §13).

The demo has to hit the deck's slide-9 numbers *from data*, not from fixtures. So this script
does not write those numbers anywhere near the query path — it builds a transaction ledger whose
shape makes the detectors produce them. Concretely, the calibration in
`scenarios/sharma_tea_stall.json` is turned into:

  * an exact 30-day ledger:      27,300,000 paise over 7,800 txns  -> ₹9,100/day, ₹35 avg ticket
  * five pinned Sundays:         yesterday ₹8,200 against a 4-week Sunday baseline of ₹10,000
  * three regular cohorts:       330 active, 150 mid-lapsed (control arm), 212 lapsed (audience)
  * 1,170 single-visit walk-ins: so repeat_rate_30d = 330/1500 = 0.22 exactly
  * 200 background merchants:    so benchmarks are real anonymised aggregates with cohort_size 20

Two invariants worth stating, because everything downstream leans on them:

  T (the demo clock) is the most recent Monday 09:15 IST. That makes the sales anomaly land on
  *yesterday, Sunday* — the natural "merchant opens the app on Monday morning" story — and keeps
  the 4-week same-weekday baseline entirely inside the 30-day window.

  customer_merchant_stats is never written by hand; it is rebuilt from `transactions` at the end.
  The ledger is the single source of truth, so the RFM edges of the MKG cannot drift from it.

Re-runnable: drops and rebuilds every table it owns. `python -m datagen.generate`
"""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.config import settings                     # noqa: E402
from backend.app.db import connect, init_db, iso, jdump, tx  # noqa: E402
from backend.app.mkg import memory                           # noqa: E402
from backend.app.ml import churn                              # noqa: E402
from backend.app.trust import pii                             # noqa: E402

IST = timedelta(hours=5, minutes=30)

# Baskets at a tea stall: whole rupees, mean ≈ ₹36 before the exact-sum rescale below.
BASKETS = [2000, 2500, 3000, 3500, 4000, 4500, 5000, 6000]
BASKET_W = [1, 2, 3, 4, 3, 2, 1, 1]
# 06:00-21:00 trade with a morning and an evening peak.
HOURS = list(range(6, 22))
HOUR_W = [3, 9, 12, 10, 6, 5, 5, 4, 4, 5, 8, 11, 10, 7, 4, 2]
CHANNELS = ["soundbox", "upi", "upi", "upi", "wallet", "card"]

TABLES = [
    "transactions", "inventory_snapshots", "customer_merchant_stats", "customers", "merchants", "benchmarks",
    "merchant_memory", "memory_episodes", "signals", "playbook_runs", "actions", "deliveries",
    "run_cohorts", "outcomes", "audit_log", "conversations", "messages", "reminders", "clock",
    "pii_vault",
]


# ---------------------------------------------------------------- exact arithmetic

def apportion(total: int, weights: list[float], floor: int = 0) -> list[int]:
    """Split `total` into len(weights) integers summing to EXACTLY total (largest remainder).

    Used everywhere a headline figure has to reconcile: day GMV across a month, txn counts
    across days, rupees across the txns of one day. Floating point never reaches the ledger.
    """
    n = len(weights)
    if n == 0:
        return []
    s = float(sum(weights))
    if s <= 0:
        weights, s = [1.0] * n, float(n)
    exact = [total * w / s for w in weights]
    base = [int(x) for x in exact]
    order = sorted(range(n), key=lambda i: (-(exact[i] - base[i]), i))
    for i in range(total - sum(base)):
        base[order[i % n]] += 1
    for i in range(n):                       # lift anything under the floor off the richest day
        while base[i] < floor:
            j = max(range(n), key=lambda k: base[k])
            if base[j] <= floor:
                break
            base[j] -= 1
            base[i] += 1
    return base


class World:
    def __init__(self, scenario: dict[str, Any]) -> None:
        self.s = scenario
        self.rng = random.Random(settings.seed)
        self.txns: list[tuple] = []
        # T = most recent Monday 09:15 IST (today if today is a Monday).
        today_ist = (datetime.now(timezone.utc) + IST).date()
        self.monday = today_ist - timedelta(days=today_ist.weekday())
        self.now = datetime.combine(self.monday, time(9, 15), tzinfo=timezone.utc) - IST

    # ------------------------------------------------------------ time helpers

    def date_of(self, d: int):
        """IST calendar date `d` days before T. d=1 is yesterday (a Sunday)."""
        return self.monday - timedelta(days=d)

    def stamp(self, d: int) -> tuple[str, str]:
        """A trading-hours instant on day d, as (event_time, ingest_time) in UTC ISO."""
        hour = self.rng.choices(HOURS, weights=HOUR_W, k=1)[0]
        wall = datetime.combine(self.date_of(d), time(hour, self.rng.randrange(60),
                                                      self.rng.randrange(60)),
                                tzinfo=timezone.utc)
        event = wall - IST
        return iso(event), iso(event + timedelta(seconds=self.rng.randrange(5, 90)))

    # ------------------------------------------------------------ customers

    def make_customers(self, prefix: str, n: int, vault: bool) -> list[str]:
        """Tokenized customers. Raw phones go to the vault only when a connector must reach them."""
        toks = []
        for i in range(n):
            ph = f"9{self.rng.randrange(700000000, 999999999)}"
            name = f"{prefix}-{i:04d}"
            toks.append(pii.store(ph, name) if vault else pii.tokenize(ph))
        return toks

    # ------------------------------------------------------------ the demo merchant

    def build_demo(self) -> dict[str, Any]:
        s, rng = self.s, self.rng
        c = s["cohorts"]
        mid = settings.demo_merchant_id
        items = s["items"]
        item_w = [10, 5, 4, 3, 2, 2][:len(items)]

        active = self.make_customers("regular", c["active_regulars"], vault=True)
        mid_lapsed = self.make_customers("midlapsed", c["mid_lapsed_control_pool"], vault=True)
        lapsed = self.make_customers("lapsed", c["lapsed_regulars"], vault=True)
        walkins = self.make_customers("walkin", c["walkins_30d"], vault=False)

        # ---- last 30 days: pinned to the calibration, to the paise -------------------
        sundays = {int(k): v for k, v in s["sundays_paise"].items() if k.isdigit()}
        target_units = s["targets"]["avg_daily_gmv_paise_30d"] * 30 // 100
        free_days = [d for d in range(1, 31) if d not in sundays]
        dow_mult = {0: 0.95, 1: 0.92, 2: 0.94, 3: 0.98, 4: 1.05, 5: 1.12}   # Mon..Sat
        weights = [dow_mult[self.date_of(d).weekday()] * (1 + rng.uniform(-0.03, 0.03))
                   for d in free_days]
        units: dict[int, int] = dict(zip(free_days,
                                         apportion(target_units - sum(sundays.values()) // 100,
                                                   weights)))
        units.update({d: v // 100 for d, v in sundays.items()})

        days = list(range(30, 0, -1))                     # oldest first: keeps the RFM cycle sane
        n_txn = dict(zip(days, apportion(s["targets"]["avg_daily_gmv_paise_30d"] * 30
                                         // s["avg_ticket_paise"],
                                         [units[d] for d in days])))
        n_walk = dict(zip(days, apportion(c["walkins_30d"], [n_txn[d] for d in days])))

        walk_iter = iter(walkins)
        cycle, cursor = list(active), 0
        plan: dict[int, list[str]] = {}
        for d in days:
            slots = []
            for _ in range(n_txn[d] - n_walk[d]):         # round-robin so coverage is uniform
                slots.append(cycle[cursor % len(cycle)])
                cursor += 1
            slots.extend(next(walk_iter) for _ in range(n_walk[d]))
            rng.shuffle(slots)
            plan[d] = slots

        # ---- days 31..90: builds the cohorts' last_visit_at, so the trigger is real ---
        early = list(range(90, 30, -1))
        base_n = s["targets"]["avg_daily_gmv_paise_30d"] // s["avg_ticket_paise"]
        early_n = {d: max(40, round(base_n * dow_mult.get(self.date_of(d).weekday(), 1.30)
                                    * (1 + rng.uniform(-0.05, 0.05)))) for d in early}
        early_plan: dict[int, list[str]] = {d: [] for d in early}

        def schedule(toks: list[str], gap_lo: int, gap_hi: int) -> None:
            """Pin each customer's LAST visit inside [gap_lo, gap_hi], with earlier visits behind it."""
            span = list(range(gap_lo, gap_hi + 1))
            for i, tok in enumerate(toks):
                g = span[i % len(span)]
                early_plan[g].append(tok)
                for _ in range(rng.randint(2, 8)):        # >= 3 lifetime visits => a regular
                    prior = rng.randint(g + 1, 90)
                    early_plan[prior].append(tok)

        schedule(lapsed, *c["lapsed_gap_days"])
        schedule(mid_lapsed, *c["mid_lapsed_gap_days"])
        for tok in active:                                # active regulars also have a past
            for _ in range(rng.randint(4, 20)):
                early_plan[rng.randint(31, 90)].append(tok)

        early_walkins = self.make_customers("earlywalkin", 0, vault=False)
        filler = 0
        for d in early:
            gap = early_n[d] - len(early_plan[d])
            for _ in range(max(0, gap)):
                filler += 1
                early_plan[d].append(pii.tokenize(f"9{600000000 + filler}"))
            rng.shuffle(early_plan[d])
        plan.update(early_plan)

        # ---- ledger: day totals are exact, amounts are apportioned inside the day ----
        for d in sorted(plan, reverse=True):
            toks = plan[d]
            if not toks:
                continue
            raw = rng.choices(BASKETS, weights=BASKET_W, k=len(toks))
            day_units = units[d] if d <= 30 else round(len(toks) * s["avg_ticket_paise"] / 100)
            amounts = apportion(day_units, [float(r) for r in raw], floor=5)
            for tok, amt in zip(toks, amounts):
                event, ingest = self.stamp(d)
                self.txns.append((mid, tok, amt * 100,
                                  rng.choice(CHANNELS), rng.choices(items, weights=item_w, k=1)[0],
                                  event, ingest, None))

        return {"active": active, "mid_lapsed": mid_lapsed, "lapsed": lapsed,
                "walkins": walkins, "early_walkins": early_walkins}

    # ------------------------------------------------------------ background universe

    def build_universe(self) -> list[dict[str, Any]]:
        s, rng = self.s, self.rng
        u = s["universe"]
        per_cell = u["merchants"] // (len(u["verticals"]) * u["pincodes_per_vertical"])
        rows, mid = [], 2000
        shock = set()
        cities = {"tea_stall": "Jaipur", "kirana": "Jaipur", "salon": "Jaipur",
                  "restaurant": "Jaipur", "pharmacy": "Jaipur"}
        for vi, vertical in enumerate(u["verticals"]):
            for pi in range(u["pincodes_per_vertical"]):
                # tea_stall shares 302001 with the demo merchant, so its benchmark cell resolves.
                pincode = f"3020{vi * 2 + pi + 1:02d}"
                for _ in range(per_cell):
                    mid += 1
                    rows.append({"merchant_id": mid, "vertical": vertical, "pincode": pincode,
                                 "city": cities[vertical]})
        for m in rng.sample(rows, u["competitor_shock_merchants"]):
            shock.add(m["merchant_id"])

        hist = u["history_days"]
        for m in rows:
            target_rr = min(0.6, max(0.1, rng.gauss(u["benchmark_repeat_rate"][m["vertical"]], 0.03)))
            ticket = u["avg_ticket_paise"][m["vertical"]]
            per_day = rng.randint(*u["txns_per_day"])
            visits = rng.randint(3, 5)
            # R regulars at `visits` visits each + W one-visit walk-ins gives repeat_rate = R/(R+W).
            n_window = per_day * 30
            regulars = max(4, round(n_window / (visits + (1 - target_rr) / target_rr)))
            walkers = round(regulars * (1 - target_rr) / target_rr)
            reg_toks = [pii.tokenize(f"m{m['merchant_id']}r{i}") for i in range(regulars)]
            slots: list[tuple[int, str]] = []
            for i, tok in enumerate(reg_toks):
                for v in range(visits):
                    slots.append((rng.randint(1, 30), tok))
            for i in range(walkers):
                slots.append((rng.randint(1, 30), pii.tokenize(f"m{m['merchant_id']}w{i}")))
            for i in range(per_day * (hist - 30)):        # pre-window history, outside the benchmark
                slots.append((rng.randint(31, hist), pii.tokenize(f"m{m['merchant_id']}h{i}")))
            m["repeat_rate_target"] = target_rr
            for d, tok in slots:
                amt = max(500, int(rng.gauss(ticket, ticket * 0.25)) // 100 * 100)
                if m["merchant_id"] in shock and d <= u["competitor_shock_day"]:
                    amt = int(amt * 0.75) // 100 * 100    # competitor opened nearby
                event, ingest = self.stamp(d)
                self.txns.append((m["merchant_id"], tok, amt, rng.choice(CHANNELS), None,
                                  event, ingest, None))
        return rows


# ---------------------------------------------------------------- persistence

def rebuild_stats(merchant_id: int, now: datetime) -> None:
    """Recompute the RFM edge from `transactions`. The ledger is the only source of truth."""
    conn = connect()
    conn.execute("DELETE FROM customer_merchant_stats WHERE merchant_id = ?", (merchant_id,))
    d30, d90 = iso(now - timedelta(days=30)), iso(now - timedelta(days=90))
    rows = conn.execute(
        "SELECT customer_tok, COUNT(*) AS visits, MIN(event_time) AS first_at, "
        "       MAX(event_time) AS last_at, SUM(amount_paise) AS ltv, "
        "       SUM(CASE WHEN event_time >= ? THEN 1 ELSE 0 END) AS v90, "
        "       SUM(CASE WHEN event_time >= ? THEN 1 ELSE 0 END) AS v30 "
        "FROM transactions WHERE merchant_id = ? GROUP BY customer_tok",
        (d90, d30, merchant_id),
    ).fetchall()

    scored = []
    for r in rows:
        visits, ltv = int(r["visits"]), int(r["ltv"])
        avg = ltv // visits
        gap = (now - datetime.strptime(r["last_at"], "%Y-%m-%dT%H:%M:%SZ")
               .replace(tzinfo=timezone.utc)).days
        scored.append({
            "tok": r["customer_tok"], "visits": visits, "v90": int(r["v90"]), "v30": int(r["v30"]),
            "first": r["first_at"], "last": r["last_at"], "ltv": ltv, "avg": avg, "gap": gap,
            "churn": churn.score(days_since_visit=gap, visits_90d=int(r["v90"]),
                                 avg_ticket_paise=avg),
        })
    # RFM decile 10 = best (recent, frequent, high value); ties broken deterministically.
    scored.sort(key=lambda x: (-x["ltv"], x["gap"], x["tok"]))
    n = len(scored)
    for i, x in enumerate(scored):
        x["decile"] = 10 - min(9, i * 10 // max(1, n))
    conn.executemany(
        "INSERT INTO customer_merchant_stats(merchant_id, customer_tok, visits_lifetime, visits_90d,"
        " visits_30d, first_visit_at, last_visit_at, avg_ticket_paise, ltv_paise, churn_score,"
        " rfm_decile) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(merchant_id, x["tok"], x["visits"], x["v90"], x["v30"], x["first"], x["last"],
          x["avg"], x["ltv"], x["churn"], x["decile"]) for x in scored],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO customers(customer_tok, first_seen_at, segment) VALUES (?,?,?)",
        [(x["tok"], x["first"],
          "regular" if x["visits"] >= settings.regular_min_visits else "walkin") for x in scored],
    )


def seed_inventory(world: World, merchant_id: int) -> int:
    """Create a small, believable stock snapshot from the generated sales ledger.

    Inventory is intentionally derived from the same transactions the assistant reads:
    the last 7-day item velocity determines the reorder point and the on-hand quantity.
    The scenario only supplies operational metadata such as suppliers and lead times.
    """
    conn = connect()
    now = world.now
    start = iso(now - timedelta(days=7))
    items = world.s.get("inventory", {})
    written = 0
    for item, cfg in items.items():
        row = conn.execute(
            "SELECT COUNT(*) AS units FROM transactions WHERE merchant_id = ? "
            "AND item = ? AND event_time >= ?",
            (merchant_id, item, start),
        ).fetchone()
        sold_7d = int(row["units"] or 0)
        lead_days = max(1, int(cfg.get("restock_days", 1)))
        safety_units = max(4, round(sold_7d * float(cfg.get("safety_days", 1.5)) / 7))
        reorder_point = max(1, round(sold_7d * lead_days / 7) + safety_units)
        # Deliberately leave one fast-moving item just below its reorder point so the
        # stock answer is useful and the stockout detector has real operational context.
        coverage_days = float(cfg.get("coverage_days", 3.0))
        on_hand = max(0, round(sold_7d * coverage_days / 7))
        if item == world.s.get("inventory_risk_item"):
            on_hand = max(0, reorder_point - max(1, round(sold_7d * 0.08)))
        ratio = on_hand / reorder_point if reorder_point else 1.0
        status = "at_risk" if ratio < 0.75 else "reorder_soon" if ratio < 1.25 else "healthy"
        conn.execute(
            "INSERT INTO inventory_snapshots(merchant_id, item, units_sold_7d, units_on_hand, "
            "reorder_point_units, unit_cost_paise, supplier, restock_days, status, as_of) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (merchant_id, item, sold_7d, on_hand, reorder_point,
             int(cfg.get("unit_cost_paise", 0)), cfg.get("supplier", "local supplier"),
             lead_days, status, iso(now)),
        )
        written += 1
    return written


def compute_benchmarks(rows: list[dict[str, Any]], now: datetime, k_min: int = 20) -> int:
    """Real anonymised aggregates over the background merchants — never a hardcoded constant.

    The demo merchant is excluded from its own cell: "merchants like you" must mean *other*
    merchants, and including yourself would let a peer read your numbers back out.
    """
    conn = connect()
    end = (now + IST).replace(hour=0, minute=0, second=0, microsecond=0) - IST
    start = end - timedelta(days=30)
    cells: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for m in rows:
        agg = conn.execute(
            "SELECT COUNT(*) AS txns, COALESCE(SUM(amount_paise),0) AS gmv FROM transactions "
            "WHERE merchant_id = ? AND event_time >= ? AND event_time < ?",
            (m["merchant_id"], iso(start), iso(end)),
        ).fetchone()
        per_cust = conn.execute(
            "SELECT COUNT(*) AS n FROM (SELECT customer_tok, COUNT(*) AS c FROM transactions "
            "WHERE merchant_id = ? AND event_time >= ? AND event_time < ? GROUP BY customer_tok)",
            (m["merchant_id"], iso(start), iso(end)),
        ).fetchone()
        repeaters = conn.execute(
            "SELECT COUNT(*) AS n FROM (SELECT customer_tok, COUNT(*) AS c FROM transactions "
            "WHERE merchant_id = ? AND event_time >= ? AND event_time < ? GROUP BY customer_tok "
            "HAVING c >= 2)",
            (m["merchant_id"], iso(start), iso(end)),
        ).fetchone()
        txns, gmv, distinct = int(agg["txns"]), int(agg["gmv"]), int(per_cust["n"])
        if not txns:
            continue
        cells.setdefault((m["vertical"], m["pincode"]), []).append({
            "repeat_rate": int(repeaters["n"]) / distinct if distinct else 0.0,
            "gmv_day": gmv / 30, "ticket": gmv / txns,
        })
    written = 0
    for (vertical, pincode), peers in sorted(cells.items()):
        if len(peers) < k_min:                     # k-anonymity floor: do not publish the cell
            continue
        conn.execute(
            "INSERT OR REPLACE INTO benchmarks(vertical, pincode, repeat_rate, avg_daily_gmv_paise,"
            " avg_ticket_paise, cohort_size, computed_at) VALUES (?,?,?,?,?,?,?)",
            (vertical, pincode,
             round(sum(p["repeat_rate"] for p in peers) / len(peers), 4),
             round(sum(p["gmv_day"] for p in peers) / len(peers)),
             round(sum(p["ticket"] for p in peers) / len(peers)),
             len(peers), iso(now)),
        )
        written += 1
    return written


def seed_history(world: World, mid: int) -> int:
    """Five already-closed runs, so the weekly report card opens with a track record.

    Their outcomes are labelled source=seeded_history and confidence=measured-at-the-time: run #6
    is the one measured live from transactions during the demo. The report card adds them up but
    never blends the labels.
    """
    conn, s = connect(), world.s["seeded_history"]
    for i, r in enumerate(s["runs"], start=1):
        run_id = f"run_seed_{i:02d}"
        created = world.now - timedelta(days=r["days_ago"])
        roi = round((r["gmv_influenced_paise"] - r["cost_paise"]) / r["cost_paise"], 2) if r["cost_paise"] else None
        closed_at = created + timedelta(days=7)   # outcome booked a week after send
        conn.execute(
            "INSERT INTO playbook_runs(run_id, merchant_id, playbook, signal_id, state, prescription,"
            " approved_by, approved_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, mid, r["playbook"], None, "closed",
             jdump({"headline": f"{r['playbook']} ({r['days_ago']}d ago)",
                    "audience_size": r["delivered"], "cost_cap_paise": r["cost_paise"],
                    "measure": {"window_days": 7}, "source": "seeded_history"}),
             "merchant:1042", iso(created), iso(created), iso(closed_at)),
        )
        conn.execute(
            "INSERT INTO outcomes(run_id, window_d, gmv_influenced_paise, redemptions, delivered,"
            " cost_paise, roi, confidence, detail, computed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, 7, r["gmv_influenced_paise"], r["redemptions"], r["delivered"],
             r["cost_paise"], roi, "measured",
             jdump({"source": "seeded_history",
                    "note": "closed before the demo window; kept for the track record"}),
             iso(closed_at)),
        )
        memory.append_episode(
            mid, occurred_on=iso(created)[:10], event=f"ran {r['playbook']}",
            result={"gmv_influenced_paise": r["gmv_influenced_paise"], "redemptions": r["redemptions"],
                    "delivered": r["delivered"], "roi": roi},
            run_id=run_id,
        )
    return len(s["runs"])


def write_memory(world: World, mid: int) -> None:
    memory.upsert_doc(mid, {
        "profile": {"owner": world.s["merchant"]["owner"], "lang": world.s["merchant"]["lang"],
                    "shop": world.s["merchant"]["name"],
                    "hours": world.s["merchant"]["open_hours"]},
        "patterns": [
            "Sunday is the biggest day of the week; a soft Sunday is the earliest warning sign.",
            "Morning 07:00-09:00 and evening 16:00-19:00 carry most of the trade.",
            "Tea plus one snack is the typical basket; samosa drives the attach.",
        ],
        "learned": {
            "10pc_off_beats_free_item": "A 10% discount out-performed a free-item offer on win-backs.",
            "hindi_copy_preferred": "Hindi WhatsApp copy gets read; English does not.",
            "quiet_hours": "Never message after 21:30 — the shop is shut and it annoys people.",
        },
    })


def main() -> int:
    scenario = json.loads(settings.scenario_path.read_text())
    world = World(scenario)
    init_db()
    pii.ensure_vault()
    conn = connect()
    # The wipe must disable FKs OUTSIDE the transaction — inside one, the PRAGMA is a
    # no-op and child rows (messages->conversations, runs->signals) would block it.
    conn.execute("PRAGMA foreign_keys=OFF")
    with tx(conn):
        for t in TABLES:
            conn.execute(f"DELETE FROM {t}")
        conn.execute("INSERT INTO clock(id, now) VALUES (1, ?)", (iso(world.now),))
        m = scenario["merchant"]
        conn.execute(
            "INSERT INTO merchants(merchant_id, name, vertical, city, pincode, open_hours, lang,"
            " phone_tok, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (m["merchant_id"], m["name"], m["vertical"], m["city"], m["pincode"], m["open_hours"],
             m["lang"], pii.store(m["phone"], m["owner"]), iso(world.now - timedelta(days=400))),
        )
    conn.execute("PRAGMA foreign_keys=ON")

    cohorts = world.build_demo()
    universe = world.build_universe()
    with tx(conn):
        conn.executemany(
            "INSERT INTO merchants(merchant_id, name, vertical, city, pincode, open_hours, lang,"
            " phone_tok, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            [(u["merchant_id"], f"{u['vertical'].title()} #{u['merchant_id']}", u["vertical"],
              u["city"], u["pincode"], "08:00-21:00", "hi",
              pii.tokenize(f"merchant{u['merchant_id']}"), iso(world.now - timedelta(days=300)))
             for u in universe],
        )
        conn.executemany(
            "INSERT INTO transactions(merchant_id, customer_tok, amount_paise, channel, item,"
            " event_time, ingest_time, source_run_id) VALUES (?,?,?,?,?,?,?,?)",
            world.txns,
        )

    with tx(conn):
        rebuild_stats(settings.demo_merchant_id, world.now)
        inventory_rows = seed_inventory(world, settings.demo_merchant_id)
    cells = compute_benchmarks(universe, world.now)
    with tx(conn):
        runs = seed_history(world, settings.demo_merchant_id)
        write_memory(world, settings.demo_merchant_id)

    print(f"clock T          : {iso(world.now)}  ({world.monday} 09:15 IST, a Monday)")
    print(f"transactions     : {len(world.txns):,}")
    print(f"merchants        : {len(universe) + 1}  (1 demo + {len(universe)} background)")
    print(f"benchmark cells  : {cells}")
    print(f"seeded runs      : {runs}")
    print(f"inventory rows   : {inventory_rows}")
    print(f"cohorts          : " + ", ".join(f"{k}={len(v)}" for k, v in cohorts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
