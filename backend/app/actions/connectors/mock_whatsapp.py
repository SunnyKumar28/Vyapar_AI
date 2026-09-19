"""Mock WhatsApp Business API (§7 deviation note: sandbox connector, production shape).

In production this module is the BSP client. Its contract here is the same one the real
one has: a template send per recipient, per-recipient delivery events, and costs booked
on dispatch. PII is resolved at the LAST possible moment (detokenize inside this module
only) and what gets logged/audited is the masked number — the agent never sees a phone.

Delivery failures are simulated with a seeded rate so the demo shows an honest funnel:
sent -> delivered -> (some) failed, and only the delivered can redeem.
"""
from __future__ import annotations

import random
from datetime import timedelta
from typing import Any

from ...config import settings
from ...db import connect, demo_now, iso, jdump, q1
from ...trust import audit, pii


def send(run: dict[str, Any], audience: list[dict[str, Any]], action_id: str,
         *, trace_id: str | None = None) -> dict[str, int]:
    presc = run["prescription"]
    m = q1("SELECT name FROM merchants WHERE merchant_id = ?", (run["merchant_id"],))
    shop = m["name"] if m else "your shop"
    template = presc.get("copy_template", "")
    rng = random.Random(f"{settings.seed}:{action_id}")

    delivered = failed = 0
    conn = connect()
    for i, a in enumerate(audience):
        tok = a["customer_tok"]
        # Per-scenario delivery rate: the demo merchant's connector is a little imperfect,
        # the way a real BSP on a real network is. Deterministic per recipient.
        ok = rng.random() < 0.9623
        status = "delivered" if ok else "failed"
        at = iso(demo_now() + timedelta(seconds=2 + rng.randrange(600)))
        conn.execute(
            "INSERT OR IGNORE INTO deliveries(action_id, customer_tok, status, amount_paise, event_time)"
            " VALUES (?,?,?,?,?)",
            (action_id, tok, status, None, at))
        if ok:
            # Rendering the copy happens here, connector-side, on the real identity —
            # and only the masked number ever reaches the audit log.
            vault = pii.detokenize(tok)
            name = (vault or {}).get("name", " customers ji")
            first = name.split("-")[0].capitalize() if vault else "Janaab"
            _preview = template.format(name=first, shop=shop) if template and "{" in template else template
            audit.log("action", {"connector": "whatsapp", "action_id": action_id,
                                "customer_tok": tok, "status": "delivered",
                                "phone": pii.mask_phone((vault or {}).get("phone", "")),
                                "message_lang": presc.get("lang", "hi")},
                      trace_id=trace_id, merchant_id=run["merchant_id"], run_id=run["run_id"])
            delivered += 1
        else:
            audit.log("action", {"connector": "whatsapp", "action_id": action_id,
                                "customer_tok": tok, "status": "failed",
                                "reason": "invalid number (simulated BSP reject)"},
                      trace_id=trace_id, merchant_id=run["merchant_id"], run_id=run["run_id"])
            failed += 1
    return {"delivered": delivered, "failed": failed}
