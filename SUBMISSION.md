# The 11 PM Ops Brief — Submission

**Hackathon:** Databricks DNB Customer Hackathon
**Track:** **Track 1 [Technical]** — *Build an agent on apps with Genie under the hood*
**Workspace:** `dnb-hackathon-west-2` (Lighthouse environment)
**App URL:** https://ops-brief-apatel-7474650842988229.aws.databricksapps.com
**Genie space:** `01f15aac2d0a1f5bb72ff76a070ae337` — *Ops Brief — Disruption & Shipment Analytics* (data path for the DataAnalyst subagent via managed Genie MCP)
**MLflow experiment:** `4469475842996134`

---

## 1 · The Problem

Every supply-chain operator wakes up to the same dread question: *"what hit overnight?"*

Information that matters is spread across **internal data** (in-flight shipment counts, at-risk revenue, active disruptions) and **external signals** (severe weather, port congestion, news pulse, carrier diversions, fuel prices, border waits, earthquakes, hurricanes, public-health alerts). No human reads all of it at 11 PM. Most of it lands in a Slack channel or never reaches the operator at all.

**The 11 PM Ops Brief** is an autonomous multi-agent system that fuses all of it into a single grounded, executive-grade page — with proposed write-back actions — every night.

## 2 · What Got Built

### A multi-agent LangGraph supervisor (not a chatbot)

```
                              ┌─────────────┐
                              │  Supervisor │  ← LLM planner (structured output)
                              └──────┬──────┘
                                     │
       ┌────────────┬────────────────┼────────────────┬────────────┬───────────┐
       ▼            ▼                ▼                ▼            ▼           ▼
   ┌────────┐  ┌─────────┐     ┌──────────┐     ┌──────────┐  ┌────────┐  ┌──────────┐
   │ Data   │  │ Signal  │     │Historian │     │ Composer │  │ Critic │  │Publisher │
   │Analyst │  │ Scout   │     │          │     │          │  │        │  │          │
   └────────┘  └─────────┘     └──────────┘     └──────────┘  └────────┘  └──────────┘
   Genie MCP   17 external      Memory +         compose_brief  LLM-only   surfaces
   (Metric     signals +        Genie briefs     + 4 action     QC pass    canonical
    Views)     UC AI Fns        table recall     tools                     final msg
```

Each subagent is its own `create_react_agent` with a curated tool bucket. The Supervisor's `Plan` is a Pydantic-validated structured output that gets walked deterministically by the graph.

### 17 real-time external signal sources

| # | Source | What it tells us | Status |
|---|---|---|---|
| 1 | NWS Alerts | Severe weather (NOAA api.weather.gov) | LIVE |
| 2 | GDELT 2.0 | 15-min global news pulse | LIVE |
| 3 | IMF PortWatch | Port vessel/cargo flow | LIVE |
| 4 | OpenSky ADS-B | Live carrier hub air traffic | LIVE |
| 5 | USGS Earthquakes | M≥4 in US/territories | LIVE |
| 6 | FEMA Disasters | Last-7-day declarations | LIVE |
| 7 | NASA EONET | Active natural events | LIVE |
| 8 | Reddit RSS | r/supplychain + r/logistics + r/freight | LIVE |
| 9 | Google News RSS | Supply-chain headlines | LIVE |
| 10 | FAA TFRs | Temporary flight restrictions | LIVE |
| 11 | CBP Border Waits | Commercial-truck lane delays | LIVE |
| 12 | HackerNews Algolia | Supply-chain story pulse | LIVE |
| 13 | EIA Petroleum | US on-highway diesel $/gal | LIVE |
| 14 | USGS Volcanoes | Active alert volcanoes | LIVE |
| 15 | NOAA NHC | Active named storms | LIVE |
| 16 | openFDA Recalls | Food + drug recall enforcement reports (last 30d) | LIVE |
| 17 | IMF PortWatch chokepoints | Suez/Panama/Hormuz/Malacca/Dover vessel flow | LIVE |

Every tool returns `🟢 LIVE` or `🟡 SYNTHETIC (reason)` so the agent — and the UI tiles — know what they're looking at. Each has a deterministic synthetic fallback for demo reliability.

### Stage 2 of Databricks AI maturity ladder — UC AI Functions

Three governed SQL UDFs in `dnb_hackathon_west_2.ops_brief` powered by `ai_query` / `ai_classify`:
- `classify_disruption_severity(text)` → critical | elevated | nominal
- `summarize_signal_pulse(source, items)` → one executive sentence
- `carrier_risk_score(carrier, history)` → JSON `{score, reasoning}`

These are bound as LangChain tools via `UCFunctionToolkit` and shared across SignalScout + Composer. Lineage, ACL, billing all flow through Unity Catalog.

### Stage 3 — Build-your-own Agent on Apps + MLflow

