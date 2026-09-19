"""CSV ingestion for a single merchant workspace.

The importer accepts common POS/e-commerce column names and converts rows into the
same privacy-safe transaction ledger used by the agent. Raw customer identifiers
are HMAC-tokenized; they never enter prompts, audit logs, or the browser response.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .config import settings
from .db import connect, iso, jdump, tx
from .mkg import memory
from .trust import pii

MAX_BYTES = 3 * 1024 * 1024
MAX_ROWS = 25_000
_KEY = re.compile(r"[^a-z0-9]+")


class CsvImportError(ValueError):
    """A safe validation error suitable for a 4xx API response."""


@dataclass(frozen=True)
class ImportedRow:
    customer_tok: str
    amount_paise: int
    channel: str
    item: str | None
    event_time: str


def _normalise(value: str) -> str:
    return _KEY.sub("", value.lower())


def _pick(row: dict[str, str], aliases: tuple[str, ...]) -> str:
    for alias in aliases:
        value = row.get(alias, "").strip()
        if value:
            return value
    return ""


def _money(value: str) -> int | None:
    raw = value.strip().replace(",", "")
    raw = raw.replace("₹", "").replace("$", "").replace("£", "")
    raw = re.sub(r"[^0-9.\-]", "", raw)
    if not raw:
        return None
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return None
    if amount <= 0:
        return None
    return int(amount * 100)


def _quantity(value: str) -> Decimal | None:
    try:
        amount = Decimal(value.strip().replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None
    return amount if amount > 0 else None


def _timestamp(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
        "%d/%m/%Y %H:%M", "%d/%m/%Y", "%m/%d/%Y %H:%M", "%m/%d/%Y",
        "%d-%m-%Y", "%d-%m-%Y %H:%M:%S",
    ):
        try:
            return iso(datetime.strptime(value, fmt).replace(tzinfo=timezone.utc))
        except ValueError:
            continue
    return None


def _channel(value: str) -> str:
    value = value.lower().strip()
    if "upi" in value:
        return "upi"
    if "wallet" in value:
        return "wallet"
    if "sound" in value:
        return "soundbox"
    return "card"  # card is the neutral, non-contact fallback for cash/unknown POS data.


def _columns(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    if not rows:
        raise CsvImportError("The CSV has no transaction rows.")
    out: list[dict[str, str]] = []
    for raw in rows:
        out.append({_normalise(key): (value or "") for key, value in raw.items() if key})
    return out


def parse_csv(raw: bytes) -> tuple[list[ImportedRow], int]:
    if not raw:
        raise CsvImportError("Choose a non-empty CSV file.")
    if len(raw) > MAX_BYTES:
        raise CsvImportError("CSV is over 3 MB. Export a smaller date range (up to 25,000 rows).")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvImportError("Use a UTF-8 CSV file.") from exc
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise CsvImportError("The CSV needs a header row.")
    rows = _columns(list(reader))
    if len(rows) > MAX_ROWS:
        raise CsvImportError("CSV has more than 25,000 rows. Export a smaller date range.")

    imported: list[ImportedRow] = []
    skipped = 0
    for index, row in enumerate(rows, start=2):
        date = _timestamp(_pick(row, ("invoicedate", "orderdate", "date", "eventtime", "transactiondate")))
        total = _money(_pick(row, ("sales", "salesamount", "netsalesamount", "amount", "totalamount", "revenue")))
        if total is None:
            price = _money(_pick(row, ("unitprice", "price", "unitpriceinr")))
            qty = _quantity(_pick(row, ("quantity", "qty", "units")))
            total = int(Decimal(price) * qty) if price and qty else None
        if not date or not total:
            skipped += 1
            continue
        external_customer = _pick(row, ("customerid", "customer", "custid", "customeremail", "phone"))
        invoice = _pick(row, ("invoiceno", "invoice", "orderid", "order"))
        customer_tok = pii.tokenize("import:" + (external_customer or invoice or f"row-{index}"))
        item = _pick(row, ("description", "productname", "product", "item", "stockcode", "sku")) or None
        imported.append(ImportedRow(customer_tok, total, _channel(_pick(row, ("paymentmode", "paymentmethod", "channel"))), item, date))
    if not imported:
        raise CsvImportError("No valid rows found. Include a date and either a sales amount or quantity plus unit price.")
    return imported, skipped


def _rebuild_stats(conn: Any, merchant_id: int, now: datetime) -> None:
    conn.execute("DELETE FROM customer_merchant_stats WHERE merchant_id = ?", (merchant_id,))
    rows = conn.execute(
        "SELECT customer_tok, COUNT(*) AS visits, MIN(event_time) AS first_seen, MAX(event_time) AS last_seen, "
        "SUM(amount_paise) AS ltv FROM transactions WHERE merchant_id = ? GROUP BY customer_tok",
        (merchant_id,),
    ).fetchall()
    ranked = sorted(rows, key=lambda r: (int(r["ltv"]), int(r["visits"]), r["last_seen"]), reverse=True)
    deciles = {r["customer_tok"]: max(1, 10 - (i * 10 // max(1, len(ranked)))) for i, r in enumerate(ranked)}
    start90, start30 = iso(now - timedelta(days=90)), iso(now - timedelta(days=30))
    for r in rows:
        tok = r["customer_tok"]
        visits90 = conn.execute("SELECT COUNT(*) AS n FROM transactions WHERE merchant_id=? AND customer_tok=? AND event_time>=?", (merchant_id, tok, start90)).fetchone()["n"]
        visits30 = conn.execute("SELECT COUNT(*) AS n FROM transactions WHERE merchant_id=? AND customer_tok=? AND event_time>=?", (merchant_id, tok, start30)).fetchone()["n"]
        ltv, visits = int(r["ltv"]), int(r["visits"])
        days_lapsed = max(0, (now - datetime.strptime(r["last_seen"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).days)
        conn.execute(
            "INSERT INTO customer_merchant_stats(merchant_id,customer_tok,visits_lifetime,visits_90d,visits_30d,first_visit_at,last_visit_at,avg_ticket_paise,ltv_paise,churn_score,rfm_decile) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (merchant_id, tok, visits, int(visits90), int(visits30), r["first_seen"], r["last_seen"], round(ltv / visits), ltv, min(0.99, round(days_lapsed / 90, 3)), deciles[tok]),
        )
        conn.execute("INSERT OR IGNORE INTO customers(customer_tok,first_seen_at,segment) VALUES (?,?,?)", (tok, r["first_seen"], "imported"))


def replace_workspace(raw: bytes, merchant_name: str | None = None) -> dict[str, Any]:
    rows, skipped = parse_csv(raw)
    merchant_id = settings.demo_merchant_id
    latest = max(datetime.strptime(row.event_time, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) for row in rows)
    now = latest + timedelta(days=1)
    name = (merchant_name or "My business").strip()[:80] or "My business"
    conn = connect()
    with tx(conn):
        # A new import starts a clean analysis workspace. No other merchant is touched.
        conn.execute("DELETE FROM deliveries WHERE action_id IN (SELECT action_id FROM actions WHERE run_id IN (SELECT run_id FROM playbook_runs WHERE merchant_id=?))", (merchant_id,))
        conn.execute("DELETE FROM actions WHERE run_id IN (SELECT run_id FROM playbook_runs WHERE merchant_id=?)", (merchant_id,))
        conn.execute("DELETE FROM run_cohorts WHERE run_id IN (SELECT run_id FROM playbook_runs WHERE merchant_id=?)", (merchant_id,))
        conn.execute("DELETE FROM outcomes WHERE run_id IN (SELECT run_id FROM playbook_runs WHERE merchant_id=?)", (merchant_id,))
        conn.execute("DELETE FROM playbook_runs WHERE merchant_id=?", (merchant_id,))
        for table in ("transactions", "inventory_snapshots", "signals", "merchant_memory", "memory_episodes", "audit_log", "reminders"):
            conn.execute(f"DELETE FROM {table} WHERE merchant_id=?", (merchant_id,))
        conn.execute(
            "INSERT INTO merchants(merchant_id,name,vertical,city,pincode,open_hours,lang,phone_tok,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(merchant_id) DO UPDATE SET "
            "name=excluded.name,vertical=excluded.vertical,city=excluded.city,pincode=excluded.pincode," 
            "open_hours=excluded.open_hours,lang=excluded.lang",
            (merchant_id, name, "retail", "Imported data", "000000", "Imported CSV", "en", "imported-no-contact", iso(now)),
        )
        conn.executemany(
            "INSERT INTO transactions(merchant_id,customer_tok,amount_paise,channel,item,event_time,ingest_time,source_run_id) VALUES (?,?,?,?,?,?,?,NULL)",
            [(merchant_id, row.customer_tok, row.amount_paise, row.channel, row.item, row.event_time, iso(now)) for row in rows],
        )
        conn.execute("INSERT INTO clock(id,now) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET now=excluded.now", (iso(now),))
        _rebuild_stats(conn, merchant_id, now)
        memory.upsert_doc(merchant_id, {"profile": {"data_source": "merchant CSV import", "raw_customer_data": "tokenized before storage"}, "patterns": []})
    return {"imported": len(rows), "skipped": skipped, "as_of": iso(now), "merchant_name": name, "data_source": "csv"}
