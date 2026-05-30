#!/usr/bin/env python3
"""
MLflow Agent Evaluation harness for the 11 PM Ops Brief multi-agent supervisor.

Runs a curated golden dataset of 20 questions against the LangGraph supervisor
agent (data_analyst, signal_scout, historian, composer, critic, publisher),
captures the final user-facing response *and* the set of subagents that fired,
and scores each item with three custom LLM-as-judge scorers:

    1. groundedness_scorer    — do numeric/named claims trace to retrieved data?
    2. action_quality_scorer  — are proposed actions specific, proportional, owned?
    3. subagent_routing_scorer — did the supervisor invoke the expected subagents?

All scoring is delegated to a Databricks foundation-model endpoint via
ChatDatabricks (default: databricks-claude-sonnet-4-6). Results are persisted
to MLflow:
    - mean/median per scorer as run metrics
    - per-item scores as a logged Pandas table artifact
    - full raw responses + traces as a JSON artifact

The script is async (the agent is async) but runs items sequentially to avoid
rate-limiting the upstream endpoints. With 20 items at 10–90s each, expect
roughly 15–30 minutes end-to-end.

USAGE
-----
    cd <repo-root>
    DATABRICKS_CONFIG_PROFILE=dnb-hackathon uv run python scripts/evaluate_agent.py
    # optional flags:
    #   --limit 5        run only the first N items (fast smoke test)
    #   --judge-model databricks-meta-llama-3-3-70b-instruct
    #   --experiment-name "ops_brief_eval_manual"
    #   --no-mlflow      print to stdout only, skip MLflow logging

CUSTOMIZATION
-------------
- Edit GOLDEN_DATASET below to add/remove questions. Each entry MUST specify
  `expected_subagents` (subset of {data_analyst, signal_scout, historian,
  composer, critic}) and `expected_traits` (free-form list of qualitative
  criteria the LLM judge checks). `category` is used only for the summary
  leaderboard.
- To add a new scorer: write an `async def my_scorer(question, response,
  trace_nodes, expected, judge) -> ScorerResult`, then append it to SCORERS.
- The judge prompt template lives in `_JUDGE_PROMPT_TEMPLATE` — keep the JSON
  schema enforcement so we can parse {score, reasoning} deterministically.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# Ensure the repo root is importable so `agent_server.*` resolves when the
# script is launched from anywhere (e.g. `uv run python scripts/...`).
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(dotenv_path=REPO_ROOT / ".env", override=False)

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("mlflow.utils.autologging_utils").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("evaluate_agent")
logger.setLevel(logging.INFO)


# ─────────────────────────────────────────────────────────────────────────────
# Golden dataset — 20 items across 5 categories (5 + 4 + 4 + 3 + 4)
# ─────────────────────────────────────────────────────────────────────────────
GOLDEN_DATASET: list[dict[str, Any]] = [
    # ── simple data questions (5) ────────────────────────────────────────────
    {
        "id": "data_001",
        "category": "data",
        "question": "How many shipments are currently in flight?",
        "expected_subagents": ["data_analyst"],
        "expected_traits": [
            "mentions an in-flight count",
            "cites Genie/SQL or internal data source",
            "does not hallucinate external weather/news",
        ],
    },
    {
        "id": "data_002",
        "category": "data",
        "question": "Which customer has the most at-risk shipments tonight?",
        "expected_subagents": ["data_analyst"],
        "expected_traits": [
            "names a single top customer or says none",
            "mentions an at-risk count or share",
            "no hallucinated customer names without data backing",
        ],
    },
    {
        "id": "data_003",
        "category": "data",
        "question": "What's our revenue at risk tonight in USD?",
        "expected_subagents": ["data_analyst"],
        "expected_traits": [
            "gives a USD figure or explicitly says unknown",
            "scopes to current/tonight",
            "does not invent revenue numbers",
        ],
    },
    {
        "id": "data_004",
        "category": "data",
        "question": "Show me active disruptions broken down by type.",
        "expected_subagents": ["data_analyst"],
        "expected_traits": [
            "groups by disruption type",
            "references the disruption_event_metrics view or equivalent",
            "no fabricated event types",
        ],
    },
    {
        "id": "data_005",
        "category": "data",
        "question": "Which carrier has the most at-risk shipments tonight?",
        "expected_subagents": ["data_analyst"],
        "expected_traits": [
            "names one carrier or says none/insufficient data",
            "includes a count or percentage",
            "does not invent carriers not present in the data",
        ],
    },
    # ── external signal questions (4) ────────────────────────────────────────
    {
        "id": "signal_001",
        "category": "signal",
        "question": "Are there any severe weather alerts in TN, TX, or GA right now?",
        "expected_subagents": ["signal_scout"],
        "expected_traits": [
            "explicitly addresses each of TN, TX, GA (or notes none)",
            "cites NWS or weather alert source",
            "does not invent alert IDs",
        ],
    },
    {
        "id": "signal_002",
        "category": "signal",
        "question": "Is there port congestion at LA or Long Beach right now?",
        "expected_subagents": ["signal_scout"],
        "expected_traits": [
            "references IMF PortWatch or port-level vessel data",
            "gives a directional answer (congested / normal / unknown)",
            "no invented vessel counts",
        ],
    },
    {
        "id": "signal_003",
        "category": "signal",
        "question": "Are there any notable USGS earthquakes today?",
        "expected_subagents": ["signal_scout"],
        "expected_traits": [
            "cites USGS",
            "includes magnitude or location if any reported",
            "says 'none notable' if nothing crossed threshold",
        ],
    },
    {
        "id": "signal_004",
        "category": "signal",
        "question": "What's trending on r/supplychain right now?",
        "expected_subagents": ["signal_scout"],
        "expected_traits": [
            "summarizes Reddit r/supplychain headlines",
            "does not fabricate post titles",
            "notes recency or freshness",
        ],
    },
    # ── multi-source brief questions (4) ─────────────────────────────────────
    {
        "id": "brief_001",
        "category": "brief",
        "question": "Generate tonight's full ops brief.",
        "expected_subagents": [
            "data_analyst",
            "signal_scout",
            "historian",
            "composer",
            "critic",
        ],
        "expected_traits": [
            "contains internal data section (in-flight, at-risk, revenue)",
            "contains external signals section (weather/news/ports)",
            "references historical/recurring context if any",
            "ends with a severity score or summary verdict",
            "includes at least one proposed action",
        ],
    },
    {
        "id": "brief_002",
        "category": "brief",
        "question": "What's the situation at FedEx Memphis (MEM) hub tonight?",
        "expected_subagents": ["data_analyst", "signal_scout", "composer"],
        "expected_traits": [
            "mentions FedEx and/or MEM by name",
            "blends internal at-risk shipment counts with external signal (weather/OpenSky)",
            "no hallucinated diversions without data",
        ],
    },
    {
        "id": "brief_003",
        "category": "brief",
        "question": "Compose a brief for tomorrow morning's ops standup.",
        "expected_subagents": [
            "data_analyst",
            "signal_scout",
            "historian",
            "composer",
            "critic",
        ],
        "expected_traits": [
            "structured for a standup (headline + bullets)",
            "covers data, signals, and history",
            "actionable next steps for AM ops",
        ],
    },
    {
        "id": "brief_004",
        "category": "brief",
        "question": "What's the severity score for tonight, and why?",
        "expected_subagents": ["data_analyst", "signal_scout", "composer"],
        "expected_traits": [
            "emits a numeric severity score 0–10",
            "justifies the score with at least 2 evidence points",
            "score is proportional to evidence (no 9/10 from quiet data)",
        ],
    },
    # ── historical / recall questions (3) ────────────────────────────────────
    {
        "id": "recall_001",
        "category": "recall",
        "question": "Have we seen Memphis weather disruptions in past briefs?",
        "expected_subagents": ["historian"],
        "expected_traits": [
            "queries past briefs / memory store",
            "answers yes/no with at least one referenced past brief if yes",
            "does not invent prior briefs",
        ],
    },
    {
        "id": "recall_002",
        "category": "recall",
        "question": "Are any disruption themes recurring across the last 7 nights?",
        "expected_subagents": ["historian"],
        "expected_traits": [
            "names specific recurring themes or says none",
            "scopes to last ~7 nights",
            "no fabricated themes without backing",
        ],
    },
    {
        "id": "recall_003",
        "category": "recall",
        "question": "What was last night's brief headline?",
        "expected_subagents": ["historian"],
        "expected_traits": [
            "returns a single headline string or says 'no brief found'",
            "scopes to the previous day",
            "no invented headlines",
        ],
    },
    # ── action-quality questions (4) ─────────────────────────────────────────
    {
        "id": "action_001",
        "category": "action",
        "question": "Recommend 3 concrete actions for tonight's ops brief.",
        "expected_subagents": ["data_analyst", "signal_scout", "composer"],
        "expected_traits": [
            "exactly 3 actions (or notes why fewer)",
            "each action has a verb + object + owner",
            "actions are proportional to severity (no nuclear options for quiet night)",
        ],
    },
    {
        "id": "action_002",
        "category": "action",
        "question": "Should we proactively notify customers in TN about weather risk?",
        "expected_subagents": ["signal_scout", "data_analyst", "composer"],
        "expected_traits": [
            "decisive yes/no recommendation",
            "grounds the call in both internal exposure and external alert",
            "names which team/role would send the notification",
        ],
    },
    {
        "id": "action_003",
        "category": "action",
        "question": "Is OnTrac stable enough to ship through tonight?",
        "expected_subagents": ["data_analyst", "signal_scout", "composer"],
        "expected_traits": [
            "binary stable / not-stable / partial answer",
            "cites OnTrac-specific data (at-risk count, lanes, weather)",
            "no carrier substitution (don't answer about FedEx instead)",
        ],
    },
    {
        "id": "action_004",
        "category": "action",
        "question": "Draft a short email to the FedEx carrier rep about MEM hub risk tonight.",
        "expected_subagents": ["data_analyst", "signal_scout", "composer"],
        "expected_traits": [
            "produces an email body with greeting + ask + signoff",
            "states the specific risk (weather / volume / diversion) with evidence",
            "tone is professional, not alarmist",
        ],
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# Scorer infrastructure
# ─────────────────────────────────────────────────────────────────────────────
_JUDGE_PROMPT_TEMPLATE = """You are a strict, fair evaluator of AI agent responses.

