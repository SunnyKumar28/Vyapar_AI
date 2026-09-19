"""Three providers, one interface: `next_action(transcript) -> Action`.

ScriptedLLM (default) — a deterministic planner. The demo must work offline, every time,
with zero API spend, and the numbers must be traceable to tool calls. It parses intent
from the merchant's message (Hindi/Hinglish/English keywords), plays a fixed tool
sequence through the SAME registry and guardrails the real model uses, and composes its
final answer from the tool outputs — it never fabricates a number any more than the
real model is allowed to.

ClaudeLLM (VYAPAAR_LLM=claude) — the real function-calling loop against claude-opus-5:
adaptive thinking, strict tool schemas, stop_reason-driven, json.loads on tool inputs,
validated before dispatch. Same registry, same guardrails, same audit log; only the
brain changes (principle #6: policy in code, so the brain is swappable).

SarvamLLM (VYAPAAR_LLM=sarvam) — OpenAI-compatible function calling for Hindi/Hinglish
chat. It sees the same structured tools and degrades to ScriptedLLM when unavailable.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..config import settings
from ..db import jdump
from ..trust import audit


@dataclass
class Action:
    """One step's decision: call a tool, or answer the merchant."""
    kind: str                       # "tool" | "final" | "error"
    tool: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    text: str = ""


def _fmt_paise(p: int) -> str:
    return f"₹{p // 100:,}".replace(",", ",")


# ------------------------------------------------------------------ scripted planner

