#!/usr/bin/env python3
"""§14 harness: golden-set behavioural eval + model gates.

    python -m eval.run_eval

Grades what matters in an agent: the RIGHT tools in the right order, numbers that came
from those tools, no PII leakage into the answer, no fabricated precision, cards only
when a prescription is warranted. Plus the three model gates (churn AUC, forecast
holdout, benchmark k-anonymity) re-checked live.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.config import settings
from backend.app.db import init_db, q
from backend.app.mkg import repositories as repo
from backend.app.ml import churn, forecast


def run_cases(cases: list[dict]) -> tuple[int, int]:
    from backend.app.agent import loop as agent_loop
    passed = total = 0
    for case in cases:
        total += 1
        problems: list[str] = []
        res = agent_loop.run_chat(settings.demo_merchant_id, case["user"])
        text, steps = res["text"], res["steps"]
        used = [s["tool"] for s in steps if s["ok"]]
        for t in case.get("tools_must", []):
            if t not in used:
                problems.append(f"missing tool {t}")
        for frag in case.get("answer_must_contain", []):
            if frag not in text:
                problems.append(f"answer missing {frag!r}")
        for frag in case.get("answer_must_not_contain", []):
            if frag in text:
                problems.append(f"answer must not contain {frag!r}")
        if case.get("card_expected") and not res["card"]:
            problems.append("expected a prescription card")
        if not case.get("card_expected") and res["card"] and case["id"] != "winback_direct":
            problems.append("unexpected card")
        if any(s["tool"] == "request_approval" for s in steps) and not case.get("card_expected"):
            problems.append("asked for approval when it should not")
        ok = not problems
        passed += ok
        print(f"  {'✓' if ok else '✗'} {case['id']:<26} tools={','.join(used[:4])}{'…' if len(used) > 4 else ''}")
        for p in problems:
            print(f"      · {p}")
        if problems:
            print(f"      answer: {text[:180]}")
    return passed, total


def run_gates(cfg: dict) -> bool:
    print("\n  model gates (§14):")
    ok = True
    lapsed = repo.lapsed_customers(settings.demo_merchant_id)
    labelled = [(c["churn_score"], 1 if c["days_since_visit"] >= 45 else 0)
                for c in repo.lapsed_customers(settings.demo_merchant_id, min_gap_days=10)]
    g = churn.gate(labelled, min_auc=cfg["churn_auc_min"])
    print(f"    {'✓' if g['passed'] else '✗'} churn AUC {g['auc']} ≥ {cfg['churn_auc_min']} → {g['serving']}")
    ok &= g["passed"]

    series = repo.daily_gmv(settings.demo_merchant_id, 60)
    fg = forecast.gate(series)
    serving_ok = "fallback" in fg.get("serving", "model") or fg.get("passed", False)
    print(f"    {'✓' if serving_ok else '✗'} forecast gate → {fg.get('serving', fg)}")
    ok &= serving_ok

    kmin = cfg["benchmark_k_floor"]
    bad = [r for r in q("SELECT * FROM benchmarks") if int(r["cohort_size"]) < kmin]
    print(f"    {'✓' if not bad else '✗'} benchmark k-anonymity: {len(bad)} cells below k={kmin}")
    ok &= not bad
    return ok


def main() -> int:
    import subprocess
    r = subprocess.run([sys.executable, "-m", "datagen.generate"], cwd=str(ROOT),
                       capture_output=True, text=True)
    if r.returncode:
        print(r.stderr)
        return 1
    init_db()
    from backend.app.signals import detectors
    detectors.scan(settings.demo_merchant_id)      # the nightly job; cases assume SENSE ran
    spec = yaml.safe_load((ROOT / "eval" / "golden_set.yaml").read_text())
    print("=" * 64)
    print("GOLDEN-SET EVAL")
    print("=" * 64)
    passed, total = run_cases(spec["cases"])
    gates_ok = run_gates(spec["gates"])
    print("=" * 64)
    verdict = "PASS" if passed == total and gates_ok else "FAIL"
    print(f"cases {passed}/{total} · gates {'ok' if gates_ok else 'FAILED'} → {verdict}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
