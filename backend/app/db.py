"""SQLite store implementing the §11 production DDL.

Deliberate deviation from §12 (Postgres + pgvector): no Docker on the demo box, so the
schema below is the §11 DDL with three mechanical substitutions —

    Postgres            SQLite (here)
    ---------------     -----------------------------------------
    BIGSERIAL PK        INTEGER PRIMARY KEY AUTOINCREMENT
    TIMESTAMPTZ         TEXT (ISO-8601 UTC, lexically sortable)
    JSONB               TEXT (json.dumps; read back through jload)

Column names, keys, constraints and semantics are unchanged, so principle #7
("MVP schemas = production schemas") holds: going to Postgres is a driver swap.
"""
from __future__ import annotations

import json
import sqlite3
import shutil
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import ROOT, settings

_local = threading.local()

DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS merchants (
  merchant_id   INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  vertical      TEXT NOT NULL,
  city          TEXT NOT NULL,
  pincode       TEXT NOT NULL,
  open_hours    TEXT NOT NULL,
  lang          TEXT NOT NULL DEFAULT 'hi',
  phone_tok     TEXT NOT NULL,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
  customer_tok  TEXT PRIMARY KEY,          -- tokenized; raw PII never lands here
  first_seen_at TEXT NOT NULL,
  segment       TEXT NOT NULL DEFAULT 'unknown'
);

CREATE TABLE IF NOT EXISTS transactions (
  txn_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  merchant_id   INTEGER NOT NULL REFERENCES merchants(merchant_id),
  customer_tok  TEXT    NOT NULL,
  amount_paise  INTEGER NOT NULL CHECK (amount_paise > 0),
  channel       TEXT    NOT NULL CHECK (channel IN ('upi','card','wallet','soundbox')),
  item          TEXT,
  event_time    TEXT    NOT NULL,          -- when it happened at the shop
  ingest_time   TEXT    NOT NULL,          -- when we saw it (late data is measurable)
  source_run_id TEXT                       -- set when the txn came from a campaign redemption
);
CREATE INDEX IF NOT EXISTS ix_txn_merchant_time ON transactions(merchant_id, event_time);
CREATE INDEX IF NOT EXISTS ix_txn_customer      ON transactions(merchant_id, customer_tok);

CREATE TABLE IF NOT EXISTS customer_merchant_stats (   -- the RFM edge of the MKG
  merchant_id      INTEGER NOT NULL,
  customer_tok     TEXT    NOT NULL,
  visits_lifetime  INTEGER NOT NULL DEFAULT 0,
  visits_90d       INTEGER NOT NULL DEFAULT 0,
  visits_30d       INTEGER NOT NULL DEFAULT 0,
  first_visit_at   TEXT,
  last_visit_at    TEXT,
  avg_ticket_paise INTEGER NOT NULL DEFAULT 0,
  ltv_paise        INTEGER NOT NULL DEFAULT 0,
  churn_score      REAL    NOT NULL DEFAULT 0.0,
  rfm_decile       INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (merchant_id, customer_tok)
);

CREATE TABLE IF NOT EXISTS inventory_snapshots (        -- generated operational context
  inventory_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  merchant_id        INTEGER NOT NULL REFERENCES merchants(merchant_id),
  item               TEXT NOT NULL,
  units_sold_7d      INTEGER NOT NULL DEFAULT 0,
  units_on_hand      INTEGER NOT NULL DEFAULT 0,
  reorder_point_units INTEGER NOT NULL DEFAULT 0,
  unit_cost_paise    INTEGER NOT NULL DEFAULT 0,
  supplier           TEXT NOT NULL,
  restock_days       INTEGER NOT NULL DEFAULT 1,
  status             TEXT NOT NULL,                    -- healthy|reorder_soon|at_risk
  as_of              TEXT NOT NULL,
  UNIQUE (merchant_id, item, as_of)
);
CREATE INDEX IF NOT EXISTS ix_inventory_merchant_asof
  ON inventory_snapshots(merchant_id, as_of);

