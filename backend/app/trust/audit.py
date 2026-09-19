"""Append-only audit log (§15, principle #4).

Every prompt, tool call, tool result, LLM response, approval, action and policy decision
lands here with a trace_id, so any merchant dispute is answerable and any agent run is
reproducible. Nothing in this module ever updates or deletes a row.
"""
from __future__ import annotations

import uuid
from typing import Any

from ..db import connect, iso, jdump, jload, utcnow


def new_trace_id() -> str:
    return f"tr_{uuid.uuid4().hex[:16]}"


def log(
    kind: str,
    payload: Any,
    *,
    trace_id: str | None = None,
    merchant_id: int | None = None,
    run_id: str | None = None,
) -> int:
    cur = connect().execute(
        "INSERT INTO audit_log(ts, trace_id, merchant_id, run_id, kind, payload) VALUES (?,?,?,?,?,?)",
        (iso(utcnow()), trace_id, merchant_id, run_id, kind, jdump(payload)),
    )
    return int(cur.lastrowid or 0)


def trace(trace_id: str) -> list[dict[str, Any]]:
    rows = connect().execute(
        "SELECT id, ts, kind, run_id, payload FROM audit_log WHERE trace_id = ? ORDER BY id",
        (trace_id,),
    ).fetchall()
    return [
        {"id": r["id"], "ts": r["ts"], "kind": r["kind"], "run_id": r["run_id"], "payload": jload(r["payload"])}
        for r in rows
    ]


def recent(merchant_id: int, limit: int = 120) -> list[dict[str, Any]]:
    rows = connect().execute(
        "SELECT id, ts, trace_id, kind, run_id, payload FROM audit_log "
        "WHERE merchant_id = ? ORDER BY id DESC LIMIT ?",
        (merchant_id, limit),
    ).fetchall()
    return [
        {
            "id": r["id"], "ts": r["ts"], "trace_id": r["trace_id"],
            "kind": r["kind"], "run_id": r["run_id"], "payload": jload(r["payload"]),
        }
        for r in rows
    ]
