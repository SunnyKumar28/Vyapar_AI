"""PII tokenization gateway (§15).

Raw names/phones are tokenized *before* anything reaches the LLM. The agent, the MKG and
every prompt see only `cust_xxxxxxxxxxxx`. Only connectors call `detokenize`, at send time.
In production the vault is a KMS-backed service; here it is a table the agent cannot read.
"""
from __future__ import annotations

import hashlib
import hmac
import sqlite3

from ..config import settings
from ..db import connect

_VAULT_DDL = """
CREATE TABLE IF NOT EXISTS pii_vault (
  customer_tok TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  phone        TEXT NOT NULL
);
"""


_ready = False


def ensure_vault() -> None:
    """Idempotent, and called BEFORE any transaction opens: sqlite3.executescript would
    COMMIT the caller's transaction as a side effect, which corrupts datagen's big inserts."""
    global _ready
    if not _ready:
        connect().executescript(_VAULT_DDL)
        _ready = True


def tokenize(phone: str) -> str:
    digest = hmac.new(settings.pii_secret.encode(), phone.encode(), hashlib.sha256).hexdigest()
    return f"cust_{digest[:12]}"


def store(phone: str, name: str) -> str:
    tok = tokenize(phone)
    ensure_vault()          # no-op after the first call (see note above)
    connect().execute(
        "INSERT OR REPLACE INTO pii_vault(customer_tok, name, phone) VALUES (?,?,?)",
        (tok, name, phone),
    )
    return tok


def detokenize(tok: str) -> dict[str, str] | None:
    """Connector-only. Never called from agent code or from anything that builds a prompt."""
    try:
        row: sqlite3.Row | None = connect().execute(
            "SELECT name, phone FROM pii_vault WHERE customer_tok = ?", (tok,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return {"name": row["name"], "phone": row["phone"]} if row else None


def mask_phone(phone: str) -> str:
    return f"{phone[:3]}…{phone[-2:]}" if len(phone) > 5 else "…"


def scrub(text: str) -> str:
    """Belt-and-braces: strip anything phone-shaped out of LLM-bound text."""
    import re

    return re.sub(r"\b(?:\+?91[\-\s]?)?[6-9]\d{9}\b", "[phone-redacted]", text)