CRITERION: {criterion_name}
DESCRIPTION: {criterion_description}

USER QUESTION:
\"\"\"{question}\"\"\"

EXPECTED TRAITS (the response should satisfy these):
{expected_traits}

WHAT SUBAGENTS ACTUALLY FIRED:
{trace_nodes}

WHAT SUBAGENTS WERE EXPECTED:
{expected_subagents}

AGENT RESPONSE:
\"\"\"{response}\"\"\"

SCORING RUBRIC:
- 1.0 = fully satisfies the criterion; no caveats
- 0.75 = mostly satisfies, minor gap
- 0.5 = partial; one significant gap
- 0.25 = poor; major issue (hallucination, wrong subagent, vague)
- 0.0 = fails completely or is irrelevant

Respond ONLY with a JSON object on a single line, no markdown, no preamble:
{{"score": <float 0.0–1.0>, "reasoning": "<one or two sentences>"}}
"""

_SCORE_RE = re.compile(r"\{[^{}]*\"score\"[^{}]*\}", re.DOTALL)


@dataclass
class ScorerResult:
    name: str
    score: float
    reasoning: str
    error: str | None = None


@dataclass
class ItemResult:
    id: str
    category: str
    question: str
    expected_subagents: list[str]
    actual_subagents: list[str]
    response: str
    latency_seconds: float
    error: str | None = None
    scores: dict[str, ScorerResult] = field(default_factory=dict)


def _parse_judge_json(raw: str) -> dict[str, Any]:
    """Best-effort JSON parse — judges sometimes wrap in prose."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except Exception:
        pass
    m = _SCORE_RE.search(raw)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    # Last resort: hunt a float
    fm = re.search(r"\"score\"\s*:\s*([0-9.]+)", raw)
    if fm:
        return {"score": float(fm.group(1)), "reasoning": raw[:300]}
    return {"score": 0.0, "reasoning": f"could not parse judge output: {raw[:200]}"}


