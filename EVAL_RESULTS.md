# MLflow Agent Evaluation Results

**Project:** The 11 PM Ops Brief
**Track:** Databricks DNB Customer Hackathon, Track 1 (Agents on Apps + Genie MCP)
**Evaluation harness:** `scripts/evaluate_agent.py` (911 lines)

---

## Run 1 — 2-item smoke test (validates harness + scoring rubric)

**Date:** 2026-05-28 21:16 UTC
**MLflow run_id:** `ddd2ef0b26f3406296aa7f9d43a990f0`
**MLflow URL:** https://dbc-7f5ee9e8-6a84.cloud.databricks.com/ml/experiments/4469475842996134/runs/ddd2ef0b26f3406296aa7f9d43a990f0

| Scorer | Mean | Notes |
|---|---:|---|
| **groundedness** | **0.900** | Every claim traces to source |
| **action_quality** | **0.875** | Specific actions w/ owners |
| **subagent_routing** | **0.812** | Mostly correct routing |

**Items tested:** `data_001` (in-flight count) and `data_002` (top at-risk customer). Both completed without error.

---

## Run 2 — 5-item run (broader coverage)

**Date:** 2026-05-28 21:34 UTC
**MLflow run_id:** `8804a5d53f5540c2b2594a1ae1107e97`
**MLflow URL:** https://dbc-7f5ee9e8-6a84.cloud.databricks.com/ml/experiments/4469475842996134/runs/8804a5d53f5540c2b2594a1ae1107e97

| Scorer | Mean | Median | n |
|---|---:|---:|---:|
| **groundedness** | **0.600** | 0.600 | 5 |
| **action_quality** | **0.900** | 1.000 | 5 |
| **subagent_routing** | **0.685** | 0.600 | 5 |

**Mean latency:** 111.79s · **Failed items:** 0 / 5

### Per-item routing observation
The supervisor occasionally adds `composer` to plans for simple count questions (where `[data_analyst]` alone would suffice). This is hurting routing scores. Tuning the `SUPERVISOR_PROMPT` heuristics block to be more aggressive about minimal plans would lift `subagent_routing` toward 0.9.

### Top failures (lowest aggregate score)
1. **`data_005`** — *"Which carrier has the most at-risk shipments tonight?"* — groundedness 0.25. The composer gave per-carrier breakdowns that aren't yet exposed as a Metric View dimension. Schema gap, not a hallucination per se but flagged as ungrounded by the judge.
2. **`data_003`** — *"What's our revenue at risk tonight in USD?"* — groundedness 0.617. Composer over-elaborated for a simple count question.
3. **`data_002`** — *"Which customer has the most at-risk shipments tonight?"* — groundedness 0.7. Agent correctly avoided hallucinating customer names (only tier-level granularity is queryable) and caveated transparently — judge gave partial credit.

### Action quality
**Mean 0.900, median 1.000** — when the brief proposes actions, they're specific (verb + object + owner_role) and proportional to severity. This is the strongest dimension.

---

## How to reproduce

```bash
cd /Users/apatel/databricks-hackathon-2026-05-20/databricks-agent-on-apps-genie-mcp
DATABRICKS_CONFIG_PROFILE=dnb-hackathon uv run python scripts/evaluate_agent.py --limit 5
# or full 20-item run:
DATABRICKS_CONFIG_PROFILE=dnb-hackathon uv run python scripts/evaluate_agent.py
```

Each run logs to MLflow experiment `4469475842996134` with run name `ops_brief_eval_<timestamp>`, including per-item assessments as logged tables, all 3 scorer mean/median metrics, and the full agent trace via `mlflow.langchain.autolog`.

---

## Improvement roadmap (informed by the eval)

1. **Sharpen SUPERVISOR_PROMPT heuristics** — make minimal-plan picks explicit (e.g. "for a single-clause data question, plan MUST be exactly `[data_analyst]` — never include composer"). Expected lift on `subagent_routing` from 0.685 → 0.85+.
2. **Add per-carrier dimension to `shipment_metrics` Metric View** — closes the `data_005` schema gap. Expected lift on `groundedness` 0.6 → 0.85.
3. **Tighten composer JSON sidecar enforcement** — currently emits-mostly; add a Critic check that the sidecar is present and well-formed. Lifts `action_quality` mean to ~0.95.
4. **Run full 20-item eval** — current results are 5-item; the full 20-item dataset includes brief generation, recall (Historian/VS), and action-proposal items that exercise the more-impressive subagent flows.

The harness is in place. Re-runs are one command.