class ScriptedLLM:
    """Deterministic intent -> plan -> tool calls -> templated answer from tool results."""

    NAME = "scripted-planner-v1"

    def __init__(self, merchant_id: int, history: list[dict[str, str]] | None = None) -> None:
        self.merchant_id = merchant_id
        self.plan: list[tuple[str, dict[str, Any]]] = []
        self.step = 0
        self.transcript: list[dict[str, Any]] = []

    # -- intent ---------------------------------------------------------------

    @staticmethod
    def _intent(msg: str) -> str:
        s = msg.lower()
        if any(w in s for w in ("lapsed", "winback", "wapas", "grahak", "customer kam",
                                "regular", "bhula", "nahi aa rahe", "lost customer")):
            return "winback"
        if any(w in s for w in ("agle hafte", "agale hafte", "next week", "forecast", "predict",
                                "prediction", "future", "dip ayega", "dip aayega", "hoga",
                                "kitni bikri", "kitna bikega", "sales hogi")):
            return "forecast"
        if any(w in s for w in ("report", "weekly report", "roi", "card", "hisab")):
            return "report"
        if any(w in s for w in ("stock", "maal", "item", "samosa", "khatam", "out of",
                                "top seller", "best seller", "sabse zyada", "peak time",
                                "busy time", "payment", "upi", "card")):
            return "insights"
        if any(w in s for w in ("remind", "yaad dila", "reminder")):
            return "reminder"
        if any(w in s for w in ("sales", "bikri", "kamai", "revenue", "kyu", "kyun",
                                "why", "how much", "kitna", "dip", "kam")):
            return "sales"
        return "none"                        # stay humble: no diagnosis nobody asked for

    # -- plan -----------------------------------------------------------------

    def _build_plan(self, msg: str) -> None:
        intent = self._intent(msg)
        if intent == "winback":
            self.plan = [("get_merchant_brief", {}),
                         ("list_lapsed_customers", {}),
                         ("estimate_impact", {"audience_size": "$list_lapsed_customers.count"}),
                         ("request_approval", {"signal_kind": "churn_cluster"})]
        elif intent == "forecast":
            self.plan = [("get_merchant_brief", {}), ("forecast_sales", {})]
        elif intent == "report":
            self.plan = [("get_merchant_brief", {})]
        elif intent == "insights":
            self.plan = [("get_merchant_brief", {}), ("get_store_insights", {"days": 30})]
        elif intent == "none":
            self.plan = [("get_merchant_brief", {})]
        else:                                    # "sales" — the flagship demo path
            self.plan = [("get_merchant_brief", {}),
                         ("detect_anomalies", {}),
                         ("list_lapsed_customers", {}),
                         ("estimate_impact", {"audience_size": "$list_lapsed_customers.count"}),
                         ("request_approval", {"signal_kind": "sales_anomaly"})]

    def _resolve(self, args: dict[str, Any]) -> dict[str, Any]:
        """$-references pull live numbers from earlier tool outputs — never from memory."""
        by_tool: dict[str, dict[str, Any]] = {}
        for t in self.transcript:
            if t.get("kind") == "tool_result":
                by_tool[t["tool"]] = t["result"]
        out = {}
        for k, v in args.items():
            if isinstance(v, str) and v.startswith("$"):
                node: Any = by_tool
                for part in v[1:].split("."):
                    node = node[part] if isinstance(node, dict) else {}
                out[k] = node
            else:
                out[k] = v
        return out

    # -- the interface ----------------------------------------------------------

    def next_action(self, *, system: str, user_msg: str) -> Action:
        if not self.plan:
            self._build_plan(user_msg)
        while self.step < len(self.plan):
            name, args = self.plan[self.step]
            self.step += 1
            return Action(kind="tool", tool=name, args=self._resolve(args))
        return Action(kind="final", text=self._answer(user_msg))

    def observe(self, tool: str, result: dict[str, Any], ok: bool) -> None:
        self.transcript.append({"kind": "tool_result", "tool": tool, "result": result, "ok": ok})

    # -- answer composition ---------------------------------------------------

    def _tool(self, name: str) -> dict[str, Any] | None:
        for t in self.transcript:
            if t["kind"] == "tool_result" and t["tool"] == name:
                return t["result"]
        return None

    def _answer(self, msg: str) -> str:
        """Numbers are interpolated ONLY from tool results captured in self.transcript."""
        if not self.plan and not self.transcript:
            return "Main aapke dukaan ka data dekh sakta hoon — poochhiye: kal ki bikri, customers, ya stock."
        brief = self._tool("get_merchant_brief") or {}
        anom = (self._tool("detect_anomalies") or {}).get("anomalies") or []
        lapsed = self._tool("list_lapsed_customers") or {}
        est = self._tool("estimate_impact") or {}
        appr = self._tool("request_approval") or {}
        forecast = self._tool("forecast_sales") or {}
        insights = self._tool("get_store_insights") or {}
        sales = brief.get("sales_30d", {})

        if insights:
            inventory = insights.get("inventory") or []
            at_risk = insights.get("at_risk_items") or []
            reorder = insights.get("reorder_soon_items") or []
            top = (insights.get("top_items") or [None])[0]
            lower_msg = msg.lower()
            asks_top = any(w in lower_msg for w in ("top", "best", "sabse zyada", "bestseller"))
            asks_peak = any(w in lower_msg for w in ("peak", "busy", "rush", "bheed"))
            asks_payment = any(w in lower_msg for w in ("payment", "upi", "card", "wallet"))
            if asks_top and top:
                peak = (insights.get("peak_hours") or [None])[0]
                answer = (f"Pichhle 30 din mein sabse zyada bikne wala item {top['item']} hai: "
                          f"{top['txns']} transactions aur {_fmt_paise(top['gmv_paise'])} GMV.")
                if peak:
                    answer += f" Sabse busy ghanta {peak['hour']:02d}:00 ke aas-paas hai."
                return answer + " Isi item ka stock pehle check karna sahi rahega."
            if asks_peak:
                peak = (insights.get("peak_hours") or [None])[0]
                if peak:
                    return (f"Shop ka busiest time {peak['hour']:02d}:00 ke aas-paas hai — "
                            f"{peak['txns']} transactions aur {_fmt_paise(peak['gmv_paise'])} GMV "
                            f"pichhle {insights.get('window_days', 30)} din ke data mein. "
                            "Is slot se pehle fast-moving stock ready rakho.")
            if asks_payment and insights.get("channel_mix"):
                channel = insights["channel_mix"][0]
                return (f"Payment mix mein {channel['channel'].upper()} sabse bada channel hai: "
                        f"{channel['txns']} transactions aur {_fmt_paise(channel['gmv_paise'])} GMV "
                        f"pichhle {insights.get('window_days', 30)} din mein. "
                        "Is channel ka settlement aur soundbox status roz check karna sahi rahega.")
            if at_risk or reorder:
                risk = at_risk[0] if at_risk else reorder[0]
                status = "bahut kam" if risk["status"] == "at_risk" else "reorder karna chahiye"
                answer = (f"Stock check ke hisaab se {risk['item']} {status}: abhi "
                          f"{risk['units_on_hand']} units bache hain, reorder point "
                          f"{risk['reorder_point_units']} hai. Pichhle 7 din mein "
                          f"{risk['units_sold_7d']} units bike; supplier {risk['supplier']} "
                          f"ka restock time {risk['restock_days']} din hai.")
                if top and top["item"] != risk["item"]:
                    answer += f" Sabse zyada bikne wala item {top['item']} hai."
                return answer + " Pehla next step: is item ka aaj ka stock confirm kar lo."

        if forecast:
            total = _fmt_paise(forecast.get("next_period_total_paise", 0))
            if forecast.get("dip_expected"):
                dip_days = ", ".join(row.get("date", "") for row in forecast.get("dip_days", []))
                answer = f"Agle {forecast.get('horizon_days', 7)} din ki estimated bikri {total} hai. Dip ka risk hai"
                if dip_days:
                    answer += f" ({dip_days})"
                answer += "."
            else:
                answer = f"Agle {forecast.get('horizon_days', 7)} din ki estimated bikri {total} hai. Bade dip ka signal nahi dikh raha."
            drivers = forecast.get("drivers") or []
            meaningful = [d for d in drivers if d.get("kind") != "no_strong_driver"]
            if forecast.get("dip_expected") and meaningful:
                labels = []
                for driver in meaningful[:2]:
                    if driver.get("kind") == "recent_sales_drop":
                        labels.append(f"recent {driver.get('dow', '')} sales drop")
                    elif driver.get("kind") == "lapsed_regulars":
                        labels.append(f"{driver.get('count', 0)} regular customers ka gap")
                    elif driver.get("kind") == "stockout":
                        labels.append("stock-out signal")
                if labels:
                    answer += " Possible reason: " + ", ".join(labels) + "."
            elif forecast.get("dip_expected"):
                answer += " Is dip ka coded reason abhi strong nahi hai."
            else:
                answer += " Recent signals watchlist mein hain, par unse agle hafte ka dip prove nahi hota."
            return answer + " Yeh estimate hai; agla step forecast ke dip wale din stock aur customer traffic check karna hai."

        if anom and lapsed and est:
            a = anom[-1]
            low, high = est["gmv_influenced_band_paise"]
            parts = [
                f"{a['dow']} ki bikri {_fmt_paise(a['actual_gmv_paise'])} thi, pichhle 4 {a['dow']} "
                f"ka average {_fmt_paise(a['baseline_gmv_paise'])} — {abs(a['delta_pct']) * 100:.0f}% kam.",
                f"Mot wajah: {lapsed['count']} regular customer {lapsed['min_gap_days']}+ din se nahi aaye "
                f"(average ticket {_fmt_paise(lapsed['avg_ticket_paise'])}).",
                f"Wapas lane ka plan: WhatsApp par 10% chhoot (max ₹20). Anumaan: "
                f"{_fmt_paise(low)}–{_fmt_paise(high)} extra bikri, kharcha cap {_fmt_paise(est['cost_cap_paise'])}.",
            ]
            if appr.get("run_id"):
                parts.append(f"Approve karo to main {lapsed['count']} customers ko abhi message bhej deta hoon — "
                             f"ek tap par. Bhejoon?")
            return " ".join(parts)
        if lapsed and est:
            low, high = est["gmv_influenced_band_paise"]
            return (f"{lapsed['count']} regular {lapsed['min_gap_days']}+ din se gaye. Plan: 10% chhoot WhatsApp par "
                    f"— {_fmt_paise(low)}–{_fmt_paise(high)} expected, kharcha cap {_fmt_paise(est['cost_cap_paise'])}. "
                    f"Bhejoon?")
        if sales:
            return (f"Pichhle 30 din: {_fmt_paise(sales['gmv_paise'])} bikri, roz average "
                    f"{_fmt_paise(sales['avg_daily_gmv_paise'])}, {sales['txns']} transactions. "
                    f"Repeat rate {brief.get('repeat_rate_30d', {}).get('repeat_rate', 0) * 100:.0f}%. "
                    f"Kya jaanna chahte ho — kal ka dip ya customers?")
        return "Main abhi data le raha hoon — ek minute."


