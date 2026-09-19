# Vyapaar AI — every command, in order

Everything runs from the `vyapaar-ai/` folder. Python 3.10+ required; nothing else to install
(no Docker, no npm). Full walkthrough for a live audience: [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md)

---

## 1. One-command demo

```bash
./run.sh
```

Does everything below in order — deps → world → verify → fresh world → serve.
Then open **http://localhost:8642** (Ctrl-C to stop).

## 2. Step by step (what run.sh does)

```bash
# 2.1  install deps once (fastapi, uvicorn, pydantic, PyYAML, numpy)
python3 -m pip install -r requirements.txt

# 2.2  generate the deterministic world: 90-day ledger, 201 merchants, benchmarks, seeded history
python3 -m datagen.generate

# 2.3  run the nightly SENSE job (fires the 4 signals; also available from the UI)
curl -X POST localhost:8642/v1/jobs/signals/scan     # after the server is up
#     — or in Python:
python3 -c "import sys; sys.path.insert(0,'.'); from backend.app.db import init_db; init_db(); from backend.app.signals import detectors; print([s['kind'] for s in detectors.scan(1042)])"

# 2.4  verify every deck number reproduces from data (40 assertions — CONSUMES the world)
python3 -m eval.verify_demo

# 2.5  regenerate the fresh Monday-09:15 state (verify_demo advances the clock)
python3 -m datagen.generate

# 2.6  serve
python3 -m uvicorn backend.app.main:app --port 8642
```

## 3. The demo, from the browser

Open **http://localhost:8642** — the dashboard boots on Monday morning with the signals fired.

1. Type or 🎙-speak: **कल बिक्री कम क्यों लगी?** → watch the tool calls stream, get the card.
2. Tap **✓ Bhejo** on the prescription card → one-tap approve + send.
3. Tap **⏩ Advance 7 days** → the world happens; **📊 Measure outcomes** closes the loop at ₹6,100 / 31 redemptions / 4.84× ROI.
4. Tap **🧾 Weekly report card** → 6 actions · ₹21,400 · 22% → 31%.
5. Optional: advance again (7 more days) + Measure to see the 30-day follow-on live.
6. Audit drawer (bottom right): click any row → the full trace of prompts, tool calls, policy checks.

## 4. Evals — the proof layer

```bash
python -m eval.verify_demo    # 40 assertions on every slide-9 number, from the ledger
python3 -m eval.run_eval      # 5 golden-set chat cases + §14 model gates (churn AUC, forecast, k-anonymity)
```

Both exit nonzero on any failure — CI-able.

## 5. Swap the brain (optional)

```bash
cp .env.example .env          # put ANTHROPIC_API_KEY=... in it (never in the repo)
VYAPAAR_LLM=claude ./run.sh   # same demo on claude-opus-5; scripted planner is the fallback
```

To use Sarvam for text answers as well as optional voice, add `SARVAM_API_KEY` to `.env` and run:

```bash
VYAPAAR_LLM=sarvam VYAPAAR_MODEL=sarvam-105b-conversations ./run.sh
```

The model receives structured results from the generated ledger, inventory snapshot and
benchmarks through the allowlisted tool registry. Without a working key or network, the
scripted planner answers from the same tools.

## 5.1 Sarvam voice (optional)

```bash
cp .env.example .env          # then add SARVAM_API_KEY in .env
./run.sh                       # dashboard uses Saaras STT + Bulbul TTS
curl localhost:8642/v1/voice/status
```

Without `SARVAM_API_KEY`, the dashboard keeps the browser voice fallback. The language dropdown
still changes browser speech recognition/synthesis and is passed to chat when Sarvam is enabled.

## 6. Useful API calls (server on :8642)

```bash
curl localhost:8642/v1/health                          # clock, provider, model
curl localhost:8642/v1/merchants/me/dashboard         # signals, pending approvals, brief
curl -X POST localhost:8642/v1/jobs/signals/scan      # nightly SENSE
curl -N -X POST localhost:8642/v1/chat \
     -H 'Content-Type: application/json' \
     -d '{"message":"kal bikri kam kyu lagi?"}'       # raw SSE stream of the agent loop
curl localhost:8642/v1/approvals/pending               # cards awaiting one-tap
curl localhost:8642/v1/runs/<run_id>/tracker           # state machine, arms, deliveries, outcome
curl localhost:8642/v1/report/weekly                  # the report card
curl -X POST localhost:8642/v1/demo/advance -H 'Content-Type: application/json' -d '{"days":7}'
curl -X POST localhost:8642/v1/jobs/measure           # the nightly MEASURE job
curl "localhost:8642/v1/audit?limit=40"               # the proof drawer
curl "localhost:8642/v1/audit?trace_id=<id>"          # one full trace
curl localhost:8642/docs                               # interactive API docs
```

## 7. Housekeeping / troubleshooting

```bash
# fully fresh world (deletes the DB and rebuilds)
rm -f vyapaar.db vyapaar.db-wal vyapaar.db-shm && python3 -m datagen.generate

# the demo state feels "used" (clock advanced, campaigns already sent) → regenerate
python3 -m datagen.generate

# port 8642 busy → kill the old server
pkill -f "uvicorn backend.app.main" && python3 -m uvicorn backend.app.main:app --port 8642

# secrets: .env is gitignored; .env.example documents every variable
# deterministic world: seed 20260917 (VYAPAAR_SEED) — same numbers every run
```

## 8. What to show if you only have 60 seconds

```bash
./run.sh          # then in the browser: ask the Hinglish question, tap Bhejo, Advance 7 days, report card
```
and the two one-liners that prove it isn't theatre:

```bash
python3 -m eval.verify_demo
python3 -m eval.run_eval
```