async def _call_judge(
    judge_model: Any,
    *,
    criterion_name: str,
    criterion_description: str,
    question: str,
    response: str,
    expected_traits: list[str],
    expected_subagents: list[str],
    trace_nodes: list[str],
) -> dict[str, Any]:
    prompt = _JUDGE_PROMPT_TEMPLATE.format(
        criterion_name=criterion_name,
        criterion_description=criterion_description,
        question=question,
        response=response[:6000],  # cap context — judge doesn't need a novella
        expected_traits="\n".join(f"  - {t}" for t in expected_traits) or "  - (none)",
        expected_subagents=", ".join(expected_subagents) or "(none)",
        trace_nodes=", ".join(trace_nodes) or "(none)",
    )
    try:
        resp = await judge_model.ainvoke([{"role": "user", "content": prompt}])
        raw = resp.content if isinstance(resp.content, str) else str(resp.content)
        parsed = _parse_judge_json(raw)
        score = float(parsed.get("score", 0.0))
        score = max(0.0, min(1.0, score))
        return {"score": score, "reasoning": parsed.get("reasoning", "")[:600]}
    except Exception as exc:
        logger.warning("Judge call failed for %s: %s", criterion_name, exc)
        return {"score": 0.0, "reasoning": f"judge error: {exc!s}"}


