"""The ReAct loop (§4). Max 8 steps, every hop audited, SSE events out.

Control flow lives HERE, in code — not in the model. The brain proposes one action at a
time (tool call or final answer); the loop validates, executes, observes, repeats. A
money-touching tool still cannot send anything without an approved run, and a failed
step becomes a tool_result the brain can see, not a crash.

Every number the merchant is shown crossed this loop with a trace id attached. That is
the demo's audit drawer: click any figure, see the tool call that produced it.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from ..config import settings
from ..db import connect, demo_now, iso, jdump, tx
from ..trust import audit
from . import llm, prompts, tools

Event = Callable[[dict[str, Any]], None]


def run_chat(merchant_id: int, message: str, session_id: str | None = None,
             on_event: Event | None = None, *, lang: str | None = None) -> dict[str, Any]:
    """One turn. Returns the full result; streams SSE events if on_event is given."""
    session = session_id or f"sess_{uuid.uuid4().hex[:12]}"
    trace = audit.new_trace_id()
    emit = on_event or (lambda e: None)

    def safe_emit(evt: dict[str, Any]) -> None:
        emit(evt)

    system = prompts.system_prompt(merchant_id, language_code=lang)
    now = iso(demo_now())

    with tx() as conn:
        previous = conn.execute(
            "SELECT role, text FROM messages WHERE session_id=? ORDER BY message_id DESC LIMIT 12",
            (session,),
        ).fetchall()
        conn.execute("INSERT OR IGNORE INTO conversations(session_id, merchant_id, started_at)"
                     " VALUES (?,?,?)", (session, merchant_id, now))
        conn.execute("INSERT INTO messages(session_id, role, text, lang, ts) VALUES (?,?,?,?,?)",
                     (session, "user", message, lang, now))
    history = [
        {"role": row["role"], "content": row["text"]}
        for row in reversed(previous)
        if row["role"] in ("user", "assistant") and row["text"]
    ]
    brain = llm.make_brain(merchant_id, history=history)
    audit.log("prompt", {"provider": brain.NAME, "system_len": len(system),
                         "user": message, "session": session, "language_code": lang},
              trace_id=trace, merchant_id=merchant_id)
    safe_emit({"type": "trace", "trace_id": trace, "provider": brain.NAME,
               "session_id": session, "message": message})

    steps: list[dict[str, Any]] = []
    card: dict[str, Any] | None = None
    final_text = ""
    error_text = ""

    for step in range(settings.agent.max_steps):
        action = brain.next_action(system=system, user_msg=message)
        if action.kind == "final":
            final_text = action.text
            break
        if action.kind == "error":
            error_text = action.text
            break
        # ---- a tool call: allowlist + validate + execute + audit + observe ----
        safe_emit({"type": "tool_call", "step": step + 1, "tool": action.tool,
                   "args": {k: v for k, v in action.args.items() if not k.startswith("_")}})
        try:
            result = tools.call(action.tool, action.args, trace_id=trace,
                               merchant_id=merchant_id)
            ok = True
        except Exception as exc:
            result = {"error": str(exc)[:300]}
            ok = False
            audit.log("tool_result", {"tool": action.tool, "error": str(exc)[:300]},
                      trace_id=trace, merchant_id=merchant_id)
        brain.observe(action.tool, result if ok else {}, ok)
        steps.append({"step": step + 1, "tool": action.tool, "ok": ok,
                      "summary": _summarize(action.tool, result)})
        safe_emit({"type": "tool_result", "step": step + 1, "tool": action.tool, "ok": ok,
                   "summary": steps[-1]["summary"]})
        if ok and action.tool == "request_approval" and isinstance(result, dict):
            card = result
            safe_emit({"type": "card", "card": {
                "run_id": result["run_id"],
                "approval_token": result["approval_token"],
                "prescription": result["prescription"],
            }})
    else:
        final_text = ("Bahut zyada steps lag gaye — main thoda ruk kar dobara try karta hoon. "
                      "(Safety cap: 8 tool steps per turn.)")

    if error_text and not final_text:
        # The scripted planner is the fallback for a dead model path (principle #5).
        # Run its complete plan so a temporary Sarvam outage still answers the question.
        fallback = llm.ScriptedLLM(merchant_id)
        for fallback_step in range(settings.agent.max_steps):
            fb = fallback.next_action(system=system, user_msg=message)
            if fb.kind == "final":
                final_text = fb.text
                break
            if fb.kind == "error":
                break
            safe_emit({"type": "tool_call", "step": len(steps) + 1,
                       "tool": fb.tool, "args": fb.args})
            try:
                result = tools.call(fb.tool, fb.args, trace_id=trace, merchant_id=merchant_id)
                ok = True
            except Exception as exc:
                result = {"error": str(exc)[:300]}
                ok = False
            fallback.observe(fb.tool, result if ok else {}, ok)
            steps.append({"step": len(steps) + 1, "tool": fb.tool, "ok": ok,
                          "summary": _summarize(fb.tool, result)})
            safe_emit({"type": "tool_result", "step": len(steps), "tool": fb.tool,
                       "ok": ok, "summary": steps[-1]["summary"]})
            if ok and fb.tool == "request_approval" and isinstance(result, dict):
                card = result
                safe_emit({"type": "card", "card": {
                    "run_id": result["run_id"],
                    "approval_token": result["approval_token"],
                    "prescription": result["prescription"],
                }})
        if not final_text:
            final_text = "Model abhi uplabdh nahi hai — thodi der baad try karein."
        audit.log("llm_response", {"fallback_used": True, "error": error_text[:200]},
                  trace_id=trace, merchant_id=merchant_id)

    with tx() as conn:
        conn.execute("INSERT INTO messages(session_id, role, text, lang, card, ts) "
                     "VALUES (?,?,?,?,?,?)",
                     (session, "assistant", final_text, lang,
                      jdump(card["prescription"]) if card else None, iso(demo_now())))
    audit.log("llm_response", {"final_text": final_text, "steps": steps,
                               "card_run": card["run_id"] if card else None},
              trace_id=trace, merchant_id=merchant_id,
              run_id=card["run_id"] if card else None)
    safe_emit({"type": "final", "text": final_text, "trace_id": trace,
               "steps": steps})
    return {"session_id": session, "trace_id": trace, "text": final_text,
            "steps": steps, "card": card, "provider": brain.NAME}


def _summarize(tool: str, result: Any) -> str:
    """One-line digest for the UI's audit drawer — a judge can read these at a glance."""
    if not isinstance(result, dict):
        return str(result)[:120]
    if "error" in result:
        return f"error: {result['error']}"
    if tool == "get_merchant_brief":
        s = result.get("sales_30d", {})
        return (f"30d GMV {s.get('gmv_paise', 0):,}p · repeat "
                f"{result.get('repeat_rate_30d', {}).get('repeat_rate', 0):.0%} · "
                f"regulars {result.get('customers', {}).get('regulars', 0)}")
    if tool == "detect_anomalies":
        hits = result.get("anomalies", [])
        if not hits:
            return "no anomalies"
        a = hits[-1]
        return f"{a['date']} {a['dow']}: {a['delta_pct']:+.1%} vs {a['dow']} baseline (z={a['z_score']})"
    if tool == "forecast_sales":
        return (f"next {result.get('horizon_days')}d {result.get('next_period_total_paise', 0):,}p · "
                f"dip expected={result.get('dip_expected')} · model {result.get('model')}")
    if tool == "list_lapsed_customers":
        return (f"{result.get('count', 0)} lapsed regulars · avg ticket "
                f"{result.get('avg_ticket_paise', 0):,}p")
    if tool == "get_store_insights":
        risk = result.get("at_risk_items") or []
        top = (result.get("top_items") or [{}])[0]
        return (f"top item {top.get('item', 'n/a')} · peak hours "
                f"{len(result.get('peak_hours') or [])} · inventory at risk "
                f"{', '.join(x.get('item', '') for x in risk) or 'none'}")
    if tool == "estimate_impact":
        band = result.get("gmv_influenced_band_paise", [0, 0])
        return (f"band {band[0]:,}–{band[1]:,}p · cost cap {result.get('cost_cap_paise', 0):,}p · "
                f"model {result.get('model')}")
    if tool == "request_approval":
        p = result.get("prescription", {})
        return (f"run {result.get('run_id', '')[:20]} · {p.get('playbook')} · "
                f"audience {p.get('audience_size')} · awaiting merchant approval")
    if tool == "execute_action":
        return f"delivered {result.get('delivered')} · cost {result.get('cost_paise', 0):,}p"
    if tool == "get_action_status":
        return f"state {result.get('state')}"
    return json.dumps(result, default=str)[:120]
