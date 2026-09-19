"""Approval service (§15). Every money- or customer-touching action needs an explicit,
logged approval carrying a signed token, so an approval cannot be replayed or forged."""
from __future__ import annotations

import hashlib
import hmac
from typing import Any

from ..config import settings
from ..db import connect, demo_now, iso, jdump, jload, q1
from . import audit


def sign(run_id: str, state: str = "awaiting_approval") -> str:
    msg = f"{run_id}:{state}".encode()
    return hmac.new(settings.approval_secret.encode(), msg, hashlib.sha256).hexdigest()[:32]


def verify(run_id: str, token: str, state: str = "awaiting_approval") -> bool:
    return hmac.compare_digest(sign(run_id, state), token or "")


def pending(merchant_id: int) -> list[dict[str, Any]]:
    rows = connect().execute(
        "SELECT run_id, playbook, prescription, created_at FROM playbook_runs "
        "WHERE merchant_id = ? AND state = 'awaiting_approval' ORDER BY created_at DESC",
        (merchant_id,),
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "run_id": r["run_id"],
            "playbook": r["playbook"],
            "created_at": r["created_at"],
            "approval_token": sign(r["run_id"]),
            "prescription": jload(r["prescription"]),
        })
    return out


def record(run_id: str, decision: str, actor: str, *, reason: str | None = None, trace_id: str | None = None) -> None:
    row = q1("SELECT merchant_id FROM playbook_runs WHERE run_id = ?", (run_id,))
    audit.log(
        "approval",
        {"run_id": run_id, "decision": decision, "actor": actor, "reason": reason, "at": iso(demo_now())},
        trace_id=trace_id,
        merchant_id=int(row["merchant_id"]) if row else None,
        run_id=run_id,
    )
    _ = jdump  # kept for symmetry with other trust modules
