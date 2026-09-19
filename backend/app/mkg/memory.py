"""Merchant memory: the brief the agent reads first, and the episodic store it learns from (§3.3).

Two layers, both part of the Merchant Knowledge Graph:

  merchant_memory   one JSON doc per merchant — the durable profile ("Sundays are the big day",
                    "10% off worked, free-item didn't"). Cheap to read, goes into every prompt.
  memory_episodes   one row per thing that happened, individually retrievable by similarity.

Production uses pgvector. Here `embed` is a hashed bag-of-words projection: no dependency, no
download, deterministic, and good enough to rank a few hundred episodes. Same call signature as
an embedding service, so swapping it out touches this file only.
"""
from __future__ import annotations

import hashlib
import math
import re
from datetime import timedelta
from typing import Any

from ..config import settings
from ..db import connect, demo_now, iso, jdump, jload, q, q1
from . import repositories as repo

DIM = 256
_WORD = re.compile(r"[a-z0-9₹%]+")


def embed(text: str) -> list[float]:
    """Hashed bag-of-words, L2-normalised. Stand-in for an embedding model (§12 deviation)."""
    vec = [0.0] * DIM
    for word in _WORD.findall(text.lower()):
        h = hashlib.blake2b(word.encode(), digest_size=8).digest()
        idx = int.from_bytes(h[:4], "big") % DIM
        sign = 1.0 if h[4] & 1 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


# ---------------------------------------------------------------- durable doc

def upsert_doc(merchant_id: int, doc: dict[str, Any]) -> None:
    text = " ".join(f"{k} {v}" for k, v in doc.items())
    connect().execute(
        "INSERT INTO merchant_memory(merchant_id, doc, embedding, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(merchant_id) DO UPDATE SET doc=excluded.doc, embedding=excluded.embedding, "
        "updated_at=excluded.updated_at",
        (merchant_id, jdump(doc), jdump(embed(text)), iso(demo_now())),
    )


def doc(merchant_id: int) -> dict[str, Any]:
    r = q1("SELECT doc FROM merchant_memory WHERE merchant_id = ?", (merchant_id,))
    return jload(r["doc"]) if r else {}


def learn(merchant_id: int, key: str, value: Any) -> dict[str, Any]:
    """Write one fact back into the doc. This is the MEASURE→SENSE edge of the loop."""
    d = doc(merchant_id)
    d.setdefault("learned", {})[key] = value
    upsert_doc(merchant_id, d)
    return d


# ---------------------------------------------------------------- episodes

def append_episode(merchant_id: int, *, occurred_on: str, event: str,
                   result: dict[str, Any], run_id: str | None = None) -> int:
    cur = connect().execute(
        "INSERT INTO memory_episodes(merchant_id, occurred_on, event, result, embedding, run_id) "
        "VALUES (?,?,?,?,?,?)",
        (merchant_id, occurred_on, event, jdump(result),
         jdump(embed(f"{event} {jdump(result)}")), run_id),
    )
    return int(cur.lastrowid)


def recall(merchant_id: int, query: str, k: int = 3) -> list[dict[str, Any]]:
    """Top-k episodes by cosine similarity. The retrieval half of the MKG RAG (§3.3)."""
    qv = embed(query)
    scored = []
    for r in q("SELECT episode_id, occurred_on, event, result, embedding, run_id "
               "FROM memory_episodes WHERE merchant_id = ? ORDER BY episode_id", (merchant_id,)):
        scored.append((cosine(qv, jload(r["embedding"])), {
            "episode_id": int(r["episode_id"]), "occurred_on": r["occurred_on"],
            "event": r["event"], "result": jload(r["result"]), "run_id": r["run_id"],
        }))
    scored.sort(key=lambda p: (-p[0], p[1]["episode_id"]))
    return [{**e, "similarity": round(s, 3)} for s, e in scored[:k]]


# ---------------------------------------------------------------- the brief

def brief(merchant_id: int, *, recall_query: str | None = None) -> dict[str, Any]:
    """Everything the agent needs before it reasons. Backs the `get_merchant_brief` tool (§4.2).

    Every number here is read from the MKG. Nothing is estimated, and nothing is formatted —
    the caller renders paise, the model never does arithmetic on it.
    """
    m = repo.merchant(merchant_id)
    if not m:
        return {"error": f"merchant {merchant_id} not found"}

    now = demo_now()
    start30, end30 = repo.window(30, now)
    gmv30 = repo.gmv_between(merchant_id, start30, end30)
    txns30 = q1("SELECT COUNT(*) AS n FROM transactions WHERE merchant_id = ? "
                "AND event_time >= ? AND event_time < ?",
                (merchant_id, iso(start30), iso(end30)))
    n30 = int(txns30["n"]) if txns30 else 0
    counts = repo.regulars_count(merchant_id)
    rr = repo.repeat_rate(merchant_id, 30, now)
    bench = repo.benchmark(m["vertical"], m["pincode"])
    d = doc(merchant_id)

    runs = [
        {"run_id": r["run_id"], "playbook": r["playbook"], "state": r["state"],
         "created_at": r["created_at"]}
        for r in repo.runs_for(merchant_id, limit=6)
    ]
    return {
        "as_of": iso(now),
        "merchant": {k: m[k] for k in ("merchant_id", "name", "vertical", "city", "pincode",
                                       "open_hours", "lang")},
        "sales_30d": {
            "gmv_paise": gmv30,
            "avg_daily_gmv_paise": round(gmv30 / 30),
            "txns": n30,
            "avg_ticket_paise": round(gmv30 / n30) if n30 else 0,
            "window": [iso(start30), iso(end30)],
        },
        "repeat_rate_30d": rr,
        "customers": {
            "regulars": counts["regulars"],
            "lapsed_regulars": counts["lapsed"],
            "distinct_30d": rr["distinct_customers"],
            "definition": f">= {counts['min_visits']} lifetime visits; lapsed = no visit in "
                          f"{counts['gap_days']} days",
        },
        "top_items": repo.top_items(merchant_id, 30),
        "benchmark": bench,
        "open_signals": repo.open_signals(merchant_id, 5),
        "recent_runs": runs,
        "memory": {
            "profile": d.get("profile", {}),
            "patterns": d.get("patterns", []),
            "learned": d.get("learned", {}),
            "episodes": recall(merchant_id, recall_query, 3) if recall_query else [],
        },
    }