# ─────────────────────────────────────────────────────────────────────────────
# Three scorer functions. We define them as plain async functions and also
# register MLflow @scorer wrappers below so they show up in mlflow.genai
# evaluations if the user wants to plug them into mlflow.genai.evaluate().
# We don't *call* the @scorer wrappers in the manual loop — we call the async
# functions directly so we can pass async-only context (judge_model).
# ─────────────────────────────────────────────────────────────────────────────
async def groundedness_scorer(
    *, question: str, response: str, expected: dict[str, Any],
    trace_nodes: list[str], judge_model: Any,
) -> ScorerResult:
    """Every numeric/named entity (FC, carrier, customer, $) should trace to
    retrieved data. Hallucinated entities = low score."""
    result = await _call_judge(
        judge_model,
        criterion_name="groundedness",
        criterion_description=(
            "Examine every numeric claim, named entity (carrier, customer, "
            "fulfillment center, port, weather alert, dollar figure) in the "
            "response. Penalize entities the agent could not have retrieved "
            "given the subagents that fired. If the response says 'I don't "
            "have that data', that is GOOD groundedness, not bad. Score 1.0 "
            "for fully grounded, 0.0 for blatant hallucination."
        ),
        question=question,
        response=response,
        expected_traits=expected.get("expected_traits", []),
        expected_subagents=expected.get("expected_subagents", []),
        trace_nodes=trace_nodes,
    )
    return ScorerResult(
        name="groundedness", score=result["score"], reasoning=result["reasoning"]
    )


