"""Write-back action tools — the "Palantir Actions" layer.

The Composer subagent proposes actions; the UI's Action Queue surfaces them
for human approval; on approve, they execute (or just log, depending on
production mode).

For the hackathon demo we keep three actions:

  - propose_action — proposes any action with title/severity/payload; the UI
    renders it in the Action Queue. (Generic — any verb.)
  - file_disruption_ticket — drafts a Jira-ish disruption-tracking ticket.
  - draft_carrier_email — drafts an outbound carrier comms email.
  - log_decision — appends the brief's decision summary to an in-process log
    (also written to a JSONL file so judges can inspect the trail).

In a real deploy these would write to Delta tables / Jira / Slack via OBO.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# In-memory ring buffer so /actions/queue can return the recent proposals
# without us needing Postgres/Lakebase. The UI hits this endpoint.
_ACTION_QUEUE: deque[dict] = deque(maxlen=50)
_DECISION_LOG: deque[dict] = deque(maxlen=200)

# Write to disk too so judges can audit (and so this survives a restart for the demo).
_DATA_DIR = Path(os.getenv("OPS_BRIEF_DATA_DIR", "/tmp/ops_brief"))
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_ACTIONS_FILE = _DATA_DIR / "actions.jsonl"
_DECISIONS_FILE = _DATA_DIR / "decisions.jsonl"


def _persist(path: Path, record: dict) -> None:
    try:
        with path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as exc:
        logger.warning("Failed to persist to %s: %s", path, exc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ────────────────────────────────────────────────────────────────────────────
# Tool: propose_action — generic action proposal (the canonical write-back)
# ────────────────────────────────────────────────────────────────────────────


class _ProposeActionArgs(BaseModel):
    title: str = Field(description="Imperative-verb-led action title (e.g. 'Reroute 47 MEM-bound shipments via DFW').")
    action_type: str = Field(
        description="Category. One of: reroute | notify | hold | escalate | inspect | other.",
        default="other",
    )
    severity: str = Field(
        description="Severity. One of: critical | elevated | nominal.",
        default="elevated",
    )
    owner_role: str = Field(description="Who owns execution (role, not person). e.g. 'Ops Director'.")
    rationale: str = Field(description="One-sentence why-this-matters.")
    impact_estimate: str = Field(
        default="",
        description="Optional quantified impact, e.g. '$88K revenue protected' or '47 shipments rerouted'.",
    )
    related_objects: list[str] = Field(
        default_factory=list,
        description="Object IDs the action touches: FC-3, CARRIER-FEDEX, SHIPMENT-..., etc.",
    )


@tool(args_schema=_ProposeActionArgs)
def propose_action(
    title: str,
    action_type: str = "other",
    severity: str = "elevated",
    owner_role: str = "Ops Director",
    rationale: str = "",
    impact_estimate: str = "",
    related_objects: Optional[list[str]] = None,
) -> str:
    """Propose a write-back action for human approval.

    The proposed action lands in the Action Queue pane of the UI; an operator
    reviews and either approves (triggers execution) or rejects.
    """
    action_id = f"act_{uuid.uuid4().hex[:12]}"
    record = {
        "id": action_id,
        "title": title,
        "type": action_type,
        "severity": severity,
        "owner_role": owner_role,
        "rationale": rationale,
        "impact_estimate": impact_estimate,
        "related_objects": list(related_objects or []),
        "status": "pending",
        "proposed_at": _now_iso(),
    }
    _ACTION_QUEUE.append(record)
    _persist(_ACTIONS_FILE, record)
    logger.info("[Composer] proposed action %s: %s", action_id, title)
    return (
        f"Proposed action {action_id}: '{title}' "
        f"(type={action_type}, severity={severity}, owner={owner_role}). "
        f"Queued for human approval."
    )


# ────────────────────────────────────────────────────────────────────────────
# Tool: file_disruption_ticket — Jira-ish ticket draft
# ────────────────────────────────────────────────────────────────────────────


class _FileDisruptionTicketArgs(BaseModel):
    title: str = Field(description="Ticket title.")
    severity: str = Field(description="critical | elevated | nominal", default="elevated")
    affected_carriers: list[str] = Field(default_factory=list)
    affected_fcs: list[str] = Field(default_factory=list)
    affected_lanes: list[str] = Field(default_factory=list)
    description: str = Field(description="Markdown body.")


@tool(args_schema=_FileDisruptionTicketArgs)
def file_disruption_ticket(
    title: str,
    severity: str = "elevated",
    affected_carriers: Optional[list[str]] = None,
    affected_fcs: Optional[list[str]] = None,
    affected_lanes: Optional[list[str]] = None,
    description: str = "",
) -> str:
    """File a disruption-tracking ticket. Returns the draft ticket payload (not yet sent)."""
    ticket_id = f"DISR-{int(time.time()) % 1_000_000:06d}"
    record = {
        "id": ticket_id,
        "kind": "disruption_ticket",
        "title": title,
        "severity": severity,
        "affected_carriers": list(affected_carriers or []),
        "affected_fcs": list(affected_fcs or []),
        "affected_lanes": list(affected_lanes or []),
        "description": description,
        "status": "draft",
        "drafted_at": _now_iso(),
    }
    # Mirror to the action queue so the UI shows it too.
    _ACTION_QUEUE.append(
        {
            "id": ticket_id,
            "title": f"File ticket: {title}",
            "type": "escalate",
            "severity": severity,
            "owner_role": "Disruption Lead",
            "rationale": description[:200],
            "impact_estimate": "",
            "related_objects": (affected_carriers or []) + (affected_fcs or []) + (affected_lanes or []),
            "status": "pending",
            "proposed_at": _now_iso(),
            "payload": record,
        }
    )
    _persist(_ACTIONS_FILE, record)
    logger.info("[Composer] drafted disruption ticket %s: %s", ticket_id, title)
    return (
        f"Drafted disruption ticket {ticket_id} (severity={severity}). "
        f"Affected: carriers={affected_carriers or []}, FCs={affected_fcs or []}, "
        f"lanes={affected_lanes or []}. Status: draft (awaiting human approval to send)."
    )


# ────────────────────────────────────────────────────────────────────────────
# Tool: draft_carrier_email — outbound comms draft
# ────────────────────────────────────────────────────────────────────────────


class _DraftCarrierEmailArgs(BaseModel):
    to_carrier: str = Field(description="Carrier name, e.g. 'FedEx', 'UPS', 'OnTrac'.")
    subject: str = Field(description="Email subject line.")
    severity: str = Field(default="elevated", description="critical | elevated | nominal")
    body_markdown: str = Field(description="Email body in markdown. Will be rendered as text/markdown.")
    impacted_shipment_count: int = Field(default=0)


@tool(args_schema=_DraftCarrierEmailArgs)
def draft_carrier_email(
    to_carrier: str,
    subject: str,
    severity: str = "elevated",
    body_markdown: str = "",
    impacted_shipment_count: int = 0,
) -> str:
    """Draft an outbound carrier email. Returns the draft (not sent — awaiting approval)."""
    email_id = f"email_{uuid.uuid4().hex[:10]}"
    record = {
        "id": email_id,
        "kind": "carrier_email",
        "to": to_carrier,
        "subject": subject,
        "severity": severity,
        "body_markdown": body_markdown,
        "impacted_shipments": impacted_shipment_count,
        "status": "draft",
        "drafted_at": _now_iso(),
    }
    _ACTION_QUEUE.append(
        {
            "id": email_id,
            "title": f"Email {to_carrier}: {subject}",
            "type": "notify",
            "severity": severity,
            "owner_role": "Carrier Relations",
            "rationale": (body_markdown or "")[:200],
            "impact_estimate": f"{impacted_shipment_count} shipments" if impacted_shipment_count else "",
            "related_objects": [f"CARRIER-{to_carrier.upper()}"],
            "status": "pending",
            "proposed_at": _now_iso(),
            "payload": record,
        }
    )
    _persist(_ACTIONS_FILE, record)
    logger.info("[Composer] drafted carrier email %s to %s", email_id, to_carrier)
    return (
        f"Drafted email to {to_carrier} (subject: '{subject}'). "
        f"Status: draft (awaiting approval). Impacted: {impacted_shipment_count} shipments."
    )


# ────────────────────────────────────────────────────────────────────────────
# Tool: log_decision — append a decision-log entry (separate from actions)
# ────────────────────────────────────────────────────────────────────────────


class _LogDecisionArgs(BaseModel):
    brief_id: str = Field(default="", description="Brief ID this decision is part of.")
    headline: str = Field(description="One-line decision headline.")
    severity_score: float = Field(default=0.0)
    themes: list[str] = Field(default_factory=list)
    rationale: str = Field(default="")


@tool(args_schema=_LogDecisionArgs)
def log_decision(
    brief_id: str = "",
    headline: str = "",
    severity_score: float = 0.0,
    themes: Optional[list[str]] = None,
    rationale: str = "",
) -> str:
    """Log a brief-level decision to the decision log."""
    decision_id = f"dec_{uuid.uuid4().hex[:10]}"
    record = {
        "id": decision_id,
        "brief_id": brief_id or f"brief_{int(time.time())}",
        "headline": headline,
        "severity_score": float(severity_score or 0.0),
        "themes": list(themes or []),
        "rationale": rationale,
        "logged_at": _now_iso(),
    }
    _DECISION_LOG.append(record)
    _persist(_DECISIONS_FILE, record)
    logger.info("[Composer] decision logged %s: %s", decision_id, headline)
    return f"Decision logged: {decision_id}. Headline: '{headline}'. Severity: {severity_score:.1f}."


# ────────────────────────────────────────────────────────────────────────────
# Read-side helpers used by FastAPI endpoints
# ────────────────────────────────────────────────────────────────────────────


def get_action_queue(limit: int = 20) -> list[dict]:
    """Return the most-recent N action proposals (newest first)."""
    items = list(_ACTION_QUEUE)
    items.reverse()
    return items[:limit]


def get_decision_log(limit: int = 20) -> list[dict]:
    """Return the most-recent N decisions (newest first)."""
    items = list(_DECISION_LOG)
    items.reverse()
    return items[:limit]


def approve_action(action_id: str, approver: str = "") -> dict:
    """Mark an action approved. (In real life, this would trigger execution.)"""
    for item in _ACTION_QUEUE:
        if item.get("id") == action_id:
            item["status"] = "approved"
            item["approved_by"] = approver or "anonymous"
            item["approved_at"] = _now_iso()
            _persist(_ACTIONS_FILE, {"event": "approved", **item})
            return item
    return {"error": f"action {action_id} not found in queue"}


def reject_action(action_id: str, approver: str = "", reason: str = "") -> dict:
    """Mark an action rejected."""
    for item in _ACTION_QUEUE:
        if item.get("id") == action_id:
            item["status"] = "rejected"
            item["rejected_by"] = approver or "anonymous"
            item["rejected_at"] = _now_iso()
            item["rejection_reason"] = reason
            _persist(_ACTIONS_FILE, {"event": "rejected", **item})
            return item
    return {"error": f"action {action_id} not found in queue"}


# Exports for easy binding from agent.py
ACTION_TOOLS = [propose_action, file_disruption_ticket, draft_carrier_email, log_decision]