- FastAPI MLflow Agent Server (`@invoke` / `@stream` decorators, MLflow 3.6)
- `mlflow.langchain.autolog()` captures every span — supervisor planning, each subagent's react loop, each tool call
- Compiled `StateGraph` over Pydantic-typed `OpsState`

### Stage 4 — Memory + MCP

- Managed Genie MCP server is the only data path (no direct SQL from the agent)
- `langchain_mcp_adapters` discovers + binds Genie tools at request time, using the calling user's OBO token
- Lakebase memory wired (currently disabled pending app SP grant — fallback to InMemorySaver)
- Vector Search index over 14-day briefs archive for semantic recall

### Action Layer — the difference between report and ops system

Four write-back tools the Composer can propose:
- `propose_action` — generic action with severity + owner_role
- `file_disruption_ticket` — Jira-shape disruption ticket
- `draft_carrier_email` — outbound comms draft
- `log_decision` — appends to a `decisions_log` (Delta + JSONL)

Each proposal lands in the UI's Action Queue pane for **human approval** before any side-effect. This is the Foundry HITL pattern.

### Operational Console UI (not a chatbot)

4-pane layout, light-theme-only, Nunito Sans + ShipBob design tokens:
- **Left rail** — 17 live signal tiles (status dot, count, last-updated), refreshed every 60s via `/signals/latest`
- **Center** — brief composition pane with rendered markdown, object chips (`[OBJ:FC-MEM]` → clickable, opens drill-down), provenance chips
- **Right rail** — Action Queue with Approve/Reject buttons
- **Bottom** — Agent Timeline showing every supervisor→subagent transition + tool call
- **Investigation panel** — slide-in object cards (Foundry-style): properties + links to related entities + active disruptions

### Production-readiness — MLflow Agent Evaluation

20-item golden dataset with 3 LLM-as-judge scorers:
- **Groundedness** — every numeric claim cites a data source
- **Action quality** — proposed actions are specific (verb + object + owner)
- **Subagent routing** — supervisor invoked the right specialist

Smoke-test (`--limit 2`): mean **groundedness 0.90 · action_quality 0.875 · subagent_routing 0.812**. Full 20-item run in MLflow experiment `4469475842996134`.

### Vertical workflow — Databricks Job (proactive mode)

Registered job `ops_brief_nightly_2300CT` (ID 65664408676995, paused) — nightly cron at 23:00 America/Chicago triggers the agent, archives the brief into `briefs_archive` for tomorrow's Historian to recall. Demonstrates the "agent that runs without prompting" pattern.

### Cost telemetry

`/debug/cost` endpoint surfaces per-request: duration, tool calls per subagent, approximate token use, aggregate across recent window. Wired as FastAPI middleware over `/invocations`.

## 3 · Demo Flow

(Full timestamped script in `DEMO_SCRIPT.md`.)

1. **0:00–0:20** Hook + landing on the operational console
2. **0:20–0:55** Click "Generate tonight's full ops brief" → watch the supervisor plan + dispatch 5 subagents in the timeline pane
3. **0:55–1:35** Brief renders with severity gauge animating → click an `[OBJ:FC-MEM]` chip → investigation panel pulls live properties from the warehouse
4. **1:35–2:05** Action queue populates with 4 proposed actions → approve one → flips to ✓ APPROVED
5. **2:05–2:30** Cut to MLflow workspace UI showing the eval scores
6. **2:30–2:55** Close on the architecture diagram with all 4 maturity stages highlighted

## 4 · Why This Wins the Rubric

| Rubric category | Weight | How this scores |
|---|---|---|
| **Technical Excellence** | 25% | Multi-agent LangGraph supervisor on a Databricks App; **Genie under the hood** via managed Genie MCP (`langchain_mcp_adapters` discovers + binds at request time, OBO token); 3 governed Unity Catalog AI Functions (`ai_classify`, `ai_query`); MLflow autolog tracing of every span; HTTPS + secret-scope + Service-Principal OAuth; cost telemetry; eval suite. Production-grade error paths (graceful Lakebase fallback, synthetic-data fallbacks per signal source, MCP tool fetch retries). |
| **Innovation & Creativity** | 25% | Plan-driven supervisor (not "ReAct + lots of tools"). Object chip → investigation drawer = Foundry pattern in a Databricks App. **Action layer** with HITL approval (4 write-back tools). 17 live external signal sources from public agencies that normally don't appear together (NWS, IMF PortWatch, USGS, FEMA, NASA, FAA, CBP, EIA, openFDA…). |
| **Business Impact & Relevance** | 25% | Every supply-chain ops team has this problem. Quantified: tonight ~4,592 in-flight shipments, $192K revenue at risk, 216 at-risk shipments tracked. The brief collapses a daily 30-min stand-up into a 30-second scan, with 4 proposed actions ready for human approval. |
| **Data & App Quality** | 15% | 4-pane operational console (Apps, not a chatbot): live signal tiles refreshing 60s, severity gauge, object drill-downs (FC → carrier → shipment), action queue, agent timeline. Light-theme, design-token-clean. Dense, utilitarian. |
| **Demo** | 10% | 3-min recorded video walking the full flow + architecture + MLflow eval scores. Script in `DEMO_SCRIPT.md`. |