# ------------------------------------------------------------------ claude provider

class ClaudeLLM:
    """Real function-calling loop on claude-opus-5 (per the claude-api skill's contract)."""

    NAME = "claude-opus-5"

    def __init__(self, merchant_id: int, history: list[dict[str, str]] | None = None) -> None:
        self.merchant_id = merchant_id
        try:
            from anthropic import Anthropic      # noqa: F401  (import check only)
            self.client = Anthropic()
            self.available = True
        except Exception as exc:
            self.available = False
            self.unavailable_reason = repr(exc)
            audit.log("llm_response", {"provider": self.NAME, "event": "unavailable",
                                       "reason": self.unavailable_reason})

    def next_action(self, *, system: str, user_msg: str) -> Action:
        from .tools import TOOLS
        from .guardrails import validate
        try:
            resp = self.client.messages.create(
                model=settings.agent.model,
                max_tokens=settings.agent.max_tokens,
                system=system,
                thinking={"type": "adaptive"},
                messages=[{"role": "user", "content": user_msg}],
                tools=[{
                    "name": n,
                    "description": t["description"],
                    "input_schema": t["schema"],
                } for n, t in TOOLS.items()],
            )
            audit.log("llm_response", {"provider": self.NAME,
                                       "stop_reason": resp.stop_reason,
                                       "usage": {"in": resp.usage.input_tokens,
                                                 "out": resp.usage.output_tokens}})
            if resp.stop_reason == "tool_use":
                block = next(b for b in resp.content if b.type == "tool_use")
                args = json.loads(block.input)       # never string matching
                spec = dict(TOOLS[block.name]["schema"])
                spec.setdefault("properties", {})["merchant_id"] = {"type": "integer"}
                errs = validate({k: v for k, v in args.items() if k != "merchant_id"}, spec)
                if errs:
                    return Action(kind="error", text=f"tool input rejected: {'; '.join(errs)}")
                return Action(kind="tool", tool=block.name, args=args)
            text = "".join(b.text for b in resp.content if b.type == "text")
            return Action(kind="final", text=text)
        except Exception as exc:
            audit.log("llm_response", {"provider": self.NAME, "error": repr(exc)})
            # Principle #5: the copilot degrades to the scripted planner, never to silence.
            return Action(kind="error", text=f"model unavailable ({exc!r}); retrying with the deterministic planner")

    def observe(self, tool: str, result: dict[str, Any], ok: bool) -> None:
        pass                                        # stateless; the loop owns the transcript


