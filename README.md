# Vyapaar AI — an AI Growth Copilot for Paytm merchants

A working prototype of the closed loop: **SENSE → DIAGNOSE → PRESCRIBE → EXECUTE → MEASURE**.
A chai-wallah in Jaipur opens the app on a Monday morning, asks *"kal bikri kam kyu lagi?"*
in Hinglish, and gets a diagnosed reason, a costed prescription, and a one-tap approval —
then, a week later, a measured result with a matched control group. No dashboards to
learn, no jargon, no numbers that cannot be traced to a tool call.

**Demo:** `./run.sh` → http://localhost:8642 · full walkthrough in [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md)

## What the loop does (all of it live, from data)

| Stage | What happens | Proof |
|---|---|---|
| SENSE | Nightly detectors (code, not the model) find the Sunday −18% dip, 212 lapsed regulars, a −17pp repeat-rate gap vs peers | `POST /v1/jobs/signals/scan`, signals feed |
| DIAGNOSE | The agent reads its tools: yesterday ₹8,200 vs a 4-Sunday ₹10,000 baseline; 212 regulars 45+ days gone | `POST /v1/chat` — every tool call streams by |
| PRESCRIBE | ₹4,800–₹6,500 expected GMV, ₹1,100 spend cap, ROI ≥ 3.78× — from `estimate_impact`, never from the model | the prescription card + audit drawer |
| APPROVE | One tap, signed token, replay-proof; nothing sends without it | `POST /v1/approvals/{run_id}/approve` |
| EXECUTE | WhatsApp connector, idempotent on run_id; policy engine gates spend/frequency/quiet-hours/audience | tracker, deliveries funnel |
| MEASURE | 7 days later: **₹6,100** influenced GMV, 31 redemptions, 4.84× ROI by diff-in-diff against 150 matched never-messaged controls | report card, outcome row |
| LEARN | The outcome is written back to merchant memory; the next prescription cites it | MKG episodes |

The weekly card reads: **6 actions · ₹21,400 recovered · repeat rate 22% → 31%** (treated cohort, pre-campaign baseline) — every figure recomputed from the ledger, seeded history labelled separately from live measurement.

## The seven rules the code enforces

1. **Detectors are code; the LLM is the brain, not the pipe.** `backend/app/signals/detectors.py` runs DOW-aware MAD z-scores, a gated churn model, k≥20 benchmarks.
2. **Numbers come from tools, never from the model.** `estimate_impact` (`backend/app/playbooks/impact.py`) is the only source of ₹ projections; the system prompt forbids arithmetic; `_fmt_paise` renders.
3. **Money-touching actions are approval-gated and idempotent on `run_id`.** HMAC-signed approval tokens (`trust/approvals.py`); connectors dedupe on `idempotency_key`.
4. **Everything is audited.** Every prompt, tool call/result, approval, policy decision, send → `audit_log`, queryable per trace: `GET /v1/audit?trace_id=…`.
5. **Every model has a heuristic fallback.** Churn → 45-day recency rule; forecast → 4-week moving average; the LLM itself → the scripted planner. All gated in `eval/run_eval.py`.
6. **The merchant owns the money.** Caps and quiet hours live in the policy engine (`trust/policy.py`), in code, checked at `pre_execute` — not in prompts.
7. **MVP schemas = production schemas.** `db.py` carries the §11 DDL; SQLite→Postgres is a driver swap (three mechanical type substitutions, documented in the header).

## Layout

```
datagen/            deterministic world generator (90-day ledger, 201 merchants, benchmarks)
backend/app/
  mkg/              merchant knowledge graph: entities, brief, hashed-embedding memory
  ml/               anomaly / churn / forecast, each behind a gate with a fallback
  signals/          detectors (SENSE)
  agent/            tool registry, guardrails, prompts, ReAct loop, both LLM providers
  playbooks/        6 YAML playbooks + state machine + impact estimator
  actions/          connectors: mock WhatsApp BSP, world simulator (sandbox payment rail)
  outcomes/         diff-in-diff attribution, weekly report card
  trust/            PII tokenization, audit, policy engine, signed approvals
dashboard/          zero-build SPA (no npm): signals, cards, chat+voice, tracker, audit drawer
eval/               verify_demo (40 assertions on the deck's numbers) + golden set + gates
```

## Two brains, one contract

`VYAPAAR_LLM=scripted` (default): a deterministic planner — the demo works offline, every
time, zero spend. `VYAPAAR_LLM=sarvam` uses Sarvam's function-calling chat model for
Hindi/Hinglish answers; `VYAPAAR_LLM=claude` uses the same tool registry with Claude.
Both real providers and the offline fallback answer from the generated transaction ledger,
benchmarks and inventory snapshot; policy stays in code either way.

Voice is browser-native by default. Add `SARVAM_API_KEY` to `.env` to use Sarvam Saaras v3
and Bulbul v3; the dashboard language dropdown supports English plus Hindi, Bengali, Tamil,
Telugu, Kannada, Malayalam, Marathi, Gujarati, Punjabi and Odia. The browser voice remains
the fallback when Sarvam is not configured.

## Sandbox deviations (all labelled in-product)

| Production | This prototype | Why |
|---|---|---|
| Postgres + pgvector | SQLite (WAL) + hashed bag-of-words embeddings | no Docker on the demo box; same DDL semantics |
| WhatsApp BSP | mock connector with a seeded delivery/failure rate | no BSP creds; same per-recipient event shape |
| Payment event stream | `world_sim.advance()` — a labelled demo control | no rail in the sandbox; events are real transactions |
| Next.js app | zero-build SPA | same §10 API, nothing to install |

## Commands

```
./run.sh                      # generate → verify → serve on :8642
python -m eval.verify_demo    # 40 assertions: every deck number, from data
python -m eval.run_eval       # golden-set behavioural eval + §14 model gates
python -m datagen.generate    # fresh deterministic world (seed 20260917)
VYAPAAR_LLM=claude ./run.sh   # same demo on the real model (needs ANTHROPIC_API_KEY)
./run.sh                      # uses Sarvam voice too when SARVAM_API_KEY is in .env
```