## 5 · Submission Deliverables (Rule §4.3)

- ✅ **Working URL:** https://ops-brief-apatel-7474650842988229.aws.databricksapps.com
- ✅ **Code link:** this repository
- ✅ **Video demo:** recorded per `DEMO_SCRIPT.md` (see submission form attachment)
- ✅ **Project description:** this file + `README_OPS_BRIEF.md`
- ✅ **Lighthouse URL:** workspace `dnb-hackathon-west-2`
- ✅ **Original work:** built on top of the public Databricks `databricks-agent-on-apps-genie-mcp` template (open-source, permissive license); all hackathon-specific code/agents/UI authored May 20+ 2026.

## 6 · How To Reproduce

```bash
# 0. One-time auth
databricks auth login --host https://dbc-7f5ee9e8-6a84.cloud.databricks.com --profile dnb-hackathon

# 1. Synthetic data (~30k shipments + Metric Views + Genie space)
cd ops-brief-project
DATABRICKS_CONFIG_PROFILE=dnb-hackathon uv run python scripts/setup_data.py
DATABRICKS_CONFIG_PROFILE=dnb-hackathon uv run python scripts/create_genie_space.py
DATABRICKS_CONFIG_PROFILE=dnb-hackathon uv run python scripts/setup_uc_functions.py

# 2. Deploy the agent (LangGraph + 17 signals + UC functions + UI)
cd ../databricks-agent-on-apps-genie-mcp
DATABRICKS_CONFIG_PROFILE=dnb-hackathon databricks bundle deploy --profile dnb-hackathon
DATABRICKS_CONFIG_PROFILE=dnb-hackathon databricks bundle run ops_brief --profile dnb-hackathon

# 3. Run the MLflow eval
DATABRICKS_CONFIG_PROFILE=dnb-hackathon uv run python scripts/evaluate_agent.py --limit 5

# 4. Register the nightly job (paused by default — unpause via UI when ready)
cd ../ops-brief-project
DATABRICKS_CONFIG_PROFILE=dnb-hackathon uv run python scripts/register_nightly_job.py
```

## 7 · File Map

```
ops-brief-project/                       # synthetic data + Genie space + AI Functions + nightly job
├── data/
│   └── schema.sql                       # 9 Delta tables (FCs, carriers, customers, orders, shipments, disruptions, briefs)
├── scripts/
│   ├── setup_data.py                    # 30k synthetic rows + Metric Views (~3 min)
│   ├── create_genie_space.py            # Genie space + verified queries
│   ├── setup_uc_functions.py            # 3 UC AI Functions (ai_classify / ai_query)
│   ├── setup_briefs_archive.py          # 14-day briefs archive + Vector Search index
│   └── register_nightly_job.py          # Databricks Job (23:00 CT, PAUSED)

databricks-agent-on-apps-genie-mcp/      # the multi-agent app
├── agent_server/
│   ├── supervisor.py                    # LangGraph supervisor + 6 nodes
│   ├── agent.py                         # MLflow ResponsesAgent wiring
│   ├── external_tools.py                # 4 original signal tools (NWS/GDELT/PortWatch/OpenSky)
│   ├── external_tools_extra.py          # 5 more (USGS/FEMA/EONET/Reddit/GoogleNews)
│   ├── external_tools_v3.py             # 8 more (FAA/CBP/HN/EIA/Volcano/NHC/FDA/Chokepoints)
│   ├── uc_function_tools.py             # UCFunctionToolkit binding
│   ├── brief_tools.py                   # compose_brief
│   ├── action_tools.py                  # 4 write-back action tools + ring buffer
│   ├── recall_tools.py                  # Vector Search recall (Historian)
│   ├── investigation_tools.py           # Object lookup (drill-down)
│   ├── cost_telemetry.py                # Per-request token + tool-call counters
│   ├── endpoints.py                     # /signals/latest, /actions/*, /objects/lookup, /decisions
│   ├── chat_ui.py                       # 4-pane operational console UI (single page)
│   ├── debug_view.py                    # /debug/health, /debug/logs
│   ├── start_server.py                  # FastAPI assembly + lifespan
│   ├── utils_memory.py                  # Lakebase wiring (degraded fallback)
│   └── utils.py                         # MLflow integration + MCP client
├── scripts/
│   └── evaluate_agent.py                # 20-item golden + 3 LLM-as-judge scorers
├── databricks.yml                       # bundle config (app, resources)
├── DEMO_SCRIPT.md                       # 3-min recording plan + speaker script
└── SUBMISSION.md                        # this file
```