async def action_quality_scorer(
    *, question: str, response: str, expected: dict[str, Any],
    trace_nodes: list[str], judge_model: Any,
) -> ScorerResult:
    """For brief/action responses, are proposed actions specific (verb +
    object + owner), proportional to severity, and realistic?"""
    result = await _call_judge(
        judge_model,
        criterion_name="action_quality",
        criterion_description=(
            "If the question asks for a brief, recommendation, decision, or "
            "draft, evaluate the proposed actions. Each action should have: "
            "(a) a clear verb (notify, reroute, hold, escalate), (b) a "
            "specific object (which carrier/customer/lane), (c) a realistic "
            "owner role (ops lead, account manager, carrier rep — not "
            "'someone' or 'the team'), and (d) be proportional to the "
            "evidence. If the question is purely informational (no action "
            "asked for), score 1.0 by default — this criterion does not "
            "penalize informational answers."
        ),
        question=question,
        response=response,
        expected_traits=expected.get("expected_traits", []),
        expected_subagents=expected.get("expected_subagents", []),
        trace_nodes=trace_nodes,
    )
    return ScorerResult(
        name="action_quality", score=result["score"], reasoning=result["reasoning"]
    )


async def subagent_routing_scorer(
    *, question: str, response: str, expected: dict[str, Any],
    trace_nodes: list[str], judge_model: Any,
) -> ScorerResult:
    """Did the supervisor actually invoke the expected set of subagents?
    Deterministic Jaccard-style overlap on the canonical subagent names,
    *plus* an LLM rationality check for cases where the agent legitimately
    chose differently. Final score is the average."""
    expected_set = set(expected.get("expected_subagents", []))
    actual_set = set(trace_nodes) - {"supervisor", "publisher"}  # not "subagents"

    # Deterministic component: Jaccard on the canonical set.
    if not expected_set and not actual_set:
        jaccard = 1.0
    elif not expected_set or not actual_set:
        jaccard = 0.0
    else:
        jaccard = len(expected_set & actual_set) / len(expected_set | actual_set)

    # LLM rationality component: judge whether the actual routing was reasonable.
    result = await _call_judge(
        judge_model,
        criterion_name="routing_rationality",
        criterion_description=(
            "Given the user question, was the chosen set of subagents "
            "reasonable? It is fine to differ from the expected set IF the "
            "agent's choice still answers the question well. Score "
            "considering: did it miss a subagent it clearly needed? Did it "
            "over-invoke (waste cost) when one subagent would do? An exact "
            "match to expected is 1.0; a sensible deviation is 0.7–0.9; a "
            "clearly wrong routing is <0.3."
        ),
        question=question,
        response=response,
        expected_traits=[],
        expected_subagents=list(expected_set),
        trace_nodes=list(actual_set),
    )
    final_score = round(0.5 * jaccard + 0.5 * result["score"], 4)
    reasoning = (
        f"jaccard={jaccard:.2f} (expected={sorted(expected_set)}, "
        f"actual={sorted(actual_set)}); judge={result['score']:.2f} — "
        f"{result['reasoning']}"
    )
    return ScorerResult(name="subagent_routing", score=final_score, reasoning=reasoning)


SCORERS = [groundedness_scorer, action_quality_scorer, subagent_routing_scorer]


# ─────────────────────────────────────────────────────────────────────────────
# MLflow @scorer registration (decorative — for users who want to plug these
# scorers into mlflow.genai.evaluate() separately. Our manual loop uses the
# raw async functions above so we can pass the judge model in.)
# ─────────────────────────────────────────────────────────────────────────────
def _register_mlflow_scorers() -> None:
    """Register @scorer-decorated wrappers so they appear in MLflow's scorer
    registry. These wrappers expect `outputs` to be a dict with keys
    {response, trace_nodes} and `expectations` to carry expected_* keys."""
    try:
        from mlflow.genai.scorers import scorer  # type: ignore
    except Exception:
        return

    @scorer  # type: ignore[misc]
    def groundedness(outputs: dict, expectations: dict) -> float:  # noqa: ARG001
        # Placeholder: real scoring requires an async LLM call. Returning a
        # sentinel so anyone wiring this into mlflow.genai.evaluate() sees
        # they need to provide a judge model in scope.
        return float(outputs.get("groundedness_score", 0.0))

    @scorer  # type: ignore[misc]
    def action_quality(outputs: dict, expectations: dict) -> float:  # noqa: ARG001
        return float(outputs.get("action_quality_score", 0.0))

    @scorer  # type: ignore[misc]
    def subagent_routing(outputs: dict, expectations: dict) -> float:  # noqa: ARG001
        return float(outputs.get("subagent_routing_score", 0.0))