# ------------------------------------------------------------------ Sarvam provider

class SarvamLLM:
    """OpenAI-compatible Sarvam tool-calling brain for flexible Indic questions."""

    NAME = "sarvam-105b-conversations"

    def __init__(self, merchant_id: int, history: list[dict[str, str]] | None = None) -> None:
        self.merchant_id = merchant_id
        self.history = history or []
        self.messages: list[dict[str, Any]] = []
        self.started = False
        self.pending_call_id: str | None = None
        self.available = bool(settings.sarvam_api_key)

    def _call(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            settings.sarvam_base_url.rstrip("/") + "/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "api-subscription-key": settings.sarvam_api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=settings.sarvam_timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(body).get("error", {}).get("message", body)
            except (TypeError, ValueError):
                detail = body
            raise RuntimeError(f"Sarvam chat HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"Could not reach Sarvam chat: {exc}") from exc

    @staticmethod
    def _tools() -> list[dict[str, Any]]:
        from .tools import TOOLS
        return [{
            "type": "function",
            "function": {
                "name": name,
                "description": spec["description"],
                "parameters": spec["schema"],
            },
        } for name, spec in TOOLS.items()]

    def next_action(self, *, system: str, user_msg: str) -> Action:
        from .tools import TOOLS
        from .guardrails import validate
        try:
            if not self.started:
                self.messages = [{"role": "system", "content": system}]
                self.messages.extend(self.history[-12:])
                self.messages.append({"role": "user", "content": user_msg})
                self.started = True
            resp = self._call({
                "model": settings.agent.model,
                "messages": self.messages,
                "tools": self._tools(),
                "tool_choice": "auto",
                "max_tokens": settings.agent.max_tokens,
                "temperature": 0.2,
            })
            choice = (resp.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            calls = message.get("tool_calls") or []
            if calls:
                call = calls[0]
                function = call.get("function") or {}
                name = function.get("name")
                if name not in TOOLS:
                    return Action(kind="error", text=f"unknown tool requested: {name}")
                args = json.loads(function.get("arguments") or "{}")
                spec = dict(TOOLS[name]["schema"])
                errs = validate(args, spec)
                if errs:
                    return Action(kind="error", text=f"tool input rejected: {'; '.join(errs)}")
                self.messages.append(message)
                self.pending_call_id = str(call.get("id") or "")
                audit.log("llm_response", {"provider": self.NAME, "finish_reason": "tool_calls",
                                           "tool": name}, merchant_id=self.merchant_id)
                return Action(kind="tool", tool=name, args=args)
            content = message.get("content") or ""
            if isinstance(content, list):
                content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
            self.messages.append({"role": "assistant", "content": str(content)})
            audit.log("llm_response", {"provider": self.NAME, "finish_reason": choice.get("finish_reason"),
                                       "usage": resp.get("usage", {})}, merchant_id=self.merchant_id)
            return Action(kind="final", text=str(content).strip())
        except Exception as exc:
            audit.log("llm_response", {"provider": self.NAME, "error": repr(exc)},
                      merchant_id=self.merchant_id)
            return Action(kind="error", text=f"Sarvam model unavailable ({exc!r})")

    def observe(self, tool: str, result: dict[str, Any], ok: bool) -> None:
        self.messages.append({
            "role": "tool",
            "tool_call_id": self.pending_call_id or f"call_{tool}",
            "content": json.dumps(result if ok else {"error": result.get("error", "tool failed")}, default=str),
        })
        self.pending_call_id = None


def make_brain(merchant_id: int, history: list[dict[str, str]] | None = None) -> Any:
    if settings.agent.provider == "sarvam":
        brain = SarvamLLM(merchant_id, history=history)
        if brain.available:
            return brain
    if settings.agent.provider == "claude":
        brain = ClaudeLLM(merchant_id, history=history)
        if getattr(brain, "available", False):
            return brain
    return ScriptedLLM(merchant_id, history=history)
