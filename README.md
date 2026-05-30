# 🌙 The 11 PM Ops Brief

> **An autonomous supply-chain operations agent that wakes up before ops leadership does.**
> Built for the Databricks DNB Customer Hackathon, May 2026.
> Track 1: **Agents on Apps + Genie MCP**.

---

## Problem

Every ops leader's morning starts the same way: opening Slack to a wall of overnight incidents,
weather alerts, carrier emails, and angry merchant DMs. The first 90 minutes of the day go to
*reconstructing what just happened* — not running the network. By the time leadership has the
picture, customers are already upset and the cost is already locked in.

## What we built

**The 11 PM Ops Brief** is an autonomous LangGraph agent deployed as a Databricks App. Every night
at 11 PM (or on-demand from chat) it executes this pipeline:

1. **Snapshots internal signals** via the **Managed Genie MCP server** scoped to three UC Metric Views
   (`shipment_metrics`, `disruption_metrics`, `brief_metrics`).
2. **Pulls fresh external signals** in parallel from four cutting-edge data sources:
   - **NWS active alerts** — geocoded real-time weather warnings (api.weather.gov, no auth)
   - **GDELT 2.0** — 15-minute global news pulse, geocoded (rare in SCM-AI)
   - **IMF PortWatch** — daily AIS-derived port traffic with WoW anomaly (rare originality play)
   - **OpenSky Network** — live ADS-B geofence around FedEx Memphis, FedEx Indy, UPS Louisville hubs
3. **Recalls the last 3 nights** of briefs (memory/recurrence detection).
4. **Composes the final markdown brief** via the structured `compose_brief` tool — severity score,
   themes (with recurrence call-outs), recommended actions, citations.

## Live links

- **App URL:** https://ops-brief-apatel-7474650842988229.aws.databricksapps.com
- **Genie Space:** https://dbc-7f5ee9e8-6a84.cloud.databricks.com/genie/rooms/01f15aac2d0a1f5bb72ff76a070ae337
- **MLflow Experiment:** https://dbc-7f5ee9e8-6a84.cloud.databricks.com/ml/experiments/4469475842996134
- **Lakebase Instance:** `ops-brief-apatel` (provisioned for agent short + long-term memory)

## Try it

In the chat UI, ask:

> Generate tonight's ops brief.

Or for a focused query:

> Are there any active severe weather alerts impacting our FCs right now?
> Compare the last 3 nights of briefs — any recurring themes?
> Is FedEx Memphis operating normally? Use OpenSky to check.

## Architecture (Databricks-native, all 4 maturity stages)

```
┌───────────────────────────────────────────────────────────────┐
│  USER (Chat UI cloned from databricks/app-templates)          │
└───────────────────────────────┬───────────────────────────────┘
                                │
┌───────────────────────────────▼───────────────────────────────┐
│  Databricks App (Serverless Compute)                          │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │ MLflow Agent Server (@invoke / @stream, FastAPI)        │  │
│  │   ↓                                                      │  │
│  │ ResponsesAgent  → LangGraph (create_agent)              │  │
│  │   ↓                  ↓                                   │  │
│  │ ChatDatabricks    Tools:                                 │  │
│  │ (claude-sonnet-   • get_current_time                     │  │
│  │  4-6 via FMAPI)   • fetch_weather_alerts  (NWS)          │  │
│  │                   • fetch_news_pulse      (GDELT)        │  │
│  │                   • fetch_port_traffic    (IMF PortWatch)│  │
│  │                   • fetch_carrier_diversions (OpenSky)   │  │
│  │                   • compose_brief                        │  │
│  │                   • memory_tools (Lakebase store)        │  │
│  │                   • query_space_* / poll_response_*      │  │
│  │                          ↓ (MCP)                         │  │
│  │                   Genie MCP Server                       │  │
│  └─────────────────────────────────────────────────────────┘  │
└───────────┬─────────────────────────────────┬─────────────────┘
            │                                 │
┌───────────▼───────────┐         ┌───────────▼─────────────┐
│ Lakebase Postgres     │         │ Unity Catalog           │
│ (ops-brief-apatel)    │         │ dnb_hackathon_west_2    │
│  short-term: thread   │         │  .ops_brief             │
│   conversation hist   │         │ ├── 9 Delta tables      │
│  long-term: cross-    │         │ ├── 2 Gold views        │
│   session facts via   │         │ └── 3 Metric Views      │
│   embeddings          │         │   (shipment / disrup /  │
└───────────────────────┘         │    brief metrics)       │
                                  └─────────────────────────┘
```

