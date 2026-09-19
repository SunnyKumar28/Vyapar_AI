"""Vyapaar AI — FastAPI surface (§10) plus the demo controls.

Everything a judge needs is behind these endpoints:
  POST /v1/chat                      SSE: the agent loop, every tool call streamed live
  GET  /v1/merchants/me/dashboard   signals, pending approvals, guardrail state
  POST /v1/approvals/{id}/approve   the one-tap yes (signed token)
  POST /v1/approvals/{id}/reject
  GET  /v1/runs/{id}/tracker        state machine + deliveries + outcome
  GET  /v1/report/weekly            the report card
  POST /v1/jobs/signals/scan        the nightly SENSE job
  POST /v1/jobs/measure             the nightly MEASURE job
  POST /v1/webhooks/whatsapp        connector events (delivery/read/redeem)
  POST /v1/demo/advance             demo clock control (labelled as such in the UI)
  GET  /v1/audit?trace_id=...       the proof drawer: every prompt and tool I/O

The dashboard is a zero-build SPA served from /dashboard — same API a real client would
call, no bundler, no npm.
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import SARVAM_LANGUAGES, settings
from .db import connect, demo_now, init_db, iso, jdump, jload, q, q1, tx
from .mkg import memory, repositories as repo
from .trust import policy as policy_engine
from .signals import detectors
from .playbooks import engine
from .outcomes import attribution, report_card
from .trust import approvals, audit
from .actions.connectors import world_sim
from . import voice

app = FastAPI(title="Vyapaar AI", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

init_db()


def _mid() -> int:
    return settings.demo_merchant_id


# ------------------------------------------------------------------ chat (SSE)

class ChatIn(BaseModel):
    message: str
    session_id: str | None = None
    language_code: str | None = None


class VoiceTextIn(BaseModel):
    text: str
    language_code: str | None = None


@app.get("/v1/voice/status")
def voice_status() -> dict[str, Any]:
    return voice.status()


@app.post("/v1/voice/transcribe")
async def voice_transcribe(request: Request) -> dict[str, Any]:
    if not voice.enabled():
        raise HTTPException(503, "Sarvam voice is not configured; use browser voice input")
    audio = await request.body()
    if not audio:
        raise HTTPException(400, "Audio file is empty")
    if len(audio) > 12 * 1024 * 1024:
        raise HTTPException(413, "Audio recording is too large")
    try:
        text = voice.transcribe(
            audio,
            request.headers.get("x-audio-filename", "voice.webm"),
            request.headers.get("content-type", "audio/webm").split(";", 1)[0],
            request.headers.get("x-language-code"),
        )
    except voice.VoiceError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"text": text, "provider": "sarvam", "model": settings.sarvam_stt_model}


@app.post("/v1/voice/speak")
def voice_speak(body: VoiceTextIn) -> Response:
    if not voice.enabled():
        raise HTTPException(503, "Sarvam voice is not configured; use browser text-to-speech")
    try:
        audio = voice.synthesize(body.text, body.language_code)
    except voice.VoiceError as exc:
        raise HTTPException(502, str(exc)) from exc
    return Response(content=audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@app.post("/v1/chat")
async def chat(body: ChatIn) -> StreamingResponse:
    """Server-sent events: one per tool call, per result, then the card and the final text."""
    if body.language_code and body.language_code not in SARVAM_LANGUAGES:
        raise HTTPException(422, f"Unsupported language code: {body.language_code}")
    mid = _mid()

    async def stream() -> AsyncIterator[bytes]:
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        def emit(evt: dict[str, Any]) -> None:
            queue.put_nowait(evt)

        def run_agent() -> dict[str, Any]:
            from .agent import loop as agent_loop
            return agent_loop.run_chat(mid, body.message, body.session_id, on_event=emit,
                                       lang=body.language_code)

        task = loop.run_in_executor(None, run_agent)
        while True:
            try:
                evt = await asyncio.wait_for(queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                if task.done():
                    break
                continue
            yield f"data: {json.dumps(evt, default=str)}\n\n"
        result = await asyncio.wrap_future(task)
        done_evt = {"type": "done", "session_id": result["session_id"],
                    "trace_id": result["trace_id"], "provider": result["provider"]}
        yield f"data: {json.dumps(done_evt, default=str)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------ dashboard

@app.get("/v1/merchants/me/dashboard")
def dashboard() -> dict[str, Any]:
    mid = _mid()
    m = repo.merchant(mid)
    brief = memory.brief(mid)
    return {
        "merchant": {k: m[k] for k in ("merchant_id", "name", "vertical", "city", "pincode",
                                        "open_hours", "lang")},
        "as_of": iso(demo_now()),
        "brief": brief,
        "signals": repo.open_signals(mid, 10),
        "pending_approvals": approvals.pending(mid),
        "runs": repo.runs_for(mid, limit=12),
        "guardrails": {
            "spend_this_week_paise": policy_engine.spend_this_week(mid, demo_now()),
            "spend_cap_week_paise": settings.policy.spend_cap_paise_per_week,
            "campaigns_this_week": policy_engine.campaigns_this_week(mid, demo_now()),
            "campaigns_cap_week": settings.policy.campaigns_per_week,
        },
    }


# ------------------------------------------------------------------ approvals

class ApproveIn(BaseModel):
    token: str
    actor: str = "merchant"
    reason: str | None = None


@app.post("/v1/approvals/{run_id}/approve")
def approve(run_id: str, body: ApproveIn) -> dict[str, Any]:
    try:
        run = engine.approve(run_id, body.actor, body.token, body.reason)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    # Execute immediately on approval — the "one tap" in the deck's one-tap approve.
    if run["state"] == "executing":
        from .agent import tools
        try:
            out = tools.call("execute_action", {"run_id": run_id}, trace_id=None, merchant_id=run["merchant_id"])
            run = repo.run(run_id)
            return {"run": run, "execution": out}
        except PermissionError as e:
            return {"run": run, "execution": {"blocked": str(e)}}
    return {"run": run}


@app.post("/v1/approvals/{run_id}/reject")
def reject(run_id: str, body: ApproveIn) -> dict[str, Any]:
    try:
        return {"run": engine.reject(run_id, body.actor, body.reason or "dismissed")}
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/v1/approvals/pending")
def pending() -> list[dict[str, Any]]:
    return approvals.pending(_mid())


# ------------------------------------------------------------------ runs + report

@app.get("/v1/runs/{run_id}/tracker")
def tracker(run_id: str) -> dict[str, Any]:
    run = repo.run(run_id)
    if not run:
        raise HTTPException(404, "unknown run")
    acts = [dict(r) for r in q("SELECT * FROM actions WHERE run_id = ?", (run_id,))]
    for a in acts:
        a["payload"] = jload(a["payload"])
    dlv = q("SELECT status, COUNT(*) AS n FROM deliveries WHERE action_id IN "
            "(SELECT action_id FROM actions WHERE run_id = ?) GROUP BY status", (run_id,))
    outs = [dict(r) for r in q("SELECT * FROM outcomes WHERE run_id = ? ORDER BY window_d", (run_id,))]
    for o in outs:
        o["detail"] = jload(o["detail"])
    cohorts = {arm: repo.cohort(run_id, arm) for arm in ("treatment", "control")}
    return {"run": run, "actions": acts, "deliveries": [dict(d) for d in dlv],
            "outcomes": outs, "cohort_sizes": {k: len(v) for k, v in cohorts.items()},
            "state_machine": engine.STATES}


@app.get("/v1/report/weekly")
def weekly() -> dict[str, Any]:
    return report_card.report(_mid())


# ------------------------------------------------------------------ jobs + webhooks

@app.post("/v1/jobs/signals/scan")
def scan_job() -> dict[str, Any]:
    fired = detectors.scan(_mid())
    return {"scanned": True, "fired": len(fired),
            "signals": [{"kind": s["kind"], "title": s["title"]} for s in fired]}


@app.post("/v1/jobs/measure")
def measure_job() -> dict[str, Any]:
    mid = _mid()
    done = []
    for r in q("SELECT run_id FROM playbook_runs WHERE merchant_id = ? "
               "AND state IN ('executed','measuring','closed') ORDER BY created_at", (mid,)):
        for window_d in (7, 30):   # measure is an idempotent upsert; closed runs re-check cleanly
            out = attribution.measure(r["run_id"], window_d)
            if out.get("confidence"):
                done.append({"run_id": r["run_id"], "window_d": window_d,
                             "gmv_influenced_paise": out["components_paise"]["gmv_influenced"]})
    return {"measured": done}


class WebhookIn(BaseModel):
    connector: str = "whatsapp"
    events: list[dict[str, Any]]


@app.post("/v1/webhooks/whatsapp")
def whatsapp_webhook(body: WebhookIn) -> dict[str, Any]:
    """Connector events land here in production (BSP callback). Idempotent per
    (action, customer, status) via the deliveries UNIQUE constraint."""
    accepted = 0
    for ev in body.events:
        conn = connect()
        conn.execute(
            "INSERT OR IGNORE INTO deliveries(action_id, customer_tok, status, amount_paise,"
            " event_time) VALUES (?,?,?,?,?)",
            (ev.get("action_id", ""), ev.get("customer_tok", ""), ev.get("status", "delivered"),
             ev.get("amount_paise"), ev.get("event_time") or iso(demo_now())))
        accepted += 1
    audit.log("action", {"webhook": "whatsapp", "events": accepted, "connector": body.connector})
    return {"accepted": accepted}


# ------------------------------------------------------------------ demo controls

class AdvanceIn(BaseModel):
    days: int = 7


@app.post("/v1/demo/advance")
def advance(body: AdvanceIn) -> dict[str, Any]:
    """DEMO CONTROL — moves the sandbox clock so the measurement window can complete.
    Clearly labelled in the UI; in production time passes on its own."""
    if body.days <= 0 or body.days > 30:
        raise HTTPException(422, "advance 1..30 days")
    return world_sim.advance(body.days)


@app.post("/v1/demo/reset")
def demo_reset() -> dict[str, Any]:
    """DEMO CONTROL — regenerate the world from the scenario and re-run the nightly scan."""
    import subprocess, sys
    from .config import ROOT
    r = subprocess.run([sys.executable, "-m", "datagen.generate"], cwd=str(ROOT),
                       capture_output=True, text=True)
    if r.returncode:
        raise HTTPException(500, r.stderr[-400:])
    detectors.scan(_mid())
    return {"reset": True, "output": r.stdout.strip().splitlines()[-5:]}


@app.get("/v1/audit")
def audit_view(trace_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """The proof drawer: every prompt, tool call, tool result, approval and policy check."""
    if trace_id:
        rows = q("SELECT * FROM audit_log WHERE trace_id = ? ORDER BY id LIMIT ?", (trace_id, limit))
    else:
        rows = q("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
    return [{"id": r["id"], "ts": r["ts"], "kind": r["kind"], "run_id": r["run_id"],
             "payload": jload(r["payload"])} for r in rows]


@app.get("/v1/health")
def health() -> dict[str, Any]:
    return {"ok": True, "now": iso(demo_now()), "provider": settings.agent.provider,
            "model": settings.agent.model, "voice": voice.status()}


# ------------------------------------------------------------------ static SPA

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    path = settings.dashboard_dir / "index.html"
    if path.exists():
        return HTMLResponse(path.read_text())
    return HTMLResponse("<h1>Vyapaar AI</h1><p>dashboard/ not built yet — API at /docs</p>")


if settings.dashboard_dir.exists():
    app.mount("/dashboard", StaticFiles(directory=str(settings.dashboard_dir)), name="dashboard")
