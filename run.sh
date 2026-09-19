#!/usr/bin/env bash
# Vyapaar AI — one-command demo. Regenerates the world, verifies every deck number,
# then serves the dashboard at http://localhost:8642
set -euo pipefail
cd "$(dirname "$0")"

# Prefer the project virtualenv so this script also works from a fresh terminal
# where `.venv` has not been activated. This avoids Homebrew's PEP 668 guard.
if [[ -x ".venv/bin/python" ]]; then
  PYTHON_BIN=".venv/bin/python"
else
  PYTHON_BIN="$(command -v python3)"
fi

echo "▸ Installing deps (fastapi, uvicorn, pydantic, PyYAML, numpy)…"
"$PYTHON_BIN" -m pip install -q -r requirements.txt

echo "▸ Generating the deterministic world (90d ledger, 201 merchants, benchmarks)…"
"$PYTHON_BIN" -m datagen.generate

echo "▸ Verifying every slide-9 number reproduces from data…"
# Keep the deterministic proof run offline even when the interactive server is configured
# to use Sarvam; the verification checks the data/tool contract, not provider availability.
VYAPAAR_LLM=scripted "$PYTHON_BIN" -m eval.verify_demo | tail -6

echo "▸ Verify consumed the world (it runs the whole loop) — regenerating the fresh Monday-morning state…"
"$PYTHON_BIN" -m datagen.generate && "$PYTHON_BIN" -c "
import sys; sys.path.insert(0,'.')
from backend.app.db import init_db, q
init_db()
from backend.app.signals import detectors
fired = detectors.scan(1042)
print('  fresh world ready:', len(fired), 'signals fired, clock back to Monday 09:15 IST')"

echo "▸ Booting the copilot at http://localhost:8642  (Ctrl-C to stop)"
echo "  demo script: docs/DEMO_SCRIPT.md · API docs: /docs · eval: python -m eval.run_eval"
exec "$PYTHON_BIN" -m uvicorn backend.app.main:app --port 8642
