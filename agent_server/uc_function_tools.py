"""Unity Catalog AI Functions as agent tools.

Three governed UC SQL UDFs (powered by `ai_classify` / `ai_query`) registered
by `scripts/setup_uc_functions.py` get loaded here as LangChain tools so the
multi-agent supervisor can call them like any other tool. Lineage, ACL,
billing all flow through Unity Catalog.

This module is best-effort: if the UC client can't be initialized (e.g. local
dev without warehouse access), we fall back to an empty list so the agent
still runs without UC tools.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

CATALOG = os.getenv("OPS_BRIEF_UC_CATALOG", "dnb_hackathon_west_2")
SCHEMA = os.getenv("OPS_BRIEF_UC_SCHEMA", "ops_brief")

UC_FUNCTION_FQNS = [
    f"{CATALOG}.{SCHEMA}.classify_disruption_severity",
    f"{CATALOG}.{SCHEMA}.summarize_signal_pulse",
    f"{CATALOG}.{SCHEMA}.carrier_risk_score",
]


_cached_tools: Optional[list] = None


def load_uc_function_tools() -> list:
    """Return the UC AI Function tools bound for LangChain. Memoized; safe to call repeatedly."""
    global _cached_tools
    if _cached_tools is not None:
        return _cached_tools

    try:
        from databricks_langchain import DatabricksFunctionClient, UCFunctionToolkit
    except Exception as exc:
        logger.warning("UCFunctionToolkit import failed: %s — UC tools disabled.", exc)
        _cached_tools = []
        return _cached_tools

    try:
        client = DatabricksFunctionClient()
        toolkit = UCFunctionToolkit(function_names=UC_FUNCTION_FQNS, client=client)
        tools = list(toolkit.tools)
        logger.info("Loaded %d UC AI Function tool(s): %s", len(tools), [t.name for t in tools])
        _cached_tools = tools
        return _cached_tools
    except Exception as exc:
        logger.warning("Failed to load UC AI Function tools (%s) — agent continues without them.", exc)
        _cached_tools = []
        return _cached_tools