# ─────────────────────────────────────────────────────────────────────────────
# Agent driver — runs one item, captures response + which subagent nodes fired
# ─────────────────────────────────────────────────────────────────────────────
async def _run_one_item(item: dict[str, Any], item_idx: int) -> ItemResult:
    """Build a fresh agent (so memory state doesn't leak across items),
    stream it, and harvest the publisher's final message plus the set of
    node names that emitted updates."""
    from langgraph.checkpoint.memory import InMemorySaver

    from agent_server.agent import init_agent

    start = time.monotonic()
    trace_nodes: list[str] = []
    final_response: str = ""
    error: str | None = None

    try:
        agent = await init_agent(store=None, checkpointer=InMemorySaver())
        config = {
            "configurable": {"thread_id": f"eval-{item['id']}-{item_idx}"},
            "recursion_limit": 200,
        }
        input_state = {
            "messages": [{"role": "user", "content": item["question"]}],
            "user_query": item["question"],
            "custom_inputs": {},
        }

        last_publisher_msg = ""
        async for chunk in agent.astream(input_state, config, stream_mode=["updates"]):
            # `stream_mode=["updates"]` emits ("updates", {node_name: state_delta}).
            if not isinstance(chunk, tuple) or len(chunk) != 2:
                continue
            _mode, payload = chunk
            if not isinstance(payload, dict):
                continue
            for node_name, delta in payload.items():
                if node_name not in trace_nodes:
                    trace_nodes.append(node_name)
                # Grab the publisher's outgoing AIMessage as the canonical answer.
                if node_name == "publisher" and isinstance(delta, dict):
                    for msg in (delta.get("messages") or []):
                        content = (
                            msg.get("content")
                            if isinstance(msg, dict)
                            else getattr(msg, "content", "")
                        )
                        if content:
                            last_publisher_msg = content
        final_response = last_publisher_msg or "(no publisher output captured)"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc!s}"
        logger.exception("Item %s failed", item["id"])

    return ItemResult(
        id=item["id"],
        category=item["category"],
        question=item["question"],
        expected_subagents=list(item.get("expected_subagents", [])),
        actual_subagents=trace_nodes,
        response=final_response,
        latency_seconds=round(time.monotonic() - start, 2),
        error=error,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Scoring orchestrator
# ─────────────────────────────────────────────────────────────────────────────
async def _score_item(
    item_result: ItemResult, expected: dict[str, Any], judge_model: Any
) -> None:
    """Mutates item_result.scores in place."""
    if item_result.error:
        for fn in SCORERS:
            item_result.scores[fn.__name__.replace("_scorer", "")] = ScorerResult(
                name=fn.__name__.replace("_scorer", ""),
                score=0.0,
                reasoning=f"item failed: {item_result.error}",
                error=item_result.error,
            )
        return

    for fn in SCORERS:
        try:
            result = await fn(
                question=item_result.question,
                response=item_result.response,
                expected=expected,
                trace_nodes=item_result.actual_subagents,
                judge_model=judge_model,
            )
            item_result.scores[result.name] = result
        except Exception as exc:
            logger.exception("Scorer %s blew up", fn.__name__)
            item_result.scores[fn.__name__.replace("_scorer", "")] = ScorerResult(
                name=fn.__name__.replace("_scorer", ""),
                score=0.0,
                reasoning=f"scorer crashed: {exc!s}",
                error=str(exc),
            )


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation + reporting
# ─────────────────────────────────────────────────────────────────────────────
def _summarize(results: list[ItemResult]) -> dict[str, Any]:
    scorer_names = ["groundedness", "action_quality", "subagent_routing"]
    summary: dict[str, Any] = {
        "dataset_size": len(results),
        "errors": sum(1 for r in results if r.error),
        "mean_latency_seconds": round(
            sum(r.latency_seconds for r in results) / max(len(results), 1), 2
        ),
        "scorers": {},
    }
    for name in scorer_names:
        vals = [r.scores[name].score for r in results if name in r.scores]
        if not vals:
            summary["scorers"][name] = {"mean": 0.0, "median": 0.0, "n": 0}
            continue
        vals_sorted = sorted(vals)
        mid = len(vals_sorted) // 2
        median = (
            vals_sorted[mid]
            if len(vals_sorted) % 2
            else (vals_sorted[mid - 1] + vals_sorted[mid]) / 2
        )
        summary["scorers"][name] = {
            "mean": round(sum(vals) / len(vals), 4),
            "median": round(median, 4),
            "n": len(vals),
        }
    # Top 3 failures = lowest mean score across the 3 scorers
    def item_mean(r: ItemResult) -> float:
        if not r.scores:
            return 0.0
        return sum(s.score for s in r.scores.values()) / len(r.scores)

    ranked = sorted(results, key=item_mean)
    summary["top_failures"] = [
        {
            "id": r.id,
            "category": r.category,
            "question": r.question,
            "mean_score": round(item_mean(r), 3),
            "reasons": {n: s.reasoning for n, s in r.scores.items()},
        }
        for r in ranked[:3]
    ]
    return summary


def _print_leaderboard(summary: dict[str, Any]) -> None:
    print("\n" + "=" * 72)
    print(" 11 PM OPS BRIEF — AGENT EVAL LEADERBOARD")
    print("=" * 72)
    print(f"  dataset_size:        {summary['dataset_size']}")
    print(f"  failed_items:        {summary['errors']}")
    print(f"  mean_latency_sec:    {summary['mean_latency_seconds']}")
    print()
    print(f"  {'scorer':<22}{'mean':>8}{'median':>10}{'n':>6}")
    print(f"  {'-'*22}{'-'*8}{'-'*10}{'-'*6}")
    for name, s in summary["scorers"].items():
        print(f"  {name:<22}{s['mean']:>8.3f}{s['median']:>10.3f}{s['n']:>6}")
    print()
    print("  TOP 3 FAILURES")
    print("  " + "-" * 60)
    for tf in summary["top_failures"]:
        print(f"  [{tf['id']}] ({tf['category']}, mean={tf['mean_score']})")
        print(f"    Q: {tf['question']}")
        for sname, reason in tf["reasons"].items():
            print(f"      - {sname}: {reason[:140]}")
    print("=" * 72 + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# MLflow logging
# ─────────────────────────────────────────────────────────────────────────────
def _log_to_mlflow(
    results: list[ItemResult],
    summary: dict[str, Any],
    *,
    experiment_name: str | None,
    run_name: str,
    judge_model_name: str,
) -> str | None:
    try:
        import mlflow
        import pandas as pd
    except Exception as exc:
        logger.warning("MLflow/pandas unavailable, skipping logging: %s", exc)
        return None

    exp_id = os.getenv("MLFLOW_EXPERIMENT_ID")
    if exp_id:
        mlflow.set_experiment(experiment_id=exp_id)
    elif experiment_name:
        mlflow.set_experiment(experiment_name)

    _register_mlflow_scorers()

    with mlflow.start_run(run_name=run_name) as run:
        # Metrics
        mlflow.log_param("dataset_size", summary["dataset_size"])
        mlflow.log_param("judge_model", judge_model_name)
        mlflow.log_param("agent_endpoint", os.getenv("LLM_ENDPOINT", "databricks-claude-sonnet-4-6"))
        mlflow.log_metric("failed_items", summary["errors"])
        mlflow.log_metric("mean_latency_seconds", summary["mean_latency_seconds"])
        for name, s in summary["scorers"].items():
            mlflow.log_metric(f"{name}_mean", s["mean"])
            mlflow.log_metric(f"{name}_median", s["median"])

        # Per-item table
        rows = []
        for r in results:
            row = {
                "id": r.id,
                "category": r.category,
                "question": r.question,
                "expected_subagents": ",".join(r.expected_subagents),
                "actual_subagents": ",".join(r.actual_subagents),
                "latency_s": r.latency_seconds,
                "error": r.error or "",
                "response": (r.response or "")[:1200],
            }
            for sname, sval in r.scores.items():
                row[f"{sname}_score"] = sval.score
                row[f"{sname}_reasoning"] = sval.reasoning[:400]
            rows.append(row)
        df = pd.DataFrame(rows)
        mlflow.log_table(df, artifact_file="per_item_scores.json")

        # Full raw payload
        payload = {
            "summary": summary,
            "results": [
                {
                    **asdict(r),
                    "scores": {k: asdict(v) for k, v in r.scores.items()},
                }
                for r in results
            ],
        }
        artifact_path = Path("/tmp") / f"{run_name}_raw.json"
        artifact_path.write_text(json.dumps(payload, indent=2, default=str))
        mlflow.log_artifact(str(artifact_path))

        return run.info.run_id


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
async def _amain(args: argparse.Namespace) -> int:
    from databricks_langchain import ChatDatabricks

    dataset = GOLDEN_DATASET[: args.limit] if args.limit else GOLDEN_DATASET
    judge_model = ChatDatabricks(endpoint=args.judge_model, temperature=0.0)

    print(f"Running eval: {len(dataset)} item(s) against agent")
    print(f"Judge model: {args.judge_model}")
    print(f"Agent LLM:   {os.getenv('LLM_ENDPOINT', 'databricks-claude-sonnet-4-6')}")
    print()

    results: list[ItemResult] = []
    for idx, item in enumerate(dataset):
        print(f"  [{idx+1}/{len(dataset)}] {item['id']} — {item['question'][:60]}")
        item_result = await _run_one_item(item, idx)
        await _score_item(item_result, item, judge_model)
        score_str = " ".join(
            f"{n}={s.score:.2f}" for n, s in item_result.scores.items()
        )
        print(
            f"      → nodes={item_result.actual_subagents} "
            f"latency={item_result.latency_seconds}s {score_str}"
        )
        results.append(item_result)

    summary = _summarize(results)
    _print_leaderboard(summary)

    if not args.no_mlflow:
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        run_name = args.run_name or f"ops_brief_eval_{ts}"
        run_id = _log_to_mlflow(
            results,
            summary,
            experiment_name=args.experiment_name,
            run_name=run_name,
            judge_model_name=args.judge_model,
        )
        if run_id:
            host = os.getenv("DATABRICKS_HOST", "").rstrip("/")
            exp_id = os.getenv("MLFLOW_EXPERIMENT_ID", "")
            if host and exp_id:
                print(f"MLflow run: {host}/ml/experiments/{exp_id}/runs/{run_id}")
            else:
                print(f"MLflow run_id: {run_id}")

    return 0 if summary["errors"] == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Run only the first N items (fast iteration / smoke test).",
    )
    parser.add_argument(
        "--judge-model", default="databricks-claude-sonnet-4-6",
        help="Databricks foundation-model endpoint to use as LLM judge.",
    )
    parser.add_argument(
        "--experiment-name", default=None,
        help="MLflow experiment name (overridden by MLFLOW_EXPERIMENT_ID env var).",
    )
    parser.add_argument(
        "--run-name", default=None,
        help="MLflow run name (default: ops_brief_eval_<utc-ts>).",
    )
    parser.add_argument(
        "--no-mlflow", action="store_true",
        help="Skip MLflow logging — print summary to stdout only.",
    )
    args = parser.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
