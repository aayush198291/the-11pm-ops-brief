"""Top-level agent entry point.

This module wires the LangGraph **multi-agent supervisor** (see supervisor.py)
into the MLflow ResponsesAgent stream/invoke contract that Databricks Apps
expects.

Topology built here per request:

    Supervisor → [DataAnalyst | SignalScout | Historian | Composer | Critic]

Tool routing:
    DataAnalyst   → genie MCP tools (read internal data)
    SignalScout   → 17 external real-time signal fetchers
    Historian     → memory tools + genie tools (briefs table)
    Composer      → brief composer + 4 action / write-back tools
    Critic        → none (LLM-only)
"""
import logging
from datetime import datetime
from typing import Any, AsyncGenerator, Optional

import mlflow
from databricks.sdk import WorkspaceClient
from langchain_core.tools import tool
from langgraph.store.base import BaseStore
from mlflow.genai.agent_server import invoke, stream
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentResponse,
    ResponsesAgentStreamEvent,
    to_chat_completions_input,
)

from agent_server.utils import (
    _get_or_create_thread_id,
    get_user_workspace_client,
    init_mcp_client,
    process_agent_astream_events,
)
from agent_server.utils_memory import (
    get_user_id,
    init_lakebase_config,
    lakebase_context,
    memory_tools,
)
from agent_server.external_tools import (
    fetch_weather_alerts,
    fetch_news_pulse,
    fetch_port_traffic,
    fetch_carrier_diversions,
)
from agent_server.external_tools_extra import EXTRA_TOOLS
from agent_server.external_tools_v3 import EXTRA_TOOLS_V3
from agent_server.brief_tools import compose_brief
from agent_server.action_tools import ACTION_TOOLS
from agent_server.recall_tools import RECALL_TOOLS
from agent_server.uc_function_tools import load_uc_function_tools
from agent_server.supervisor import build_ops_graph

logger = logging.getLogger(__name__)
mlflow.langchain.autolog()
logging.getLogger("mlflow.utils.autologging_utils").setLevel(logging.ERROR)

LLM_ENDPOINT_NAME = "databricks-claude-sonnet-4-6"
LAKEBASE_CONFIG = init_lakebase_config()
# Set to True by start_server.py's lifespan only if a real connection succeeded.
LAKEBASE_AVAILABLE: bool = False


def mark_lakebase_available(available: bool) -> None:
    """Called by start_server lifespan after a successful (or failed) connection probe."""
    global LAKEBASE_AVAILABLE
    LAKEBASE_AVAILABLE = available
    logger.info("LAKEBASE_AVAILABLE set to %s", available)


@tool
def get_current_time() -> str:
    """Get the current date and time."""
    return datetime.now().isoformat()


# Module-level tool buckets used by the supervisor graph. genie_tools is filled
# lazily per-request because it depends on the calling user's OBO workspace client.

_SIGNAL_TOOLS_STATIC = [
    fetch_weather_alerts,
    fetch_news_pulse,
    fetch_port_traffic,
    fetch_carrier_diversions,
    *EXTRA_TOOLS,
    *EXTRA_TOOLS_V3,
]


async def _fetch_genie_tools(workspace_client: WorkspaceClient) -> list:
    """Pull Genie MCP tools from the managed MCP server. Best-effort."""
    mcp_client = init_mcp_client(workspace_client)
    try:
        return await mcp_client.get_tools()
    except Exception:
        logger.warning("Failed to fetch Genie MCP tools — DataAnalyst/Historian will be limited.", exc_info=True)
        return []


