"""Tools for composing The 11 PM Ops Brief.

`compose_brief` is the agent's final action — it takes all gathered context
(themes, severity, actions, recurring patterns) and renders the markdown brief.
The structured tool signature gives MLflow tracing clean inputs for "the agent
decided severity = 72 because ..." style introspection.
"""
from __future__ import annotations
import json, hashlib
from datetime import datetime, timezone
from typing import Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field


class Theme(BaseModel):
    name: str = Field(description="Short theme name, e.g. 'Memphis severe weather'")
    impact_summary: str = Field(description="One-sentence impact with numbers ($ at risk, ship count)")
    sources: list[str] = Field(default_factory=list, description="URLs / data sources cited")
    is_recurring: bool = Field(default=False, description="True if this theme appeared in a prior brief — flag for narrative emphasis")
    recurrence_note: Optional[str] = Field(default=None, description="If recurring, the note (e.g. '2nd consecutive night')")


class Action(BaseModel):
    verb: str = Field(description="Action verb, e.g. 'Reroute', 'Notify', 'Hold'")
    target: str = Field(description="Object of the action, e.g. '47 MEM-bound shipments via DFW for tonight's cutoff'")
    owner_role: str = Field(description="Who should do it, e.g. 'Ops Director', 'CSM Lead'")


@tool
def compose_brief(
    severity_score: int,
    in_flight_count: int,
    at_risk_count: int,
    revenue_at_risk_usd: float,
    themes: list[Theme],
    actions: list[Action],
    bottom_line: str = "",
    recurring_pattern: Optional[str] = None,
) -> str:
    """Compose the final nightly ops brief as a markdown document.

    Inputs are structured for traceability — MLflow will record the exact severity,
    theme list, and action set the agent chose. Returns the markdown brief.

    Args:
        severity_score: 0-100. >=70 critical, 40-69 elevated, <40 nominal.
        in_flight_count: Total shipments currently in flight.
        at_risk_count: Subset of in-flight shipments at risk from active disruptions.
        revenue_at_risk_usd: Total $ value of at-risk shipments.
        themes: 3-5 disruption themes with impact and sources.
        actions: 3-5 recommended actions for ops leadership.
        bottom_line: A single flowing prose paragraph (3-5 sentences) for the
            "Exec summary" section — what's happening, why it matters, and what
            to do now. Weave the key numbers (in-flight, at-risk, $ at risk,
            posture) inline with **bold**. NO bullets, NO chips, NO tables —
            just a paragraph that reads like a human ops lead briefing the
            director on the phone. Required for every brief.
        recurring_pattern: If any theme is a multi-night recurrence, the narrative
            call-out string (e.g. "Memphis weather — 2nd consecutive night").
    """
    sev_label = "critical" if severity_score >= 70 else ("elevated" if severity_score >= 40 else "nominal")
    date_str = datetime.now(timezone.utc).strftime("%A %b %d, %Y")

    md = [
        f"# Ops Brief — {date_str}",
        f"",
    ]
    if bottom_line:
        md.append("## Exec summary")
        md.append("")
        md.append(bottom_line.strip())
        md.append("")
    if recurring_pattern:
        md.append(f"> 🔁 **Recurring pattern detected:** {recurring_pattern}")
        md.append("")
    md.append("## Top themes")
    for i, t in enumerate(themes, 1):
        emoji = "🔁 " if t.is_recurring else ""
        md.append(f"{i}. {emoji}**{t.name}** — {t.impact_summary}")
        if t.is_recurring and t.recurrence_note:
            md.append(f"   _{t.recurrence_note}_")
        if t.sources:
            md.append(f"   _Sources: {', '.join(t.sources)}_")
    md.append("")
    md.append("## Recommended actions")
    for i, a in enumerate(actions, 1):
        md.append(f"{i}. **{a.verb}** {a.target} — _{a.owner_role}_")
    md.append("")

    # Deterministic brief_id for repeatability
    payload = {
        "severity_score": severity_score, "in_flight_count": in_flight_count,
        "at_risk_count": at_risk_count, "revenue_at_risk_usd": revenue_at_risk_usd,
        "themes": [t.model_dump() for t in themes],
        "actions": [a.model_dump() for a in actions],
        "recurring_pattern": recurring_pattern,
    }
    brief_id = "BRF-" + hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:12]
    md.append(f"_Brief id: `{brief_id}` · generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}_")
    return "\n".join(md)