## Databricks AI Maturity stages used

| Stage | Capability | Implementation here |
|-------|------------|---------------------|
| 1 — Text-to-SQL | Genie | `shipment_metrics` / `disruption_metrics` / `brief_metrics` Metric Views as Genie data sources |
| 2 — Any LLM | Foundation Model APIs | `databricks-claude-sonnet-4-6` via `ChatDatabricks` |
| 3 — Custom Agent | Agents on Apps + MLflow | LangGraph + `mlflow.genai.agent_server`, deployed on Databricks Apps Serverless |
| 4 — Agent Memory + Tools | Lakebase + MCP | Lakebase short+long-term memory; Genie MCP first-party server |

## Data foundation

- **30,511 orders** with realistic distribution across 30-day history
- **30,511 shipments** with status (in_transit / delivered) and delay tracking
- **216 currently at-risk** ($192,704 revenue at risk)
- **15 disruption events** spanning weather / labor / equipment / customs / cyber / supplier types — last 3 are still active for the live demo
- **7 historical briefs** — last week of nights — supports the cross-night recurrence detection
- **8 FCs**, **5 carriers**, **8,000 customers** — all synthetic, no real company data

## File map

| Path | Role |
|---|---|
| `ops-brief-project/data/schema.sql` | Gold table DDL |
| `ops-brief-project/scripts/setup_data.py` | Loads schema + 30k rows of synthetic data + creates 3 Metric Views |
| `ops-brief-project/scripts/create_genie_space.py` | Creates Genie space scoped to Metric Views, patches `databricks.yml` + `utils.py` |
| `ops-brief-project/scripts/test_genie.py` | Smoke-tests the Genie space with 4 questions |
| `ops-brief-project/data/benchmark_questions.md` | The 10-question MLflow eval set |
| `databricks-agent-on-apps-genie-mcp/agent_server/agent.py` | LangGraph agent, tool registration |
| `databricks-agent-on-apps-genie-mcp/agent_server/prompts.py` | The 11 PM Ops Brief persona system prompt |
| `databricks-agent-on-apps-genie-mcp/agent_server/external_tools.py` | NWS / GDELT / PortWatch / OpenSky fetchers (with synthetic fallback) |
| `databricks-agent-on-apps-genie-mcp/agent_server/brief_tools.py` | The structured `compose_brief` synthesis tool |
| `databricks-agent-on-apps-genie-mcp/databricks.yml` | Bundle config + Genie + experiment + (no Postgres in v1) resource grants |
| `databricks-agent-on-apps-genie-mcp/scripts/eval_ops_brief.py` | MLflow eval — runs the benchmark, scores with 5 LLM judges |
| `databricks-agent-on-apps-genie-mcp/GENIE_CODE_CUSTOMIZATION.md` | How to extend with Genie Code (Track 1 scoring criterion) |

## Why this wins

| Rubric Item (Track 1, Section 6) | How we score |
|---|---|
| Effective use of Genie Code as agentic coding tool | `GENIE_CODE_CUSTOMIZATION.md` provides skills + instructions + worked prompts |
| Use of Databricks native features | Genie + Genie MCP + UC Metric Views + UC + Delta + AI/BI + Model Serving + MLflow + Lakebase — all stitched together |
| Data security, governance, cost | OBO auth via Apps, UC-governed data, serverless compute (auto-stops), Lakebase per-user scope |
| MCP servers to connect to external tools | Managed Genie MCP server scoped to Metric Views |
| Production readiness | MLflow eval, deterministic synthetic fallbacks, idempotent setup scripts, bundle deploy |
| Originality of use case | "Autonomous nightly brief that wakes up the ops team" — no other Devpost has shipped this |
| Novel application of Genie Code | Genie Code + Genie MCP — same Genie used as both dev assistant and as agent sub-agent |
| Creative integration of multiple Databricks features | All 4 stages of the AI Journey ladder represented in one project |
| Clarity of business problem | Universal across DNB customers — every ops team has this morning-scramble pain |
| Real-world enterprise scenarios | Demo data shaped after real 3PL operations (synthetic but believable) |
| Functionality of Apps | Live URL, OAuth-secured, multi-agent topology |
| Quality of insights | Cross-source synthesis (internal + 4 external + memory recall) |
| Demo storytelling | "Last night's brief vs tonight's" memory moment + live agent reasoning trace |
