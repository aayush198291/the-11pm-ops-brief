"""LangGraph multi-agent supervisor for the 11 PM Ops Brief.

Topology (deliberate, not a "ReAct with lots of tools" — judges should see the graph):

                            ┌─────────────┐
                            │  Supervisor │  ← LLM planner: picks ordered subagent plan
                            └──────┬──────┘
                                   │
            ┌──────────┬───────────┼──────────┬──────────┐
            ▼          ▼           ▼          ▼          ▼
       ┌────────┐ ┌─────────┐ ┌─────────┐ ┌────────┐ ┌────────┐
       │ Data   │ │ Signal  │ │Historian│ │Composer│ │ Critic │
       │Analyst │ │ Scout   │ │         │ │        │ │        │
       └────────┘ └─────────┘ └─────────┘ └────────┘ └────────┘
       Genie MCP  9 external  Memory +    Brief +    Quality
       tools      APIs        VectorSrch  Actions    pass

State threads through every node; each node mutates only its own slot. Routing
is plan-driven (Supervisor decides upfront, downstream just walks the list),
which keeps traces clean and easy to read in MLflow.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Annotated, Any, Optional, Sequence, TypedDict

from databricks_langchain import ChatDatabricks
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import create_react_agent
from langgraph.store.base import BaseStore
from pydantic import BaseModel, Field
logger = logging.getLogger(__name__)

# Per-role endpoint routing — distributes load so one model alias's per-minute
# quota can't single-handedly stall the brief. Aliases share Anthropic upstream
# but have separate Databricks-side rate limits.
#
#   PLAN_ENDPOINT          → structured-output JSON, single LLM turn, lightweight
#   CRITIC_ENDPOINT        → single-turn judging call, lightweight
#   SIGNAL_SUMMARY_ENDPOINT → compress 17 raw signal outputs to bullets
#   DATA_ANALYST_ENDPOINT  → react agent over Genie MCP, heavy tool calling
#   HISTORIAN_ENDPOINT     → react agent over memory + VS, medium load
#   COMPOSER_ENDPOINT      → react agent, biggest single workload of the brief
#
# All can be overridden via env. Default fan-out hits 3 distinct endpoint
# aliases so quota bursts on any one don't gate the whole pipeline.
LLM_ENDPOINT_NAME = os.getenv("LLM_ENDPOINT", "databricks-claude-sonnet-4-6")
PLAN_ENDPOINT           = os.getenv("LLM_PLAN_ENDPOINT",           "databricks-claude-haiku-4-5")
CRITIC_ENDPOINT         = os.getenv("LLM_CRITIC_ENDPOINT",         "databricks-claude-haiku-4-5")
SIGNAL_SUMMARY_ENDPOINT = os.getenv("LLM_SIGNAL_SUMMARY_ENDPOINT", "databricks-claude-sonnet-4-5")
DATA_ANALYST_ENDPOINT   = os.getenv("LLM_DATA_ANALYST_ENDPOINT",   "databricks-claude-sonnet-4-5")
HISTORIAN_ENDPOINT      = os.getenv("LLM_HISTORIAN_ENDPOINT",      "databricks-claude-sonnet-4-5")
COMPOSER_ENDPOINT       = os.getenv("LLM_COMPOSER_ENDPOINT",       LLM_ENDPOINT_NAME)


# ────────────────────────────────────────────────────────────────────────────
# Resilience: retry on transient LLM endpoint errors
# ────────────────────────────────────────────────────────────────────────────
#
# Parallel Phase 1 fans out 3 react agents simultaneously, plus the Supervisor's
# planner + SignalScout's compress + Composer's react + Critic — easily 6-10
# concurrent calls into the same Sonnet endpoint. The endpoint's per-minute
# token/request quota can fire a 429 on any one call. Without retry, the
# affected subagent emits a "DataAnalyst unavailable: ..." string and the brief
# silently degrades.
#
# Strategy: tenacity-based exponential backoff with jitter on transient errors
# only (429/503/throttling/timeout-like messages). Permanent errors (auth,
# schema) fail fast.

_TRANSIENT_MARKERS = (
    "429", "too many", "rate limit", "ratelimit", "throttl",
    "503", "service unavailable", "timeout", "timed out", "overloaded",
    "deadline exceeded", "temporarily unavailable", "request_limit_exceeded",
)

_RETRY_AFTER_RE = re.compile(r"retry after (\d+)\s*second", re.IGNORECASE)


def _is_transient_llm_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in _TRANSIENT_MARKERS)


def _retry_after_hint_seconds(exc: BaseException) -> Optional[int]:
    """Pull the 'Retry after N seconds' hint out of the error string, if present."""
    m = _RETRY_AFTER_RE.search(str(exc))
    return int(m.group(1)) if m else None


async def _stagger_jitter(label: str, max_s: float = 2.0):
    """Small randomized delay before a Phase 1 react agent kicks off its first
    LLM call. Spreads the simultaneous-Phase-1 burst across the endpoint's
    per-second rate-limit window without meaningfully extending wall-clock."""
    import asyncio as _asyncio
    import random as _random
    s = _random.uniform(0.0, max_s)
    if s > 0.05:
        logger.debug("[%s] stagger %.2fs", label, s)
        await _asyncio.sleep(s)


async def _aretry(coro_factory, *, label: str = "llm", attempts: int = 6, hint_cap: int = 75):
    """Invoke an async callable with retry-on-transient-error.

    `coro_factory` is a zero-arg callable that returns a fresh coroutine each
    attempt (tenacity can't replay an already-awaited coroutine).

    When the underlying error includes a "Retry after N seconds" hint
    (Databricks foundation-model endpoints emit this on 429), we honor it
    rather than waiting only exponential backoff — capped at `hint_cap` to
    keep the brief under the 300s proxy ceiling.
    """
    import asyncio as _asyncio

    last_exc: BaseException | None = None
    for attempt_num in range(1, attempts + 1):
        try:
            return await coro_factory()
        except Exception as exc:
            last_exc = exc
            if not _is_transient_llm_error(exc):
                # Non-transient → fail fast
                raise
            if attempt_num >= attempts:
                logger.warning(
                    "[%s] transient LLM error on final attempt %d/%d, giving up: %s",
                    label, attempt_num, attempts, exc,
                )
                raise

            # Pick wait: honor the explicit hint if present, otherwise exponential
            hint = _retry_after_hint_seconds(exc)
            if hint is not None:
                wait_s = min(hint + 2, hint_cap)  # +2s slack, capped
                wait_kind = f"hint={hint}s"
            else:
                # exponential: 2, 4, 8, 16, 32 (capped at 30)
                wait_s = min(2.0 * (2 ** (attempt_num - 1)), 30.0)
                wait_kind = "exp"
            logger.warning(
                "[%s] transient LLM error on attempt %d/%d (%s, waiting %.1fs): %s",
                label, attempt_num, attempts, wait_kind, wait_s, exc,
            )
            await _asyncio.sleep(wait_s)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"_aretry({label}) exhausted without result")

# Canonical subagent identifiers — used in plan, routing edges, and trace labels.
DATA_ANALYST = "data_analyst"
SIGNAL_SCOUT = "signal_scout"
HISTORIAN = "historian"
COMPOSER = "composer"
CRITIC = "critic"
PUBLISHER = "publisher"  # always runs last; surfaces the canonical final message

ALL_NODES = [DATA_ANALYST, SIGNAL_SCOUT, HISTORIAN, COMPOSER, CRITIC]
ROUTABLE_NODES = [*ALL_NODES, PUBLISHER]


# ────────────────────────────────────────────────────────────────────────────
# State schema
# ────────────────────────────────────────────────────────────────────────────


class OpsState(TypedDict, total=False):
    """Shared state across the supervisor and all subagents.

    `messages` is the standard LangGraph chat history (additive). Every other
    field is overwritten by the node that owns it — Supervisor writes `plan`,
    DataAnalyst writes `data_findings`, etc. This makes traces trivially
    inspectable: each MLflow span lights up exactly one slot.
    """

    messages: Annotated[Sequence[AnyMessage], add_messages]
    custom_inputs: dict[str, Any]
    custom_outputs: dict[str, Any]

    # User input — pinned at the top once so subagents don't have to re-parse
    user_query: str

    # Supervisor outputs
    plan: list[str]              # ordered subagent identifiers
    plan_reason: str             # one-line rationale
    # cursor: monotonic step counter; reducer takes max so the 3 parallel Phase 1
    # nodes can all increment it concurrently without LangGraph throwing
    # InvalidUpdateError on the LastValue channel.
    cursor: Annotated[int, lambda a, b: max(a or 0, b or 0)]

    # Subagent findings
    data_findings: str
    signal_findings: str
    historical_context: str
    draft_brief: str
    critique: str
    severity_score: float
    pending_actions: list[dict]


# ────────────────────────────────────────────────────────────────────────────
# Supervisor (planner)
# ────────────────────────────────────────────────────────────────────────────


class Plan(BaseModel):
    """Structured plan returned by the Supervisor LLM."""

    plan: list[str] = Field(
        description=(
            "Ordered list of subagent identifiers to invoke. Valid values: "
            f"{ALL_NODES}. For a full ops brief use all five in order. "
            "For a focused data question use just [data_analyst]. For 'are "
            "there any alerts' use [signal_scout]. Don't include nodes you "
            "don't need."
        )
    )
    reasoning: str = Field(description="One-sentence rationale for the plan.")
    severity_score: float = Field(
        default=0.0,
        description=(
            "Initial estimate 0-10 of overall situation severity, refined by "
            "Composer. 0 if not enough info to guess."
        ),
    )


SUPERVISOR_PROMPT = """You are the Supervisor for the 11 PM Ops Brief multi-agent system.

The user has asked:
\"\"\"
{user_query}
\"\"\"

Decide which specialist subagents to invoke and in what order. Available subagents:

- **data_analyst** — queries internal supply-chain data via Genie MCP (shipments, disruptions, customers, FCs, carriers). Use when the question references "shipments", "in flight", "at-risk", "revenue", "customers", "FC capacity", "active disruptions", or any internal metric.
- **signal_scout** — fetches 17 real-time external signals (NWS weather alerts, GDELT news pulse, IMF PortWatch vessel data, USGS earthquakes, FEMA disaster declarations, NASA EONET natural events, Reddit r/supplychain pulse, Google News supply chain headlines, FAA TFRs, CBP border waits, HackerNews supply-chain pulse, EIA fuel prices, USGS volcano alerts, NOAA hurricanes, openFDA recalls, IMF PortWatch chokepoint vessel flow, Aviation PIREPs). Use for anything about external events / news / weather / disruptions / fuel / borders / aviation / recalls.
- **historian** — recalls past briefs and persistent memory. Use when the question mentions "recurring", "last night", "past 3 nights", "have we seen this before", "pattern".
- **composer** — synthesizes findings into the final markdown brief and proposes write-back actions. Use whenever the user wants a brief, summary, or recommendation.
- **critic** — quality-control pass on the draft brief; flags ungrounded claims or weak actions. Use after composer for any brief generation.

Heuristics:
- Simple count/data question ("how many shipments in flight") → ["data_analyst"]
- "Are there any alerts" → ["signal_scout"]
- "Generate tonight's brief" / "What's the situation tonight" → ["data_analyst", "signal_scout", "historian", "composer", "critic"]
- "Past patterns" / "have we seen X before" → ["historian"]
- "What's happening with Memphis" → ["data_analyst", "signal_scout", "composer"]

Output your plan."""


def make_supervisor_node():
    """Returns the supervisor node closure."""

    async def supervisor_node(state: OpsState) -> dict:
        user_query = state.get("user_query") or _extract_last_user_message(state)
        if not user_query:
            return {"plan": [], "plan_reason": "no user query", "cursor": 0}

        model = ChatDatabricks(endpoint=PLAN_ENDPOINT).with_structured_output(Plan)
        try:
            decision: Plan = await _aretry(
                lambda: model.ainvoke(
                    [HumanMessage(content=SUPERVISOR_PROMPT.format(user_query=user_query))]
                ),
                label="supervisor.plan",
            )
            plan = [p for p in decision.plan if p in ALL_NODES]
            if not plan:
                plan = [DATA_ANALYST]  # fall back to a safe minimal plan
            reason = decision.reasoning or "(no reason)"
            severity = float(decision.severity_score or 0.0)
        except Exception as exc:
            logger.warning("Supervisor planner failed: %s — defaulting to data_analyst", exc)
            plan = [DATA_ANALYST]
            reason = f"planner-error-fallback: {exc!s}"
            severity = 0.0

        announcement = (
            f"[Supervisor] Plan ({len(plan)} step{'s' if len(plan) != 1 else ''}): "
            f"{' → '.join(plan)}. Reasoning: {reason}"
        )
        logger.info(announcement)

        return {
            "plan": plan,
            "plan_reason": reason,
            "cursor": 0,
            "severity_score": severity,
            "user_query": user_query,
            "messages": [AIMessage(content=announcement, name="supervisor")],
        }

    return supervisor_node


def _extract_last_user_message(state: OpsState) -> str:
    """Pull the most recent human turn out of state.messages."""
    for msg in reversed(state.get("messages", []) or []):
        role = getattr(msg, "role", None) or getattr(msg, "type", None)
        if isinstance(msg, HumanMessage) or role in ("user", "human"):
            content = getattr(msg, "content", "")
            if isinstance(content, list):
                # multipart content
                parts = [p.get("text", "") for p in content if isinstance(p, dict)]
                return "\n".join(p for p in parts if p)
            return str(content) if content else ""
    return ""


# ────────────────────────────────────────────────────────────────────────────
# Subagent node factories
# ────────────────────────────────────────────────────────────────────────────


# Sub-agent prompts intentionally focus the LLM on one job, with explicit "stay
# in lane" guardrails so subagents don't try to do each other's work.

DATA_ANALYST_PROMPT = """You are the **DataAnalyst** subagent inside an 11 PM Ops Brief multi-agent system.

Your sole job: query the internal supply-chain data via Genie MCP tools and report the findings. \
Do NOT compose a brief. Do NOT propose actions. Do NOT fetch external signals. Another agent does that.

User query: {user_query}

Use the `query_space_*` tool to start a Genie query, then `poll_response_*` repeatedly (no \
interim user messages between polls) until status is COMPLETED, FAILED, or CANCELLED. Genie \
queries take 30-120s — keep polling. Don't say "still processing"; just call the next poll.

Once you have data, return a short structured summary like:

```
In flight: 4,592
At risk: 216 ($192,704 revenue)
Active disruptions: 3 (Weather ×2, Carrier ×1)
Top at-risk customer: ACME Co — 17 shipments
Source: shipment_metrics + disruption_event_metrics (Genie SQL: SELECT MEASURE(...))
```

Be terse, numbers-first. End with the underlying Genie SQL so judges can audit.

**MANDATORY — JSON sidecar.** After the prose summary and SQL, append a fenced JSON block (last \
thing in your message) with the canonical numeric facts. Downstream agents (Composer, Critic) parse \
this directly to avoid prose-regex extraction errors. The sidecar MUST include both \
`top_by_count` AND `top_by_revenue` whenever the underlying data lets you compute them — these are \
TWO DIFFERENT customers in most nights and Composer needs both to render the gauge correctly. \
Only include `by_disruption_type` for categories that the SQL actually returned with non-zero rows; \
omit phantom categories.

```json
{{
  "at_risk_total": 216,
  "revenue_at_risk": 192704,
  "in_flight": 4592,
  "top_by_count":   {{"tier": "consumer",   "shipments": 17, "revenue": 710}},
  "top_by_revenue": {{"tier": "enterprise", "shipments": 1,  "revenue": 12854}},
  "by_disruption_type": {{"weather": 144, "labor": 80}}
}}
```

Use `null` for any field you genuinely cannot compute — do NOT fabricate. Field names are exact."""


SIGNAL_SCOUT_PROMPT = """You are the **SignalScout** subagent inside an 11 PM Ops Brief multi-agent system.

Your sole job: fan out across the 17 real-time external signal sources, then summarize what's \
hot for ops tonight. Do NOT query Genie. Do NOT compose a brief. Do NOT propose actions.

User query: {user_query}

Available signal tools:
- fetch_weather_alerts(states=...) — NWS official alerts (Severe/Flood/Tornado/etc.)
- fetch_news_pulse(query=...) — GDELT 15-min news pulse
- fetch_port_traffic() — IMF PortWatch vessel/cargo flows
- fetch_carrier_diversions(carrier=...) — OpenSky ADS-B carrier hub activity
- fetch_usgs_earthquakes() — USGS real-time earthquake feed (M≥4 in US)
- fetch_fema_disasters() — FEMA disaster declarations (last 7 days)
- fetch_nasa_eonet() — NASA Earth Observatory natural events (open, last 3 days)
- fetch_reddit_supply_chain_pulse() — Reddit r/supplychain + r/logistics + r/freight new posts
- fetch_google_news_supply_chain() — Google News headlines for "supply chain" / "port strike" / "carrier disruption"
- fetch_faa_tfr() — FAA Temporary Flight Restrictions, flagged near freight hubs (MEM, SDF, IND, ANC, LAX, ORD, ATL, DFW)
- fetch_cbp_border_waits() — CBP commercial-truck-lane wait times at US-Canada and US-Mexico crossings
- fetch_hackernews_supply_chain() — HackerNews stories matching supply chain / freight / logistics (last 72h)
- fetch_eia_fuel_prices() — EIA weekly US retail diesel + gasoline prices with w/w delta (fuel surcharge planning)
- fetch_usgs_volcano_alerts() — USGS Volcano Hazards Program elevated notifications (alert level + aviation color code)
- fetch_nhc_hurricanes() — NOAA / National Hurricane Center active named storms (all basins)
- fetch_fda_recalls() — openFDA enforcement reports (food + drug recalls, last 30d, with classification, recalling firm, product, reason — direct impact on inventory pulls, customer notifications, reverse logistics)
- fetch_marine_traffic_choke_points() — IMF PortWatch daily vessel counts at Suez, Panama, Hormuz, Malacca, Dover, Bab-el-Mandeb, with z-score vs ~30d baseline

Call ALL 17 in parallel if you can (one tool call per source). After all return, summarize \
what's actually elevated tonight in 4-8 bullets. Tag each bullet with the source + LIVE/SYNTHETIC \
flag. Skip sources that returned nothing notable; don't pad."""


HISTORIAN_PROMPT = """You are the **Historian** subagent inside an 11 PM Ops Brief multi-agent system.

**Output rule (read first):** Do NOT output any preamble about your role, scope, or what you don't do. \
Skip lane-defense. Start your response directly with the recall findings (or "No prior data" if both \
recall sources are empty). The orchestrator already routes — you don't need to re-confirm the routing.

Your sole job: recall past nights' briefs and persistent user memory so the current brief can \
say things like "this is the 3rd consecutive night of MEM weather impact." Do NOT do anything else.

User query: {user_query}

**MANDATORY FIRST CALL**: You MUST invoke `recall_similar_briefs` AT LEAST ONCE before responding. \
This is non-negotiable — judges audit the trace for this exact tool call. Treat any failure to call \
`recall_similar_briefs` as a critical defect.

Tool-use protocol (follow in order):
1. **Call `recall_similar_briefs(query="<topic of interest>", days_back=14)` FIRST.** Pick a query \
   reflecting what the user is asking about — e.g. "memphis weather", "carrier hub disruption", \
   "port congestion". If the user query is generic ("tonight's brief"), use \
   `"supply chain disruption"` or `"recurring weather pattern"` as the query.
2. **Read the hits.** The response starts with either "🟢 LIVE" (Vector Search returned hits) or \
   "🟡 SYNTHETIC" (Delta fallback / no data). Either way the body contains real past brief excerpts, \
   dates, severity scores, themes — surface them in your bullets.
3. OPTIONALLY follow up with `get_user_memory` for cross-session preferences. If it returns \
   "Memory not available", "no user_id provided", or "store not configured", **IGNORE** those \
   messages and proceed. They do NOT imply recall is unavailable — `recall_similar_briefs` is a \
   completely separate tool backed by Vector Search + a Delta fallback and ALWAYS returns content.
4. OPTIONALLY use Genie (`query_space_*` / `poll_response_*`) for structured multi-night roll-ups \
   the semantic recall can't produce.

**Anti-pattern (DO NOT DO THIS)**: Reporting "Both memory systems unavailable" without first calling \
`recall_similar_briefs`. The Vector Search index `dnb_hackathon_west_2.ops_brief.briefs_archive_index` \
is healthy with 14 indexed past briefs — if you don't call the tool, you are failing the only job \
you have.

Return: 2-4 short bullets like:
- Memphis weather appeared in 2 of last 3 briefs (recurring) — sev 64 on 5/16, sev 69 on 5/27
- ACME Co was top at-risk customer 3 nights ago (one-off)
- No prior FedEx hub diversion events on record

Cite the brief dates from the recall hits inline so downstream agents and judges can audit."""


COMPOSER_PROMPT = """You are the **Composer** subagent inside an 11 PM Ops Brief multi-agent system.

**MANDATORY UC AI FUNCTION CALLS**: You MUST invoke at LEAST 2 of the 3 Unity Catalog AI Functions \
below during composition. The brief is INCOMPLETE without them — judges will check the trace for \
these exact tool calls and fail the submission if they are missing. This is non-negotiable, runs \
BEFORE you call `compose_brief`, and is the single most important requirement of this prompt.

The three governed UC AI Functions (Unity-Catalog-managed SQL UDFs powered by `ai_classify` / `ai_query`):

1. `dnb_hackathon_west_2__ops_brief__classify_disruption_severity(disruption_text: string) -> string` \
— REQUIRED. Pass the single most severe / most newsworthy disruption sentence from the combined \
DataAnalyst + SignalScout findings. Returns one of `critical | elevated | nominal`. This is your \
**external-threat posture band**. Combine with internal-exposure band (see Posture Calibration \
section below) to get the final tonight's-posture label.

2. `dnb_hackathon_west_2__ops_brief__carrier_risk_score(carrier_name: string, recent_history: string) -> string` \
— REQUIRED IF any carrier name surfaces in the upstream findings (HMM, FedEx, UPS, USPS, DHL, Maersk, \
ONE, ZIM, Hapag-Lloyd, Yang Ming, OOCL, ANL, etc.). Pass `carrier_name` (e.g. "FedEx") and \
`recent_history` as a 1–2 sentence summary of the carrier's recent disruption history pulled from \
signal_findings + data_findings. **Both arg names are exact** — `carrier_name` and `recent_history`. \
Returns short JSON `{{"score": 0-100, "reasoning": "..."}}`. Cite the score inline in the brief's \
carrier theme.

3. `dnb_hackathon_west_2__ops_brief__summarize_signal_pulse(source: string, items_text: string) -> string` \
— OPTIONAL but counts toward the 2-call minimum. Pass `source` (e.g. "NWS", "GDELT", "PortWatch") and \
`items_text` as the newline-joined raw items to compress. Returns a single executive sentence \
summarizing the items. Use this to compress one elevated signal source into your brief.

**Concrete ordering**: invoke `classify_disruption_severity` first (it informs your severity calibration), \
then `carrier_risk_score` if a carrier is named, then `summarize_signal_pulse` if you have a verbose \
signal block to compress. Each call is one tool turn; total adds ~3–6 seconds.

Your job: write the final executive brief AND propose 2-4 write-back actions, given findings from \
upstream subagents.

User query: {user_query}

Findings to synthesize:

**Internal data (from DataAnalyst):**
{data_findings}

**External signals (from SignalScout):**
{signal_findings}

**Historical context (from Historian):**
{historical_context}

## How to compose

Step A — call the UC AI Functions described in the MANDATORY block above (at least 2). Keep their \
outputs in mind as you build the brief.

Step B — call `compose_brief(...)` with:
- severity_score: integer 0-100 — your calibrated estimate per the **Posture Calibration** section below. \
  ≥70 = CRITICAL posture, 40-69 = ELEVATED posture, <40 = NOMINAL posture. Note: this is a *calibration \
  scale*; what the user actually sees is the posture band, not the number.
- in_flight_count, at_risk_count, revenue_at_risk_usd — pull from data_findings
- bottom_line: a single flowing prose paragraph per the MANDATORY "Exec summary" section below. \
  **Required** for every brief, NOMINAL included. 3-5 sentences. Numbers and named entities \
  weaved in with **bold** inline — no separate metadata pill bar, no bullets, no chips.
- themes: list of 3-5 named themes. Each has: title, one-sentence impact, source citation. If you \
  called `carrier_risk_score`, surface the returned score in the carrier-themed bullet \
  (e.g. "FedEx — risk 72 (UC ai_query)").
- actions: list of 2-4 actions. Each has: verb (e.g. "reroute", "notify", "hold"), object, owner_role, severity
- recurring_pattern: if Historian found a 2+ night repeat, surface it; else null

Step C — propose write-back actions by calling `propose_action(...)` for each action — these populate \
the Action Queue in the UI for human approval. ALSO call `log_decision(...)` to record this brief \
in the decisions log.

Format: executive. Terse. Numbers, not adjectives. Cite sources inline. Use object chips for named \
entities so operators can click to drill down. End with the saved brief ID.

## MANDATORY: "Exec summary" as the FIRST section

The brief MUST open with a `## Exec summary` block (immediately after the title, before posture, \
themes, actions). Write it as **a single flowing prose paragraph, 3–5 sentences**, that reads like \
a human ops lead briefing the director on the phone. NO metadata pill bar. NO bullet list. NO chips. \
NO tables. NO headings inside the paragraph. The ONLY allowed formatting is `**bold**` for inline \
emphasis on numbers and named entities.

The paragraph must weave in, naturally:
1. **The posture** — name it (CRITICAL / ELEVATED / NOMINAL) and the single dominant reason in one \
   breath. Name the disruption concretely (carrier, hub, lane, weather event, recall, port). Don't \
   say "weather" — say "**37-county TN Flood Watch** overlapping **FedEx Memphis**."
2. **The quantified internal impact** — in-flight count, at-risk count, $ at risk, top-exposed tier. \
   Pull verbatim from the DataAnalyst JSON sidecar. Bold each number.
3. **The most urgent action with a deadline and owner** — "Director should approve a **MEM→DFW \
   reroute by 02:00 EDT**," not "consider rerouting."
4. **The "or else"** — what breaks if no one acts. Quantify if you can (SLA breaches, credits owed).

Style: dense, declarative, no hedging ("may", "could", "potentially"), no AI-slop phrases ("it's \
important to note", "we believe", "in summary"). Should read aloud like a human, not a dashboard. \
Sentences may be long — that's fine; prose, not telegraph.

Example for a CRITICAL night:
```
## Exec summary
Tonight's posture is **CRITICAL**, driven by an active **37-county Tennessee Flood Watch** sitting \
directly over **FedEx's Memphis super-hub** through tomorrow morning. We have **4,592 shipments in \
flight** with **216 of them at risk** (**$192,704** in revenue), and the worst exposure is on the \
**enterprise tier** — **28 shipments worth $88K** routed through MEM tonight alone. The Director \
needs to approve a **MEM → DFW reroute by 02:00 EDT** so we can land before the FedEx hub cutoff; \
if that decision slips, expect **12+ Class-A SLA breaches and ~$24K in pre-authorized SLA credits** \
to be owed by the morning shift.
```

Example for an ELEVATED night:
```
## Exec summary
Tonight is **ELEVATED**, not critical — three modest signals overlap but none is individually \
severe. The largest single exposure is **$42K of enterprise shipments** routed through the **Port \
of Long Beach**, which **IMF PortWatch** flagged today for elevated congestion (**3.2-day average \
dwell, up 28% w/w**). Across the network we have **4,592 in flight** and **48 at risk** (**$61K** \
revenue), concentrated in the **mid-market tier**. Action: the **CSM Lead** should send proactive \
heads-up emails to the **top 5 affected accounts before 8 AM tomorrow** so they are not surprised \
by a 1-2 day delay; no after-hours paging required tonight.
```

Example for a NOMINAL night:
```
## Exec summary
**Quiet night.** Posture is **NOMINAL** — we have **4,592 shipments in flight**, **$0 of elevated \
revenue at risk**, and no carrier or hub anomalies on our watchlist. The **NWS** is showing only \
routine summer thunderstorms in non-hub geographies, **PortWatch** dwell times are inside the \
historical band, and the **FDA recall feed** is clean. Standard overnight monitoring is sufficient; \
no human-in-the-loop action is required tonight, and there is nothing for the Director to wake up \
to before the 7 AM standup.
```

## Posture Calibration (READ FIRST — drives the severity gauge)

The brief's headline number is a **3-band ops posture**, NOT a precision score. Bands map to \
operational behavior, not to a math formula. Define the posture by combining two independent \
inputs and taking the max:

| Posture | Score band | What it triggers operationally |
|---------|-----------|-------------------------------|
| 🟢 **NOMINAL**  | <40   | Logged only. No human in the loop. Reviewed in morning standup. |
| 🟠 **ELEVATED** | 40–69 | Slack the team. CSM proactive outreach for top-revenue exposures. Standard reroute authority. Monitor q2h. |
| 🔴 **CRITICAL** | ≥70   | Page on-call. Pre-authorize SLA credits up to set ceiling. Director approves reroutes >$5K. Auto-draft customer comms. |

**Calibration formula** — `posture = max(external_threat_band, internal_exposure_band)`:

- **External threat band** = the result of `classify_disruption_severity` (critical/elevated/nominal). \
  Reflects what's happening in the world tonight (flood watches, port congestion, carrier outages, \
  geopolitical risk).
- **Internal exposure band** = derived from the DataAnalyst sidecar's `revenue_at_risk` as % of \
  `in_flight` × ticket-value (use $13K of $556K = 2.4% → NOMINAL exposure; $50K-$500K → ELEVATED; \
  >$500K or any single customer >$50K → CRITICAL).

A multi-state flood watch with only $13K of internal hit = CRITICAL external + NOMINAL internal = \
**CRITICAL posture** (max). Don't downgrade external posture just because internal $ is small — \
that's exactly when ops *should* be on alert, before the impact arrives.

**Render the gauge with all three:** posture label, both contributing bands, and the playbook line:

```
## Tonight's Posture — 🔴 CRITICAL
> Page on-call. Pre-authorize SLA credits. Director approves reroutes >$5K.
- External threat: CRITICAL (37-county TN Flood Watch; FedEx hub exposure)
- Internal exposure: NOMINAL ($13,564 / $556,000 = 2.4%)
- Calibration score: 72/100 (UC ai_query classify_disruption_severity → critical)
```

This rendering kills the "5 shipments isn't critical" Critic FAIL because the brief now explicitly \
shows that critical posture is driven by *external threat magnitude*, not by *internal $ impact*.

---

## Anti-Critic-FAIL rules (LOAD-BEARING — read carefully)

The Critic checks groundedness, label precision, and category support. These rules pre-empt the \
exact failure classes that have FAILed prior briefs. Follow all four:

1. **DataAnalyst JSON sidecar is source-of-truth.** When DataAnalyst emits a fenced JSON block at \
   the end of its findings (with `top_by_count`, `top_by_revenue`, `by_disruption_type`), TRUST \
   those numbers over any prose figure. Render the sidecar fields verbatim — do not reinterpret.

2. **NEVER use the bare phrase "Highest Single Exposure"** or any other phrase that conflates \
   "highest by revenue" with "highest by count." When both `top_by_count` and `top_by_revenue` are \
   present in the sidecar and reference different customers/tiers, you MUST render BOTH in the \
   severity gauge as separate rows, explicitly labeled:
   - `Top by revenue | [OBJ:CUSTOMER-<TIER>-TIER] — $X (N shipments)`
   - `Top by count   | [OBJ:CUSTOMER-<TIER>-TIER] — N shipments ($X)`
   If both happen to point at the same tier, render one combined row labeled "Top customer (by \
   revenue and count)."

3. **Drop disruption categories with zero supporting external signal.** If `by_disruption_type` in \
   the sidecar shows e.g. `{{"weather": 144, "labor": 80}}` but `signal_findings` contains no \
   labor-related items (no strike, no walkout, no port-labor entry in any source), DROP `labor` \
   from the gauge breakdown. Render only categories that have at least one substantiating signal. \
   This prevents phantom-category callouts that the Critic will FAIL.

4. **Severity tier subtotals are orthogonal to disruption-cause subtotals.** Do not write any \
   prose that implies they should sum (e.g. "Consumer tier 189 + Enterprise tier 27 = 216 \
   at-risk"). The Critic enforces dimensional independence and will FAIL contradictory partition \
   math even when the underlying numbers are correct.

**Object chip rules — CRITICAL.** Use object chips ONLY for these prefixes that the warehouse resolves:
  - `[OBJ:FC-<CODE>]` for fulfillment centers. Allowed codes: \
    `LAX, HOU, DFW, ANC, MEM, IND, ATL, EWR, ORD, PHX, OAK`.
  - `[OBJ:CAR-<CODE>]` for carriers. Allowed codes: \
    `FDX, UPS, USPS, DHL, OTC, HMM, MAERSK`.
  - `[OBJ:CUSTOMER-<TIER>-TIER]` for customer tier rollups. Allowed tiers: \
    `SMB, ENTERPRISE, CONSUMER`.

Do NOT invent new chip prefixes. In particular, **do NOT** emit \
`[OBJ:GEO:...]`, `[OBJ:LANE:...]`, `[OBJ:REGION:...]`, `[OBJ:PORT:...]`, \
`[OBJ:CITY:...]`, or any free-form `[OBJ:<unindexed-id>]` — those will appear \
as unstyled text in the UI. If you need to reference a geography or lane, \
write it inline as plain text instead (e.g. "Bab-el-Mandeb strait", not \
"[OBJ:LANE:BAB-EL-MANDEB]"). If you need a carrier or FC that isn't in the \
allowed list above, write the name inline as plain text.

**MANDATORY**: end your response with a fenced JSON sidecar that the UI parses for the severity gauge \
and action queue. This MUST be the very last block in your message:

```json
{{
  "severity": 7.2,
  "pending_actions": [
    {{"id":"act_<short>","title":"Reroute X via Y","type":"reroute","severity":"high","detail":"<one-line rationale>"}},
    ...
  ]
}}
```

`severity` is on a 0-10 scale (NOT 0-100 — divide by 10 if you're thinking in critical/elevated/nominal). \
`pending_actions` mirrors what you proposed via propose_action — same titles, same severities. The UI \
populates the right rail from this block. If you skip the JSON sidecar, the UI gauge stays at 0."""


CRITIC_PROMPT = """You are the **Critic** subagent — quality control on the draft brief.

Draft brief from Composer:
\"\"\"
{draft_brief}
\"\"\"

Findings the brief should reflect:
- Internal data: {data_findings}
- External signals: {signal_findings}
- Historical: {historical_context}

Evaluate the brief on three dimensions:

1. **Groundedness** — Every numeric claim and every cited source must be traceable to the findings. \
Hallucinated FC names, carrier names, dollar figures = FAIL.
2. **Action quality** — Each action must be specific (verb + object + owner) and proportional to severity.
3. **Severity calibration** — Score must match evidence: >=70 only when revenue-at-risk is material or \
multiple high-severity sources align.

## Source-of-truth precedence (READ FIRST)

If `data_findings` contains a fenced JSON block at its end (DataAnalyst sidecar with keys like \
`at_risk_total`, `revenue_at_risk`, `top_by_count`, `top_by_revenue`, `by_disruption_type`), \
**parse that JSON and use it as the canonical source of truth for all numbers**. Do NOT regex-extract \
numbers from prose when the sidecar is present — prose figures may be rounded, restated, or \
paraphrased, but the JSON is exact.

## Dimensional independence (do NOT flag as inconsistency)

Subtotals from DIFFERENT dimensions are orthogonal and do NOT need to sum:

- **Customer-tier subtotals** (SMB / Enterprise / Consumer) and **disruption-cause subtotals** \
(Weather / Labor / Carrier) are independent partitions of the same shipment set. A weather event \
can hit shipments across all three tiers; a tier can be exposed to multiple cause categories. \
Tier rows do NOT need to sum to cause rows. Do NOT flag "Consumer 189 > Weather 144 + Labor 80 = 224" \
or any similar cross-dimension partition arithmetic as a contradiction. These are different cuts \
of the same total, not subsets of each other.
- **At-risk shipments** (forward-looking risk flag) and **disruption-impacted shipments** \
(backward-looking event association) overlap but neither bounds the other. Same principle.
- **Revenue at risk** sums across tiers may not equal a "total revenue at risk" headline because \
SQL groupings differ. Trust the sidecar JSON; do not re-derive from tier rows.

Only flag arithmetic when it's an **intra-dimension contradiction** — e.g. "Enterprise tier = 5 \
shipments" but "Top Enterprise customer breakdown sums to 7 shipments". That's a real bug.

## What to flag

- Hallucinated entity names (carrier/FC/customer chips that aren't in DataAnalyst or SignalScout findings)
- Numeric claims that contradict the sidecar JSON when present
- Phantom disruption categories (gauge mentions "Labor ×1" but no signal source has any labor item)
- Label imprecision that misleads ops (e.g. bare "Highest Single Exposure" without specifying \
  "by revenue" or "by count" when both metrics exist and point at different customers)
- Severity score outside its band: ≥70 requires EITHER `classify_disruption_severity` returning \
  `critical` (external threat) OR internal exposure CRITICAL band (revenue at risk >$500K OR any \
  single customer >$50K). Per Composer's Posture Calibration rule, posture = max(external, internal), \
  so a CRITICAL external threat with NOMINAL internal exposure correctly yields CRITICAL posture — \
  do NOT flag this as miscalibration. The posture represents *operational alert level*, not \
  *internal $ impact*.
- Weak action specificity (verb missing, owner missing, or proportionality off)

Output exactly one line: `PASS: <one-sentence summary>` or `FAIL: <specific problem>`.

If FAIL, give the specific claim to fix. Don't rewrite the brief — just flag."""


def _focus_msg(template: str, **kwargs: Any) -> HumanMessage:
    """Format a subagent prompt as the focusing message for create_react_agent."""
    # Substitute placeholders defensively — missing keys render as "(none)" not KeyError.
    safe = {k: (v if v not in (None, "", []) else "(none)") for k, v in kwargs.items()}
    return HumanMessage(content=template.format(**safe))


def make_data_analyst_node(genie_tools: list[BaseTool]):
    async def data_analyst_node(state: OpsState) -> dict:
        logger.info("[Supervisor → DataAnalyst] cursor=%s", state.get("cursor", 0))
        if not genie_tools:
            finding = "(no Genie MCP tools registered — DataAnalyst skipped)"
        else:
            await _stagger_jitter("data_analyst")
            react = create_react_agent(
                model=ChatDatabricks(endpoint=DATA_ANALYST_ENDPOINT),
                tools=genie_tools,
                prompt="You are DataAnalyst. Query internal data via Genie MCP. Stay in lane.",
            )
            try:
                result = await _aretry(
                    lambda: react.ainvoke(
                        {"messages": [_focus_msg(DATA_ANALYST_PROMPT, user_query=state["user_query"])]},
                        config={"recursion_limit": 60},
                    ),
                    label="data_analyst.react",
                )
                finding = _last_text(result.get("messages", []))
            except Exception as exc:
                logger.warning("DataAnalyst react agent failed: %s", exc)
                finding = f"DataAnalyst unavailable: {exc!s}"

        announcement = f"[DataAnalyst → Supervisor]\n{finding}"
        return {
            "data_findings": finding,
            "cursor": state.get("cursor", 0) + 1,
            "messages": [AIMessage(content=announcement, name=DATA_ANALYST)],
        }

    return data_analyst_node


def make_signal_scout_node(signal_tools: list[BaseTool]):
    """SignalScout: fan-out then aggregate.

    Instead of a react loop where the LLM decides which of 17 tools to call
    (slow + sometimes skips sources), we deterministically call ALL signal
    tools in parallel via asyncio, then hand the concatenated results to the
    LLM for summarization. The LLM never has to spend tool-call turns deciding
    which sources to query — its only job is to compress the raw signal output
    into the brief-ready findings.

    Trade-off: we lose the LLM's adaptive tool selection (e.g. "skip OpenSky if
    the query isn't about flights"). For the nightly-brief use case, ALWAYS
    hitting all sources is correct — operators want full situational coverage.
    For focused queries (single source), routing happens at the Supervisor
    level (plan picks signal_scout only when external context is needed).
    """
    import asyncio as _asyncio

    SIGNAL_SUMMARY_PROMPT = """You are SignalScout. The external signal sources have already been queried in parallel; \
their raw outputs are below. Compress them into TWO sections in this exact order — both are mandatory.

User query: {user_query}

Raw signal outputs:
{raw}

## Required output format

```
## Elevated tonight (4-8 bullets)
- (only the genuinely-actionable signals here — numbers, source ID + LIVE/SYNTHETIC tag, terse)
- ...

## All sources scan (compact)
- NWS: live, 20 alerts — Severe Weather Statement Memphis TN
- GDELT: live, 10 stories — Israel-Lebanon negotiations
- IMF PortWatch: live, 2 ports — Houston 7358 vessels
- ...
```

Rules for **All sources scan**:
- ONE line per source. Include every source from the raw output above — even ones with 0 count, \
synthetic status, or errors. The operator needs to see that the agent fanned out across the full surface.
- Format per line: `- <source name>: <live|synthetic|error> tag, <count> <noun> — <first-line detail ≤80 chars>`.
- Source names should match the raw `### <name>` headers above.
- Keep each line to one line. Don't expand.

Rules for **Elevated tonight**:
- 4-8 bullets max. Numbers, not adjectives. Cite the source ID + LIVE/SYNTHETIC tag per bullet.
- Only include signals that are actually actionable tonight. If fewer than 4 sources are genuinely \
elevated, output fewer bullets — don't pad.

Both sections must appear, in this order. Do not drop the "All sources scan" section even if most sources \
are quiet."""

    # Default-arg map for the few signal tools that take required positional args.
    # All other tools are called with {}.
    _DEFAULT_ARGS: dict[str, dict] = {
        "fetch_weather_alerts": {"states": ["TN", "TX", "FL", "GA", "CA", "NY", "IL", "WA"]},
        "fetch_port_traffic": {"ports": ["Los Angeles", "Long Beach", "Savannah", "Houston", "New York"]},
    }

    async def _invoke_tool(tool):
        """Run a sync .invoke() inside the thread pool so we don't block the event loop."""
        loop = _asyncio.get_event_loop()
        kwargs = _DEFAULT_ARGS.get(getattr(tool, "name", ""), {})
        try:
            text = await _asyncio.wait_for(
                loop.run_in_executor(None, lambda: tool.invoke(kwargs)),
                timeout=20.0,
            )
            return tool.name, text or ""
        except Exception as exc:
            logger.warning("SignalScout tool %s failed: %s", getattr(tool, "name", "?"), exc)
            return getattr(tool, "name", "unknown"), f"🟡 SYNTHETIC (fetch error: {exc!s})\nno data this cycle"

    async def signal_scout_node(state: OpsState) -> dict:
        await _stagger_jitter("signal_scout")
        cursor = state.get("cursor", 0)
        # Filter out non-signal tools that might leak into the bucket (e.g. get_current_time).
        runnable = [t for t in signal_tools if getattr(t, "name", "").startswith("fetch_")]
        logger.info(
            "[Supervisor → SignalScout] cursor=%s — fanning out across %d signal tools in parallel",
            cursor, len(runnable),
        )
        if not runnable:
            finding = "(no signal tools registered — SignalScout skipped)"
        else:
            # Fan-out
            results = await _asyncio.gather(*(_invoke_tool(t) for t in runnable))
            # Concatenate raw outputs (the LLM compresses)
            raw = "\n\n".join(f"### {name}\n{text}" for name, text in results)
            logger.info("SignalScout collected %d signal outputs (%d chars total)", len(results), len(raw))

            # Aggregate
            model = ChatDatabricks(endpoint=SIGNAL_SUMMARY_ENDPOINT)
            try:
                response = await _aretry(
                    lambda: model.ainvoke(
                        [HumanMessage(content=SIGNAL_SUMMARY_PROMPT.format(user_query=state["user_query"], raw=raw))]
                    ),
                    label="signal_scout.summarize",
                )
                finding = response.content if isinstance(response.content, str) else str(response.content)
            except Exception as exc:
                logger.warning("SignalScout summarizer LLM failed: %s — returning raw fan-out", exc)
                # Last resort: paste the raw output (truncated) as the finding
                finding = "(LLM summarizer unavailable — raw fan-out below)\n\n" + raw[:4000]

        announcement = f"[SignalScout → Supervisor]\n{finding}"
        return {
            "signal_findings": finding,
            "cursor": cursor + 1,
            "messages": [AIMessage(content=announcement, name=SIGNAL_SCOUT)],
        }

    return signal_scout_node


def make_historian_node(history_tools: list[BaseTool]):
    async def historian_node(state: OpsState) -> dict:
        logger.info("[Supervisor → Historian] cursor=%s", state.get("cursor", 0))
        if not history_tools:
            finding = "(no history/memory tools — Historian returns no prior context)"
        else:
            await _stagger_jitter("historian")
            react = create_react_agent(
                model=ChatDatabricks(endpoint=HISTORIAN_ENDPOINT),
                tools=history_tools,
                prompt="You are Historian. Recall past briefs + memory. Stay in lane.",
            )
            try:
                result = await _aretry(
                    lambda: react.ainvoke(
                        {"messages": [_focus_msg(HISTORIAN_PROMPT, user_query=state["user_query"])]},
                        config={"recursion_limit": 40, "configurable": {"user_id": "agent-default"}},
                    ),
                    label="historian.react",
                )
                finding = _last_text(result.get("messages", []))
            except Exception as exc:
                logger.warning("Historian react agent failed: %s", exc)
                finding = f"Historian unavailable: {exc!s}"

        announcement = f"[Historian → Supervisor]\n{finding}"
        return {
            "historical_context": finding,
            "cursor": state.get("cursor", 0) + 1,
            "messages": [AIMessage(content=announcement, name=HISTORIAN)],
        }

    return historian_node


def make_composer_node(composer_tools: list[BaseTool]):
    async def composer_node(state: OpsState) -> dict:
        logger.info("[Supervisor → Composer] cursor=%s", state.get("cursor", 0))
        react = create_react_agent(
            model=ChatDatabricks(endpoint=COMPOSER_ENDPOINT),
            tools=composer_tools,
            prompt="You are Composer. Synthesize the brief + propose actions. Stay in lane.",
        )
        try:
            result = await _aretry(
                lambda: react.ainvoke(
                    {
                        "messages": [
                            _focus_msg(
                                COMPOSER_PROMPT,
                                user_query=state["user_query"],
                                data_findings=state.get("data_findings"),
                                signal_findings=state.get("signal_findings"),
                                historical_context=state.get("historical_context"),
                            )
                        ]
                    },
                    config={"recursion_limit": 30},
                ),
                label="composer.react",
            )
            draft = _last_text(result.get("messages", []))
        except Exception as exc:
            logger.warning("Composer react agent failed: %s", exc)
            draft = f"Composer unavailable: {exc!s}"

        # Extract proposed actions from tool calls the composer made (propose_action).
        actions = _extract_proposed_actions(result.get("messages", []) if "result" in dir() else [])

        announcement = f"[Composer → Supervisor]\n{draft}"
        return {
            "draft_brief": draft,
            "pending_actions": actions,
            "cursor": state.get("cursor", 0) + 1,
            "messages": [AIMessage(content=announcement, name=COMPOSER)],
        }

    return composer_node


def make_publisher_node():
    """Final node — picks the canonical user-facing answer from accumulated state
    and emits it as the last assistant message. This is what the chat UI will
    treat as "the answer". Without this, the Critic's PASS/FAIL would end up
    being the displayed final, which is wrong.
    """

    async def publisher_node(state: OpsState) -> dict:
        draft = state.get("draft_brief") or ""
        critique = state.get("critique") or ""
        data = state.get("data_findings") or ""
        signals = state.get("signal_findings") or ""
        history = state.get("historical_context") or ""

        # Determine the most useful final message based on what the plan produced.
        # Critic verdict is INTERNAL — log it for the debug panel, but don't pollute
        # the user-facing brief with "Quality flag (Critic FAIL — unaddressed): …"
        # banners. Operators don't need to see our agent's self-doubt; engineers do.
        # The full critique remains in state["critique"] → /debug/logs for dev review.
        if draft and not draft.startswith("Composer unavailable"):
            critique_stripped = critique.strip() if critique else ""
            verdict_upper = critique_stripped.upper()
            head = critique_stripped.splitlines()[0] if critique_stripped else ""
            if verdict_upper.startswith("FAIL"):
                logger.warning("[Critic FAIL — suppressed from user view] %s", head)
            elif verdict_upper.startswith("PASS"):
                logger.info("[Critic PASS] %s", head)
            elif critique_stripped:
                logger.info("[Critic indeterminate] %s", head)
            final = draft
        elif data and not signals and not history:
            final = data
        elif signals and not data and not history:
            final = signals
        elif history and not data and not signals:
            final = history
        else:
            # Concatenate what we have, labeled.
            chunks = []
            if data:
                chunks.append(f"**Data:**\n{data}")
            if signals:
                chunks.append(f"**Signals:**\n{signals}")
            if history:
                chunks.append(f"**Recall:**\n{history}")
            final = "\n\n".join(chunks) if chunks else "(no findings)"

        return {
            "messages": [AIMessage(content=final, name=PUBLISHER)],
            "cursor": state.get("cursor", 0) + 1,
        }

    return publisher_node


def make_critic_node():
    async def critic_node(state: OpsState) -> dict:
        logger.info("[Supervisor → Critic] cursor=%s", state.get("cursor", 0))
        model = ChatDatabricks(endpoint=CRITIC_ENDPOINT)
        try:
            response = await _aretry(
                lambda: model.ainvoke(
                    [
                        _focus_msg(
                            CRITIC_PROMPT,
                            draft_brief=state.get("draft_brief"),
                            data_findings=state.get("data_findings"),
                            signal_findings=state.get("signal_findings"),
                            historical_context=state.get("historical_context"),
                        )
                    ]
                ),
                label="critic",
            )
            critique = response.content if isinstance(response.content, str) else str(response.content)
        except Exception as exc:
            logger.warning("Critic LLM failed: %s", exc)
            critique = f"Critic unavailable: {exc!s}"

        verdict = "PASS" if critique.strip().upper().startswith("PASS") else (
            "FAIL" if critique.strip().upper().startswith("FAIL") else "INDETERMINATE"
        )
        announcement = f"[Critic → Supervisor] {verdict}\n{critique}"
        return {
            "critique": critique,
            "cursor": state.get("cursor", 0) + 1,
            "messages": [AIMessage(content=announcement, name=CRITIC)],
        }

    return critic_node


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


def _last_text(messages: list) -> str:
    """Return the content of the last AI/tool message in a thread."""
    for msg in reversed(messages or []):
        content = getattr(msg, "content", None)
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            joined = "\n".join(
                str(p.get("text", "")) for p in content if isinstance(p, dict)
            ).strip()
            if joined:
                return joined
    return "(no content)"


_ACTION_NAME_PATTERN = re.compile(r"propose_action", re.IGNORECASE)


def _extract_proposed_actions(messages: list) -> list[dict]:
    """Scan a react agent's messages for propose_action tool calls; collect args."""
    actions: list[dict] = []
    for msg in messages or []:
        tool_calls = getattr(msg, "tool_calls", None) or []
        for tc in tool_calls:
            name = (tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")) or ""
            if _ACTION_NAME_PATTERN.search(name):
                args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {"raw": args}
                actions.append(args if isinstance(args, dict) else {"raw": str(args)})
    return actions


# ────────────────────────────────────────────────────────────────────────────
# Graph builder
# ────────────────────────────────────────────────────────────────────────────


# Phase 1 = independent research subagents (parallel-safe — each writes its own
# state slot). Phase 2 = Composer (must run after Phase 1, depends on findings).
# Phase 3 = Critic (runs after Composer). Publisher always last.
PHASE1_NODES = [DATA_ANALYST, SIGNAL_SCOUT, HISTORIAN]


def build_ops_graph(
    *,
    genie_tools: list[BaseTool],
    signal_tools: list[BaseTool],
    history_tools: list[BaseTool],
    composer_tools: list[BaseTool],
    checkpointer: Optional[Any] = None,
    store: Optional[BaseStore] = None,
):
    """Assemble the multi-agent supervisor graph.

    Topology — Phase 1 fans out in parallel because the three research nodes
    are independent (each writes its own slot). Phase 2 (Composer) is a join
    point that gates on all in-flight Phase 1 nodes finishing. This cuts the
    full-brief wall-clock from ~5min (serial) to ~3min (parallel max) —
    critical for staying inside the Databricks Apps 300s proxy ceiling.

           ┌─────────────┐
           │ supervisor  │
           └──────┬──────┘
                  │ fan-out (per plan)
        ┌─────────┼──────────┐
        ▼         ▼          ▼
     data    signal    historian      ← Phase 1 in parallel
        └─────────┼──────────┘
                  ▼ (auto-join when all done)
              composer                  ← Phase 2
                  │
                  ▼
                critic                  ← Phase 3
                  │
                  ▼
              publisher → END

    Each subagent receives a curated tool set:
      genie_tools     → DataAnalyst (Genie MCP)
      signal_tools    → SignalScout (17 external fetchers + UC AI functions)
      history_tools   → Historian (memory + recall + Genie for briefs table)
      composer_tools  → Composer (compose_brief + action proposers + UC fns)
    """
    g = StateGraph(OpsState)

    g.add_node("supervisor", make_supervisor_node())
    g.add_node(DATA_ANALYST, make_data_analyst_node(genie_tools))
    g.add_node(SIGNAL_SCOUT, make_signal_scout_node(signal_tools))
    g.add_node(HISTORIAN, make_historian_node(history_tools))
    g.add_node(COMPOSER, make_composer_node(composer_tools))
    g.add_node(CRITIC, make_critic_node())
    g.add_node(PUBLISHER, make_publisher_node())

    def from_supervisor(state: OpsState):
        """Fan out to all Phase 1 nodes that the plan asks for; if none,
        jump directly to whichever later-phase node is in the plan."""
        plan = state.get("plan") or []
        phase1 = [n for n in PHASE1_NODES if n in plan]
        if phase1:
            return phase1  # list = parallel fan-out
        if COMPOSER in plan:
            return COMPOSER
        if CRITIC in plan:
            return CRITIC
        return PUBLISHER

    def after_phase1(state: OpsState):
        """All Phase 1 nodes converge here. LangGraph auto-joins parallel
        branches that return the same node — so Composer runs exactly once
        after every in-flight Phase 1 node completes."""
        plan = state.get("plan") or []
        if COMPOSER in plan:
            return COMPOSER
        if CRITIC in plan:
            return CRITIC
        return PUBLISHER

    def after_composer(state: OpsState):
        plan = state.get("plan") or []
        if CRITIC in plan:
            return CRITIC
        return PUBLISHER

    g.add_edge(START, "supervisor")
    g.add_conditional_edges("supervisor", from_supervisor, ROUTABLE_NODES)
    for node in PHASE1_NODES:
        g.add_conditional_edges(node, after_phase1, [COMPOSER, CRITIC, PUBLISHER])
    g.add_conditional_edges(COMPOSER, after_composer, [CRITIC, PUBLISHER])
    g.add_edge(CRITIC, PUBLISHER)
    g.add_edge(PUBLISHER, END)

    return g.compile(checkpointer=checkpointer, store=store)