CREATE TABLE IF NOT EXISTS benchmarks (                -- anonymised vertical x pincode aggregates
  vertical            TEXT NOT NULL,
  pincode             TEXT NOT NULL,
  repeat_rate         REAL NOT NULL,
  avg_daily_gmv_paise INTEGER NOT NULL,
  avg_ticket_paise    INTEGER NOT NULL,
  cohort_size         INTEGER NOT NULL,   -- k-anonymity guard; never expose below k
  computed_at         TEXT NOT NULL,
  PRIMARY KEY (vertical, pincode)
);

CREATE TABLE IF NOT EXISTS merchant_memory (           -- the MKG RAG payload (§3.3)
  merchant_id  INTEGER PRIMARY KEY REFERENCES merchants(merchant_id),
  doc          TEXT NOT NULL,            -- JSONB
  embedding    TEXT NOT NULL,            -- JSON float list (pgvector in prod)
  updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_episodes (           -- episodic memory, individually retrievable
  episode_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  merchant_id INTEGER NOT NULL,
  occurred_on TEXT NOT NULL,
  event       TEXT NOT NULL,
  result      TEXT NOT NULL,             -- JSONB
  embedding   TEXT NOT NULL,
  run_id      TEXT
);

CREATE TABLE IF NOT EXISTS signals (
  signal_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  merchant_id INTEGER NOT NULL REFERENCES merchants(merchant_id),
  kind        TEXT NOT NULL,             -- sales_anomaly|churn_cluster|benchmark_gap|stockout|festive|collections
  severity    REAL NOT NULL DEFAULT 0.0,
  payload     TEXT NOT NULL,             -- JSONB: numbers + evidence, tool-ready
  detected_at TEXT NOT NULL,
  UNIQUE (merchant_id, kind, detected_at)
);

CREATE TABLE IF NOT EXISTS playbook_runs (
  run_id       TEXT PRIMARY KEY,         -- UUID; doubles as the connector idempotency key
  merchant_id  INTEGER NOT NULL REFERENCES merchants(merchant_id),
  playbook     TEXT NOT NULL,
  signal_id    INTEGER REFERENCES signals(signal_id),
  state        TEXT NOT NULL,            -- §6 state machine
  prescription TEXT NOT NULL,            -- JSONB: copy, audience, impact range, cost
  approved_by  TEXT,
  approved_at  TEXT,
  dismissed_reason TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_runs_merchant_state ON playbook_runs(merchant_id, state);

CREATE TABLE IF NOT EXISTS actions (
  action_id       TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL REFERENCES playbook_runs(run_id),
  connector       TEXT NOT NULL,          -- whatsapp|offer|payment_link|push
  idempotency_key TEXT UNIQUE NOT NULL,   -- retries cannot double-send
  status          TEXT NOT NULL,          -- queued|sent|delivered|redeemed|failed
  audience_size   INTEGER NOT NULL DEFAULT 0,
  cost_paise      INTEGER NOT NULL DEFAULT 0,
  payload         TEXT NOT NULL,
  external_ref    TEXT,
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (                -- per-recipient connector events
  delivery_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  action_id    TEXT NOT NULL REFERENCES actions(action_id),
  customer_tok TEXT NOT NULL,
  status       TEXT NOT NULL,             -- queued|sent|delivered|read|redeemed|failed
  amount_paise INTEGER,
  event_time   TEXT NOT NULL,
  UNIQUE (action_id, customer_tok, status)
);

CREATE TABLE IF NOT EXISTS run_cohorts (               -- treatment / control membership for DiD (§8)
  run_id       TEXT NOT NULL REFERENCES playbook_runs(run_id),
  customer_tok TEXT NOT NULL,
  arm          TEXT NOT NULL CHECK (arm IN ('treatment','control')),
  rfm_decile   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, customer_tok)
);

CREATE TABLE IF NOT EXISTS outcomes (
  outcome_id           INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id               TEXT NOT NULL REFERENCES playbook_runs(run_id),
  window_d             INTEGER NOT NULL,  -- 7 | 30
  gmv_influenced_paise INTEGER,
  redemptions          INTEGER,
  delivered            INTEGER,
  cost_paise           INTEGER,
  roi                  REAL,
  control_gmv_paise    INTEGER,
  treatment_gmv_paise  INTEGER,
  n_treatment          INTEGER,
  n_control            INTEGER,
  confidence           TEXT,              -- measured | indicative (small audience, no control)
  detail               TEXT NOT NULL,     -- JSONB
  computed_at          TEXT NOT NULL,
  UNIQUE (run_id, window_d)
);

CREATE TABLE IF NOT EXISTS audit_log (                 -- append-only: prompts, tool I/O, approvals, actions
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,
  trace_id    TEXT,
  merchant_id INTEGER,
  run_id      TEXT,
  kind        TEXT NOT NULL,              -- prompt|tool_call|tool_result|llm_response|approval|action|policy|job
  payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_trace ON audit_log(trace_id);
CREATE INDEX IF NOT EXISTS ix_audit_merchant ON audit_log(merchant_id, id);

CREATE TABLE IF NOT EXISTS conversations (
  session_id  TEXT PRIMARY KEY,
  merchant_id INTEGER NOT NULL,
  started_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  message_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  TEXT NOT NULL REFERENCES conversations(session_id),
  role        TEXT NOT NULL,              -- user | assistant
  text        TEXT NOT NULL,
  lang        TEXT,
  card        TEXT,                        -- JSONB prescription card, if any
  ts          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
  reminder_id INTEGER PRIMARY KEY AUTOINCREMENT,
  merchant_id INTEGER NOT NULL,
  note        TEXT NOT NULL,
  due_at      TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clock (                     -- demo "now": the datagen's as-of instant
  id  INTEGER PRIMARY KEY CHECK (id = 1),
  now TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def jdump(value: Any) -> str:
    # sort_keys keeps rows byte-stable, which matters for prompt caching and diffing.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def jload(raw: str | None) -> Any:
    return json.loads(raw) if raw else None


def connect(path: Path | None = None) -> sqlite3.Connection:
    """One connection per thread; uvicorn runs sync endpoints in a threadpool."""
    target = str(path or settings.db_path)
    cached = getattr(_local, "conn", None)
    if cached is not None and getattr(_local, "path", None) == target:
        return cached
    conn = sqlite3.connect(target, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    _local.conn = conn
    _local.path = target
    return conn


@contextmanager
def tx(conn: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
    c = conn or connect()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
    except Exception:
        c.execute("ROLLBACK")
        raise
    else:
        c.execute("COMMIT")


def init_db(path: Path | None = None) -> sqlite3.Connection:
    target = path or settings.db_path
    bundled = ROOT / "vyapaar.db"
    if target != bundled and bundled.exists() and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundled, target)
    conn = connect(target)
    conn.executescript(DDL)
    # Git deployments do not include the developer's ignored SQLite seed file. A
    # fresh serverless instance must still render an import-ready workspace.
    if not conn.execute("SELECT 1 FROM merchants WHERE merchant_id = ?", (settings.demo_merchant_id,)).fetchone():
        conn.execute(
            "INSERT OR IGNORE INTO merchants(merchant_id,name,vertical,city,pincode,open_hours,lang,phone_tok,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (settings.demo_merchant_id, "Import your data", "retail", "Your workspace", "000000", "Upload a CSV", "en", "import-ready", iso(utcnow())),
        )
    return conn


def q(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, tuple(params)).fetchall()


def q1(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return connect().execute(sql, tuple(params)).fetchone()


def demo_now() -> datetime:
    """The clock the whole system reads. Pinned by datagen so the demo is reproducible."""
    row = q1("SELECT now FROM clock WHERE id = 1")
    return parse(row["now"]) if row else utcnow()
