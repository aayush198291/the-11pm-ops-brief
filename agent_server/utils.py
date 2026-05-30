import json
import logging
import os
import time as time_mod
from typing import Any, AsyncGenerator, AsyncIterator, Optional
from uuid import uuid4

import uuid_utils
from databricks.sdk import WorkspaceClient
from langchain_core.messages import AIMessageChunk, ToolMessage

from agent_server.multi_server_mcp_client import (
    DatabricksMCPServer,
    DatabricksMultiServerMCPClient,
)
from mlflow.genai.agent_server import get_request_headers
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentStreamEvent,
    create_function_call_item,
    create_function_call_output_item,
    create_text_output_item,
)


def _get_or_create_thread_id(request: ResponsesAgentRequest) -> str:
    # priority of getting thread id:
    # 1. Use thread id from custom inputs
    # 2. Use conversation id from ChatContext https://mlflow.org/docs/latest/api_reference/python_api/mlflow.types.html#mlflow.types.agent.ChatContext
    # 3. Generate random UUID
    ci = dict(request.custom_inputs or {})

    if "thread_id" in ci and ci["thread_id"]:
        return str(ci["thread_id"])

    if request.context and getattr(request.context, "conversation_id", None):
        return str(request.context.conversation_id)

    return str(uuid_utils.uuid7())


def _is_databricks_app_env() -> bool:
    """Check if running in a Databricks App environment."""
    return bool(os.getenv("DATABRICKS_APP_NAME"))


def init_mcp_client(workspace_client: WorkspaceClient) -> DatabricksMultiServerMCPClient:
    host_name = get_databricks_host_from_env()
    return DatabricksMultiServerMCPClient(
        [
            DatabricksMCPServer(
                name="genie",
                url=f"{host_name}/api/2.0/mcp/genie/01f15aac2d0a1f5bb72ff76a070ae337",
                workspace_client=workspace_client,
                sse_read_timeout=600,
            ),
        ]
    )


def get_user_workspace_client() -> WorkspaceClient:
    # Local dev (not running on Databricks Apps): there is no x-forwarded
    # access token from an end user, so fall back to the CLI profile from .env.
    if not _is_databricks_app_env():
        profile = os.getenv("DATABRICKS_CONFIG_PROFILE")
        logging.info(
            "Local dev mode — using Databricks CLI profile %r for user workspace client",
            profile,
        )
        return WorkspaceClient(profile=profile) if profile else WorkspaceClient()

    token = get_request_headers().get("x-forwarded-access-token")
    logging.info("OBO token present: %s", bool(token))
    return WorkspaceClient(token=token, auth_type="pat")


def get_databricks_host_from_env() -> Optional[str]:
    try:
        # w = WorkspaceClient()
        w = get_user_workspace_client()
        return w.config.host
    except Exception as e:
        logging.exception("Error getting databricks host from env: %s", e)
        return None


_FAKE_ID_PREFIX = "resp_placeholder_"


