#!/usr/bin/env python3
"""The demo verification harness: run the WHOLE loop on a fresh world and assert every
number the deck promises (slide 9 + §8 report card). CI for the demo itself.

    python -m eval.verify_demo

Exit 0 = every claim holds, from data, through the real query path. Any failure prints
the expected vs actual and the check that broke — no silent passes.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, expected, actual) -> None:
    ok = expected == actual if not isinstance(expected, float) else abs(expected - actual) < 1e-9
    CHECKS.append((name, ok, f"expected {expected!r}, got {actual!r}"))


def approx(name: str, expected: float, actual: float, tol: float = 0.02) -> None:
    CHECKS.append((name, abs(expected - actual) <= tol, f"expected ~{expected}, got {actual}"))


def main() -> int:
    # ---- fresh world ---------------------------------------------------------------
    db = ROOT / "vyapaar.db"
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            p.unlink()
    r = subprocess.run([sys.executable, "-m", "datagen.generate"], cwd=str(ROOT),
                       capture_output=True, text=True)
    if r.returncode:
        print(r.stdout, r.stderr)
        return 1

    from backend.app.config import settings
    from backend.app.db import init_db
    init_db()
    from backend.app.signals import detectors
    from backend.app.agent import loop as agent_loop, tools
    from backend.app.playbooks import engine
    from backend.app.actions.connectors import world_sim
    from backend.app.outcomes import attribution, report_card
    from backend.app.mkg import repositories as repo
    from backend.app.mkg import memory

    mid = settings.demo_merchant_id

    # ---- SENSE -----------------------------------------------------------------------
    fired = detectors.scan(mid)
    kinds = [s["kind"] for s in fired]
    check("sales_anomaly fires", True, "sales_anomaly" in kinds)
    check("churn_cluster fires", True, "churn_cluster" in kinds)
    check("benchmark_gap fires", True, "benchmark_gap" in kinds)
    anomaly_sig = next(s for s in fired if s["kind"] == "sales_anomaly")
    check("Sunday drop reads -18%", -0.18, anomaly_sig["evidence"]["delta_pct"])
    check("Sunday baseline ₹10,000", 1_000_000, anomaly_sig["evidence"]["baseline_gmv_paise"])
    check("Sunday actual ₹8,200", 820_000, anomaly_sig["evidence"]["actual_gmv_paise"])

    # ---- the calibration the whole story rests on ------------------------------------
    brief = memory.brief(mid)
    check("avg daily GMV 30d ₹9,100", 910_000, brief["sales_30d"]["avg_daily_gmv_paise"])
    check("30d GMV ₹2,73,000", 27_300_000, brief["sales_30d"]["gmv_paise"])
    check("repeat rate 0.22", 0.22, brief["repeat_rate_30d"]["repeat_rate"])
    check("regulars 480", 480, brief["customers"]["regulars"])
    lapsed = repo.lapsed_customers(mid)
    check("lapsed regulars 212", 212, len(lapsed))
    ctrl = repo.matched_controls(mid, lapsed)
    check("control pool 150", 150, ctrl["n"])

    # ---- DIAGNOSE -> PRESCRIBE (chat, through the real loop) ---------------------------
    res = agent_loop.run_chat(mid, "kal bikri kam kyu lagi?")
    check("agent used estimate_impact", True,
          any(s["tool"] == "estimate_impact" for s in res["steps"]))
    check("agent requested approval", True,
          any(s["tool"] == "request_approval" for s in res["steps"]))
    check("card present", True, res["card"] is not None)
    band = res["card"]["prescription"]["impact"]["gmv_influenced_band_paise"]
    check("impact band low ₹4,800", 480_000, band[0])
    check("impact band high ₹6,500", 650_000, band[1])
    check("cost cap ₹1,100", 110_000, res["card"]["prescription"]["impact"]["cost_cap_paise"])
    check("min ROI met", True, res["card"]["prescription"]["meets_min_roi"])

    # numbers in the answer must all come from tools — every step is audited
    trace_rows = [a for a in __import__("backend.app.db", fromlist=["q"]).q(
        "SELECT payload FROM audit_log WHERE trace_id = ?", (res["trace_id"],))]
    check("audit trace has tool calls", True,
          any(__import__("json").loads(a["payload"]).get("tool") == "estimate_impact"
              for a in trace_rows if __import__("json").loads(a["payload"]).get("tool")))

    # ---- APPROVE -> EXECUTE ------------------------------------------------------------
    run_id = res["card"]["run_id"]
    engine.approve(run_id, "merchant:1042", res["card"]["approval_token"])
    out = tools.call("execute_action", {"run_id": run_id}, trace_id=res["trace_id"], merchant_id=mid)
    check("audience 212", 212, out["audience"])
    check("some deliveries fail (honest funnel)", True, out["failed"] > 0)
    arms = {a: len(repo.cohort(run_id, a)) for a in ("treatment", "control")}
    check("treated arm 212", 212, arms["treatment"])
    check("control arm 150", 150, arms["control"])

    # ---- idempotency: a retry cannot double-send ---------------------------------------
    again = tools.call("execute_action", {"run_id": run_id}, trace_id=res["trace_id"], merchant_id=mid)
    check("execute is idempotent", True, again.get("already_executed", False))
    n_actions = len(__import__("backend.app.db", fromlist=["q"]).q(
        "SELECT 1 FROM actions WHERE run_id = ?", (run_id,)))
    check("one action row per run", 1, n_actions)

    # ---- MEASURE (7d) ------------------------------------------------------------------
    world_sim.advance(7)
    det = attribution.measure(run_id, 7)
    comp = det["components_paise"]
    check("DiD GMV ₹6,100", 610_000, comp["gmv_influenced"])
    check("31 redemptions", 31, det["redemptions"])
    cost = sum(det["cost_breakdown_paise"].values())
    check("realized cost ₹1,044", 104_400, cost)
    from backend.app.db import q1
    roi_row = q1("SELECT roi FROM outcomes WHERE run_id = ? AND window_d = 7", (run_id,))
    approx("ROI ~4.84", 4.84, float(roi_row["roi"]), tol=0.01)
    row = repo.run(run_id)
    check("run closed", "closed", row["state"])

    # ---- REPORT CARD ------------------------------------------------------------------
    rep = report_card.report(mid)
    h = rep["headline"]
    check("6 actions on card", 6, h["actions_run"])
    check("recovered ₹21,400", 2_140_000, h["recovered_revenue_paise"]["total"])
    check("live ₹6,100", 610_000, h["recovered_revenue_paise"]["live_measured"])
    check("seeded ₹15,300", 1_530_000, h["recovered_revenue_paise"]["seeded_history"])

    # ---- MEASURE (30d): the 22% -> 31% progression ---------------------------------------
    world_sim.advance(23)
    attribution.measure(run_id, 30)
    rep2 = report_card.report(mid)
    t = rep2["repeat_rate"]["treated_cohort_30d"]
    check("66 of 212 treated returned", 66, t["returned"])
    approx("treated rate ~31%", 0.31, t["rate"])
    check("baseline 22% (pre-campaign)", 0.22, t["baseline"])

    # ---- guardrails actually bite --------------------------------------------------------
    from backend.app.trust import policy as pol
    d = pol.check(mid, audience_size=1000, cost_paise=1_000_000, channel="whatsapp",
                  stage="pre_execute", at=__import__("backend.app.db", fromlist=["demo_now"]).demo_now())
    check("policy blocks a ₹10k/1000-person blast", False, d.allowed)
    codes = {v.code for v in d.violations}
    # one campaign went out this week, so the frequency cap has room; the spend caps bite
    check("caps named explicitly", True, {"spend_cap_run", "spend_cap_week"} <= codes)

    # ---- verdict --------------------------------------------------------------------------
    failed = [c for c in CHECKS if not c[1]]
    print(f"\n{'='*64}\nVERIFY-DEMO: {len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed\n{'='*64}")
    for name, ok, detail in CHECKS:
        print(f"  {'✓' if ok else '✗ FAIL'}  {name:<38} {detail if not ok else ''}")
    if failed:
        print(f"\n{len(failed)} FAILURES — the demo does not match the deck.")
        return 1
    print("\nEvery slide-9 number reproduces from data through the live query path.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