async def init_agent(
    store: Optional[BaseStore],
    workspace_client: Optional[WorkspaceClient] = None,
    checkpointer: Optional[Any] = None,
):
    """Assemble the multi-agent supervisor graph for one request.

    Returns a compiled StateGraph (LangGraph) whose `.astream()` interface is
    compatible with the existing `process_agent_astream_events` translator.
    """
    if workspace_client is None:
        workspace_client = get_user_workspace_client()

    genie_tools = await _fetch_genie_tools(workspace_client)
    uc_tools = load_uc_function_tools()

    # Tool buckets per subagent
    data_tools = [get_current_time, *genie_tools]
    signal_tools = [get_current_time, *_SIGNAL_TOOLS_STATIC, *uc_tools]
    history_tools = [*memory_tools(), *genie_tools, *RECALL_TOOLS]
    composer_tools = [get_current_time, compose_brief, *ACTION_TOOLS, *uc_tools]

    logger.info(
        "Initializing supervisor graph: data=%d signal=%d history=%d composer=%d (genie_mcp=%d uc_fns=%d)",
        len(data_tools), len(signal_tools), len(history_tools), len(composer_tools),
        len(genie_tools), len(uc_tools),
    )

    return build_ops_graph(
        genie_tools=data_tools,
        signal_tools=signal_tools,
        history_tools=history_tools,
        composer_tools=composer_tools,
        checkpointer=checkpointer,
        store=store,
    )


@invoke()
async def invoke_handler(request: ResponsesAgentRequest) -> ResponsesAgentResponse:
    outputs = [
        event.item
        async for event in stream_handler(request)
        if event.type == "response.output_item.done"
    ]

    custom_outputs: dict[str, Any] = {}
    if user_id := get_user_id(request):
        custom_outputs["user_id"] = user_id
    return ResponsesAgentResponse(output=outputs, custom_outputs=custom_outputs)


@stream()
async def stream_handler(
    request: ResponsesAgentRequest,
) -> AsyncGenerator[ResponsesAgentStreamEvent, None]:
    thread_id = _get_or_create_thread_id(request)
    mlflow.update_current_trace(metadata={"mlflow.trace.session": thread_id})

    user_id = get_user_id(request)
    if not user_id:
        logger.warning("No user_id provided - memory features will not be available")

    config: dict[str, Any] = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": 200,
    }
    if user_id:
        config["configurable"]["user_id"] = user_id

    raw_messages = to_chat_completions_input([i.model_dump() for i in request.input])
    user_query = _extract_user_query(raw_messages)

    input_state: dict[str, Any] = {
        "messages": raw_messages,
        "user_query": user_query,
        "custom_inputs": dict(request.custom_inputs or {}),
    }

    # Lakebase memory: opt-in, gated on a successful startup probe (see start_server.py).
    from langgraph.checkpoint.memory import InMemorySaver
    checkpointer = None
    store = None
    lakebase_ctx = None
    if LAKEBASE_CONFIG is not None and LAKEBASE_AVAILABLE:
        try:
            lakebase_ctx = lakebase_context(LAKEBASE_CONFIG)
            checkpointer, store = await lakebase_ctx.__aenter__()
            logger.info("Lakebase memory acquired (instance=%s)", LAKEBASE_CONFIG.description)
        except Exception as e:
            logger.warning("Lakebase unexpectedly unavailable mid-request (%s); using InMemorySaver.", e)
            if lakebase_ctx is not None:
                try:
                    await lakebase_ctx.__aexit__(type(e), e, e.__traceback__)
                except Exception:
                    pass
                lakebase_ctx = None
            checkpointer = InMemorySaver()
            store = None
    else:
        checkpointer = InMemorySaver()
        store = None

    try:
        config["configurable"]["store"] = store
        agent = await init_agent(
            store=store,
            checkpointer=checkpointer,
            workspace_client=get_user_workspace_client(),
        )
        async for event in process_agent_astream_events(
            agent.astream(input_state, config, stream_mode=["updates", "messages"])
        ):
            yield event
    finally:
        if lakebase_ctx is not None:
            try:
                await lakebase_ctx.__aexit__(None, None, None)
            except Exception:
                logger.warning("Error closing Lakebase context", exc_info=True)


def _extract_user_query(messages: list) -> str:
    """Pull the last user-role message out of a chat-completions message list."""
    for msg in reversed(messages or []):
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
        if role in ("user", "human"):
            content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "")
            if isinstance(content, list):
                parts = []
                for p in content:
                    if isinstance(p, dict):
                        parts.append(p.get("text") or p.get("content") or "")
                    else:
                        parts.append(str(p))
                return "\n".join(parts).strip()
            return str(content or "").strip()
    return ""
