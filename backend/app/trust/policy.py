"""Policy engine (§15, principle #6).

Spend caps, quiet hours, frequency caps and audience ceilings are enforced HERE, in code,
before `request_approval` and again before `execute_action`. They are never negotiated in a
prompt — a model that asks to blow the cap gets a structured refusal it must relay.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..config import settings
from ..db import demo_now, iso, jload, q, q1
from . import audit


@dataclass
class Violation:
    code: str
    message: str
    limit: Any
    requested: Any

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "limit": self.limit, "requested": self.requested}


@dataclass
class Decision:
    allowed: bool
    violations: list[Violation] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "checks_passed": self.checks,
            "violations": [v.as_dict() for v in self.violations],
        }


def _in_quiet_hours(at: datetime) -> bool:
    p = settings.policy
    # IST is the merchant's clock; the stored instant is UTC.
    local = (at + timedelta(hours=5, minutes=30)).time()
    if p.quiet_hours_start <= p.quiet_hours_end:
        return p.quiet_hours_start <= local < p.quiet_hours_end
    return local >= p.quiet_hours_start or local < p.quiet_hours_end


def campaigns_this_week(merchant_id: int, at: datetime, exclude_run_id: str | None = None) -> int:
    """Campaigns that went out in the last 7 days. A run never counts against itself."""
    since = iso(at - timedelta(days=7))
    row = q1(
        "SELECT COUNT(*) AS n FROM playbook_runs "
        "WHERE merchant_id = ? AND state IN ('executing','executed','measuring','closed') "
        "AND updated_at >= ? AND run_id != ?",
        (merchant_id, since, exclude_run_id or ""),
    )
    return int(row["n"]) if row else 0


def spend_this_week(merchant_id: int, at: datetime) -> int:
    since = iso(at - timedelta(days=7))
    rows = q(
        "SELECT a.cost_paise AS c FROM actions a JOIN playbook_runs r ON r.run_id = a.run_id "
        "WHERE r.merchant_id = ? AND a.created_at >= ?",
        (merchant_id, since),
    )
    return sum(int(r["c"]) for r in rows)


def check(
    merchant_id: int,
    *,
    audience_size: int,
    cost_paise: int,
    channel: str = "whatsapp",
    at: datetime | None = None,
    stage: str = "pre_approval",
    trace_id: str | None = None,
    run_id: str | None = None,
) -> Decision:
    p = settings.policy
    now = at or demo_now()
    d = Decision(allowed=True)

    if cost_paise > p.spend_cap_paise_per_run:
        d.violations.append(Violation(
            "spend_cap_run",
            f"Cost ₹{cost_paise/100:,.0f} exceeds the per-action cap of ₹{p.spend_cap_paise_per_run/100:,.0f}.",
            p.spend_cap_paise_per_run, cost_paise))
    else:
        d.checks.append("spend_cap_run")

    already = spend_this_week(merchant_id, now)
    if already + cost_paise > p.spend_cap_paise_per_week:
        d.violations.append(Violation(
            "spend_cap_week",
            f"Weekly spend would reach ₹{(already+cost_paise)/100:,.0f}, over the ₹{p.spend_cap_paise_per_week/100:,.0f} weekly cap.",
            p.spend_cap_paise_per_week, already + cost_paise))
    else:
        d.checks.append("spend_cap_week")

    if audience_size > p.audience_ceiling:
        d.violations.append(Violation(
            "audience_ceiling",
            f"Audience of {audience_size:,} exceeds the ceiling of {p.audience_ceiling:,}.",
            p.audience_ceiling, audience_size))
    else:
        d.checks.append("audience_ceiling")

    runs = campaigns_this_week(merchant_id, now, exclude_run_id=run_id)
    if runs >= p.campaigns_per_week:
        d.violations.append(Violation(
            "frequency_cap",
            f"{runs} campaigns already went out this week; the cap is {p.campaigns_per_week}.",
            p.campaigns_per_week, runs + 1))
    else:
        d.checks.append("frequency_cap")

    # Quiet hours gate sending, not drafting or approving.
    if stage == "pre_execute" and channel in {"whatsapp", "sms", "push"} and _in_quiet_hours(now):
        d.violations.append(Violation(
            "quiet_hours",
            f"Quiet hours {p.quiet_hours_start:%H:%M}–{p.quiet_hours_end:%H:%M} IST. The send is queued for {p.quiet_hours_end:%H:%M}.",
            f"{p.quiet_hours_start:%H:%M}-{p.quiet_hours_end:%H:%M}", iso(now)))
    else:
        d.checks.append("quiet_hours")

    d.allowed = not d.violations
    audit.log("policy", {"stage": stage, "decision": d.as_dict(),
                         "requested": {"audience_size": audience_size, "cost_paise": cost_paise, "channel": channel}},
              trace_id=trace_id, merchant_id=merchant_id, run_id=run_id)
    return d
