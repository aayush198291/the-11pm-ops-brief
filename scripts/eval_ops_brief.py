#!/usr/bin/env python3
"""
MLflow-based evaluation of The 11 PM Ops Brief agent.

Runs a fixed benchmark question set through the agent in-process (no server needed),
scores responses with predefined LLM judges, and logs everything to the
experiment configured in .env (MLFLOW_EXPERIMENT_ID).

Outputs the experiment URL so you can link to it from the demo UI as the
"Quality 87%" badge.

Usage:
  uv run python scripts/eval_ops_brief.py
"""
from __future__ import annotations
import asyncio, logging, os, sys

import mlflow
from dotenv import load_dotenv
from mlflow.genai.agent_server import get_invoke_function
from mlflow.genai.scorers import (
    Completeness,
    Fluency,
    RelevanceToQuery,
    Safety,
    ToolCallCorrectness,
)
from mlflow.types.responses import ResponsesAgentRequest

# Quiet noisy logs
load_dotenv(dotenv_path=".env", override=True)
logging.getLogger("mlflow.utils.autologging_utils").setLevel(logging.ERROR)
logging.basicConfig(level=logging.WARNING)

# Importing agent registers @invoke handler
from agent_server import agent  # noqa: F401


BENCHMARK = [
    {"inputs": {"input": [{"role":"user","content":"How many shipments are currently in flight?"}]}},
    {"inputs": {"input": [{"role":"user","content":"What is the total revenue at risk right now, broken down by carrier?"}]}},
    {"inputs": {"input": [{"role":"user","content":"How many active disruption events are there, and which type is most common?"}]}},
    {"inputs": {"input": [{"role":"user","content":"Show me the last 3 nights of ops briefs — what themes appear in more than one?"}]}},
    {"inputs": {"input": [{"role":"user","content":"Are there any active severe weather alerts for TN, TX, or FL right now?"}]}},
    {"inputs": {"input": [{"role":"user","content":"Get a news pulse on supply chain disruptions in the last 6 hours."}]}},
    {"inputs": {"input": [{"role":"user","content":"What's the IMF PortWatch snapshot for Port of LA and Long Beach right now?"}]}},
    {"inputs": {"input": [{"role":"user","content":"Is FedEx Memphis operating normally — what does OpenSky show right now?"}]}},
    {"inputs": {"input": [{"role":"user","content":"Which carrier has the most at-risk shipments tonight?"}]}},
    {"inputs": {"input": [{"role":"user","content":"What is the average severity score of our last 7 nightly briefs?"}]}},
]


# Get the agent's invoke function and wrap for the eval harness
invoke_fn = get_invoke_function()
assert invoke_fn is not None, "Agent @invoke handler not registered — check imports."

if asyncio.iscoroutinefunction(invoke_fn):
    import nest_asyncio
    nest_asyncio.apply()
    def predict_fn(input, **kwargs):
        req = ResponsesAgentRequest(input=input)
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(invoke_fn(req)).model_dump()
else:
    def predict_fn(input, **kwargs):
        req = ResponsesAgentRequest(input=input)
        return invoke_fn(req).model_dump()


def main():
    exp_id = os.getenv("MLFLOW_EXPERIMENT_ID")
    if exp_id:
        mlflow.set_experiment(experiment_id=exp_id)
    host = os.getenv("DATABRICKS_HOST") or "https://dbc-7f5ee9e8-6a84.cloud.databricks.com"
    print(f"\nRunning evaluation on {len(BENCHMARK)} benchmark questions ...")
    print(f"Experiment: {host}/ml/experiments/{exp_id}\n")

    results = mlflow.genai.evaluate(
        data=BENCHMARK,
        predict_fn=predict_fn,
        scorers=[
            RelevanceToQuery(),
            Completeness(),
            Fluency(),
            Safety(),
            ToolCallCorrectness(),
        ],
    )
    print("\n=== Evaluation complete ===")
    print(results)
    print(f"\nView in MLflow: {host}/ml/experiments/{exp_id}")


if __name__ == "__main__":
    main()