def replace_fake_id(obj: Any, real_id: str) -> Any:
    """Recursively replace any resp_placeholder_* ID with real_id in dicts/lists/strings."""
    if isinstance(obj, dict):
        return {k: replace_fake_id(v, real_id) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [replace_fake_id(item, real_id) for item in obj]
    elif isinstance(obj, str) and obj.startswith(_FAKE_ID_PREFIX):
        return real_id
    return obj


async def process_agent_astream_events(
    async_stream: AsyncIterator[Any],
) -> AsyncGenerator[ResponsesAgentStreamEvent, None]:
    """Translate LangGraph (messages, updates) events into Responses-API stream events.

    Per-node isolation: LangGraph multi-agent supervisors can fan out parallel
    nodes (e.g. data_analyst + signal_scout + historian in Phase 1). Their
    token deltas arrive interleaved on the same `messages` stream. We key all
    streaming state by `langgraph_node` (from the chunk metadata) so deltas
    from concurrent nodes never get cross-attributed.

    `output_index` remains a single global monotonic counter — each new bubble
    (text or tool-call) reserves its slot, then subsequent deltas for that
    bubble reuse the reserved slot from per-node state. response.created /
    response.completed pairs are emitted per-node-turn, matching the
    pre-parallel single-node behavior (one create + one completed bracket per
    finalized message), but isolated so concurrent nodes don't share a turn.
    """
    response_id = f"{_FAKE_ID_PREFIX}{uuid4().hex[:16]}"
    output_index = 0

    # Per-node streaming state. Keyed by node name (from langgraph_node metadata
    # in the messages stream, or the dict key in the updates stream — these
    # MUST match for the same node, which is why both branches use the same key
    # space).
    #
    # active_text_by_node[node] = {"item_id": str, "content": str, "output_index": int}
    active_text_by_node: dict[str, dict] = {}
    # active_tool_calls_by_node[node][tool_call_index] = {item_id, name, args, call_id, output_index}
    active_tool_calls_by_node: dict[str, dict[int, dict]] = {}
    # in_turn_by_node[node] = bool — whether a turn is currently "open" for this node.
    # response.created fires when we transition False→True; response.completed fires
    # when we close out the node's final message in the updates branch.
    in_turn_by_node: dict[str, bool] = {}
    # Per-node turn-scoped output_items list (gets fed to response.completed.response.output).
    turn_output_items_by_node: dict[str, list[dict]] = {}

    def _response_obj(output: list[dict] | None = None) -> dict:
        return {
            "id": response_id,
            "created_at": time_mod.time(),
            "object": "response",
            "output": output or [],
            "status": None,
        }

    def _node_from_metadata(metadata: Any) -> str:
        """Extract a stable per-node key from LangGraph message metadata."""
        if not isinstance(metadata, dict):
            return "default"
        # Prefer langgraph_node (the human-readable node name set by the graph).
        # Fall back to checkpoint_ns (the sub-graph namespace) for subagents
        # that run inside react agents and don't surface langgraph_node directly.
        return (
            metadata.get("langgraph_node")
            or metadata.get("checkpoint_ns")
            or "default"
        )

    def _ensure_turn_started(node: str) -> Optional[ResponsesAgentStreamEvent]:
        """Open a turn for this node if not already open. Returns the
        response.created event to emit, or None if the turn was already open."""
        if in_turn_by_node.get(node):
            return None
        in_turn_by_node[node] = True
        turn_output_items_by_node[node] = []
        return ResponsesAgentStreamEvent(
            type="response.created",
            response=_response_obj(),
        )

    def _end_turn(node: str) -> Optional[ResponsesAgentStreamEvent]:
        """Close out a turn for this node. Returns the response.completed event
        with this node's accumulated output items."""
        if not in_turn_by_node.get(node):
            return None
        items = turn_output_items_by_node.pop(node, [])
        in_turn_by_node[node] = False
        # Also clear residual streaming state for the node — bubble is closed.
        active_text_by_node.pop(node, None)
        active_tool_calls_by_node.pop(node, None)
        return ResponsesAgentStreamEvent(
            type="response.completed",
            response=_response_obj(items),
        )

    async for event in async_stream:
        if event[0] == "messages":
            try:
                chunk = event[1][0]
                if not isinstance(chunk, AIMessageChunk):
                    continue

                # The metadata dict is the second element of the messages tuple.
                # When subagents stream tokens in parallel (Phase 1 fan-out),
                # each chunk's langgraph_node tells us which subagent it
                # belongs to — that's the only way to keep streams separate.
                metadata = event[1][1] if len(event[1]) > 1 else {}
                node_name = _node_from_metadata(metadata)

                started = _ensure_turn_started(node_name)
                if started is not None:
                    yield started

                # Tool-call chunks — keyed by (node, tool_call_index)
                if chunk.tool_call_chunks:
                    node_tool_calls = active_tool_calls_by_node.setdefault(node_name, {})
                    for tc_chunk in chunk.tool_call_chunks:
                        idx = tc_chunk.get("index", 0)
                        name = tc_chunk.get("name") or ""
                        tc_id = tc_chunk.get("id") or ""
                        args = tc_chunk.get("args") or ""

                        if idx not in node_tool_calls:
                            item_id = str(uuid_utils.uuid7())
                            node_tool_calls[idx] = {
                                "item_id": item_id,
                                "name": name,
                                "args": "",
                                "call_id": tc_id,
                                "output_index": output_index,
                            }
                            output_index += 1
                            yield ResponsesAgentStreamEvent(
                                type="response.output_item.added",
                                item={
                                    "type": "function_call",
                                    "id": item_id,
                                    "call_id": tc_id,
                                    "name": name,
                                    "arguments": "",
                                },
                                output_index=node_tool_calls[idx]["output_index"],
                            )
                        else:
                            tc_info = node_tool_calls[idx]
                            if name and not tc_info["name"]:
                                tc_info["name"] = name
                            if tc_id and not tc_info["call_id"]:
                                tc_info["call_id"] = tc_id

                        if args:
                            node_tool_calls[idx]["args"] += args
                            yield ResponsesAgentStreamEvent(
                                type="response.function_call_arguments.delta",
                                delta=args,
                                item_id=node_tool_calls[idx]["item_id"],
                                output_index=node_tool_calls[idx]["output_index"],
                            )

                # Text content — keyed by node so concurrent nodes don't
                # share a bubble. Each node gets its own item_id + reserved
                # output_index slot the first time it streams text in a turn.
                elif chunk.content:
                    content = chunk.content
                    text_state = active_text_by_node.get(node_name)
                    if text_state is None:
                        text_state = {
                            "item_id": str(uuid_utils.uuid7()),
                            "content": "",
                            "output_index": output_index,
                        }
                        active_text_by_node[node_name] = text_state
                        output_index += 1
                        yield ResponsesAgentStreamEvent(
                            type="response.output_item.added",
                            item={
                                "type": "message",
                                "id": text_state["item_id"],
                                "role": "assistant",
                                "status": "in_progress",
                                "content": [],
                            },
                            output_index=text_state["output_index"],
                        )
                        yield ResponsesAgentStreamEvent(
                            type="response.content_part.added",
                            item_id=text_state["item_id"],
                            output_index=text_state["output_index"],
                            content_index=0,
                            part={"type": "output_text", "text": "", "annotations": []},
                        )

                    text_state["content"] += content
                    yield ResponsesAgentStreamEvent(
                        type="response.output_text.delta",
                        delta=content,
                        item_id=text_state["item_id"],
                        content_index=0,
                        output_index=text_state["output_index"],
                    )

            except Exception as e:
                logging.exception(f"Error processing agent stream event: {e}")

        elif event[0] == "updates":
            # Updates carry the finalized messages each node emitted on this
            # tick. The dict key is the node name — same key space as the
            # langgraph_node metadata in the messages stream, so the bubble
            # we opened during streaming is the one we close here.
            for update_node, node_data in event[1].items():
                messages = node_data.get("messages", [])
                if not messages:
                    continue

                has_ai_message = False

                for msg in messages:
                    if isinstance(msg, ToolMessage):
                        # Tool result — standalone event, not part of any
                        # node's text bubble (call_id is globally unique).
                        content = msg.content if isinstance(msg.content, str) else json.dumps(msg.content)
                        item = create_function_call_output_item(
                            call_id=msg.tool_call_id,
                            output=content,
                        )
                        yield ResponsesAgentStreamEvent(
                            type="response.output_item.done",
                            item=item,
                        )

                    elif hasattr(msg, "tool_calls") and msg.tool_calls:
                        has_ai_message = True
                        started = _ensure_turn_started(update_node)
                        if started is not None:
                            yield started

                        # Look up this node's pending tool-call bubbles so
                        # we can attach the final args + emit done with the
                        # same item_id / output_index that the deltas used.
                        node_tool_calls = active_tool_calls_by_node.get(update_node, {})

                        for j, tc in enumerate(msg.tool_calls):
                            call_id = tc.get("id", "")
                            name = tc.get("name", "")
                            args = tc.get("args", {})
                            args_str = json.dumps(args) if isinstance(args, dict) else str(args)

                            tc_info = node_tool_calls.get(j)
                            if tc_info:
                                item_id = tc_info["item_id"]
                                matched_oi = tc_info["output_index"]
                            else:
                                # No streaming bubble existed for this tool call
                                # (e.g. a node that emitted the call atomically
                                # without token streaming). Allocate fresh slot.
                                item_id = str(uuid_utils.uuid7())
                                matched_oi = output_index
                                output_index += 1

                            item = create_function_call_item(
                                id=item_id,
                                call_id=call_id,
                                name=name,
                                arguments=args_str,
                            )
                            turn_output_items_by_node.setdefault(update_node, []).append(item)
                            yield ResponsesAgentStreamEvent(
                                type="response.output_item.done",
                                item=item,
                                output_index=matched_oi,
                            )

                        # Clear this node's tool-call bubbles — they're now done.
                        # Other nodes' bubbles are untouched.
                        active_tool_calls_by_node.pop(update_node, None)

                    elif hasattr(msg, "content") and msg.content:
                        has_ai_message = True
                        started = _ensure_turn_started(update_node)
                        if started is not None:
                            yield started

                        text = msg.content

                        # Look up this node's open text bubble. If it exists,
                        # close it with the same item_id + output_index that
                        # all the deltas were attributed to. If it doesn't
                        # (e.g. the node emitted content atomically via an
                        # updates-only path), open + close a fresh bubble.
                        text_state = active_text_by_node.get(update_node)
                        if text_state is not None:
                            item_id = text_state["item_id"]
                            bubble_oi = text_state["output_index"]
                        else:
                            item_id = str(uuid_utils.uuid7())
                            bubble_oi = output_index
                            output_index += 1
                            yield ResponsesAgentStreamEvent(
                                type="response.output_item.added",
                                item={
                                    "type": "message",
                                    "id": item_id,
                                    "role": "assistant",
                                    "status": "in_progress",
                                    "content": [],
                                },
                                output_index=bubble_oi,
                            )
                            yield ResponsesAgentStreamEvent(
                                type="response.content_part.added",
                                item_id=item_id,
                                output_index=bubble_oi,
                                content_index=0,
                                part={"type": "output_text", "text": "", "annotations": []},
                            )

                        yield ResponsesAgentStreamEvent(
                            type="response.content_part.done",
                            item_id=item_id,
                            output_index=bubble_oi,
                            content_index=0,
                            part={"type": "output_text", "text": text, "annotations": []},
                        )

                        item = create_text_output_item(text=text, id=item_id)
                        item["status"] = "completed"
                        turn_output_items_by_node.setdefault(update_node, []).append(item)
                        yield ResponsesAgentStreamEvent(
                            type="response.output_item.done",
                            item=item,
                            output_index=bubble_oi,
                        )

                        # Close this node's text bubble. Other nodes keep theirs.
                        active_text_by_node.pop(update_node, None)

                # Close out this node's turn — fire response.completed scoped to
                # this node's items only. Sibling parallel nodes' turns stay open.
                if has_ai_message:
                    completed = _end_turn(update_node)
                    if completed is not None:
                        yield completed

    # Defensive cleanup: if the stream ended with any node's turn still open
    # (e.g. updates never arrived for a node that streamed text), close them
    # all out so the client gets a clean completed envelope per opened turn.
    for stale_node in list(in_turn_by_node.keys()):
        completed = _end_turn(stale_node)
        if completed is not None:
            yield completed
