"""Lightweight per-request cost + tool-call telemetry.

Captures: duration, tool calls per subagent, approximate token count.
Surfaces via GET /debug/cost. The UI's debug panel reads from here.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

# Ring buffer of recent request telemetry
_TELEMETRY: deque[dict] = deque(maxlen=100)
_LIVE_TELEMETRY: dict[str, dict] = {}  # in-progress request_id → record

# Rough char→token heuristic (Claude family ~4 chars/token avg)
_CHARS_PER_TOKEN = 4.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _approximate_tokens(*texts: str) -> int:
    total_chars = sum(len(t or "") for t in texts)
    return int(total_chars / _CHARS_PER_TOKEN)


def _summarize_output(output_items: list) -> dict:
    """Count tool calls and subagent transitions from a Responses output list."""
    tool_calls: dict[str, int] = {}
    subagent_msgs: dict[str, int] = {}
    approx_tokens = 0

    for item in output_items or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type", "")
        if kind == "function_call":
            name = item.get("name", "unknown")
            tool_calls[name] = tool_calls.get(name, 0) + 1
            approx_tokens += _approximate_tokens(item.get("arguments", ""))
        elif kind == "function_call_output":
            approx_tokens += _approximate_tokens(item.get("output", ""))
        elif kind == "message":
            for c in item.get("content", []) or []:
                if isinstance(c, dict):
                    text = c.get("text") or c.get("output_text") or ""
                    approx_tokens += _approximate_tokens(text)
                    # Look for "[Supervisor]", "[DataAnalyst → ", etc. to count subagent transitions
                    if text.startswith("[") and "]" in text:
                        actor = text.split("]", 1)[0].lstrip("[")
                        actor = actor.split("→")[0].strip()
                        if actor:
                            subagent_msgs[actor] = subagent_msgs.get(actor, 0) + 1

    return {
        "tool_calls": tool_calls,
        "tool_call_total": sum(tool_calls.values()),
        "subagent_messages": subagent_msgs,
        "approx_tokens": approx_tokens,
    }


class TelemetryMiddleware(BaseHTTPMiddleware):
    """Wrap /invocations to capture wall-clock + parse output for tool/subagent counts."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path not in ("/invocations", "/responses"):
            return await call_next(request)

        request_id = f"req_{int(time.time() * 1000)}_{id(request) % 100000}"
        t0 = time.time()
        record = {
            "request_id": request_id,
            "path": path,
            "started_at": _now_iso(),
            "duration_seconds": None,
            "status": "in_progress",
            "tool_calls": {},
            "tool_call_total": 0,
            "subagent_messages": {},
            "approx_tokens": 0,
        }
        _LIVE_TELEMETRY[request_id] = record

        # NOTE: do NOT read request.body() here. Doing so drains the ASGI
        # receive() stream; while we can re-inject for the downstream handler,
        # Starlette's StreamingResponse independently listens for client
        # disconnect on receive() and chokes on the re-injection, surfacing as
        # "RuntimeError: Unexpected message received: http.request". Result:
        # SSE responses never reach the client. Input-token estimation must
        # come from the parsed response payload instead (or be skipped).

        try:
            response = await call_next(request)
            record["status_code"] = response.status_code
            record["status"] = "completed" if 200 <= response.status_code < 300 else "error"

            # CRITICAL: if this is a streaming (SSE) response, we MUST NOT buffer
            # the body_iterator — doing so blocks all chunks from reaching the
            # client and breaks the long-running stream. Pass straight through.
            content_type = (response.headers.get("content-type") or "").lower()
            if "text/event-stream" in content_type:
                record["streamed"] = True
                record["duration_seconds"] = round(time.time() - t0, 2)
                return response

            # JSON path — buffer for parsing
            body_bytes = b""
            async for chunk in response.body_iterator:
                body_bytes += chunk

            # Need to rebuild response since we consumed the iterator
            from starlette.responses import Response as _Response

            new_resp = _Response(
                content=body_bytes,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

            # Parse output items
            try:
                import json as _json
                payload = _json.loads(body_bytes.decode("utf-8", errors="ignore"))
                output = payload.get("output") if isinstance(payload, dict) else None
                if output:
                    summary = _summarize_output(output)
                    record.update(summary)
                    # Add the assistant-final-text tokens too
                    final_text = ""
                    for it in output:
                        if isinstance(it, dict) and it.get("type") == "message":
                            for c in it.get("content", []) or []:
                                if isinstance(c, dict):
                                    final_text += (c.get("text") or c.get("output_text") or "")
                    record["final_text_length"] = len(final_text)
            except Exception as exc:
                logger.debug("telemetry parse failed: %s", exc)

            record["duration_seconds"] = round(time.time() - t0, 2)
            return new_resp

        except Exception as exc:
            record["status"] = "exception"
            record["error"] = str(exc)
            record["duration_seconds"] = round(time.time() - t0, 2)
            raise
        finally:
            _TELEMETRY.append(record)
            _LIVE_TELEMETRY.pop(request_id, None)


def install_cost_telemetry(app: FastAPI) -> None:
    app.add_middleware(TelemetryMiddleware)

    @app.get("/debug/cost", include_in_schema=False)
    async def debug_cost(limit: int = 20):
        recent = list(_TELEMETRY)[-limit:]
        recent.reverse()
        in_progress = list(_LIVE_TELEMETRY.values())
        # Aggregate across the visible window
        agg_tool_calls: dict[str, int] = {}
        agg_subagent: dict[str, int] = {}
        total_tokens = 0
        total_duration = 0.0
        completed = 0
        for r in recent:
            for k, v in (r.get("tool_calls") or {}).items():
                agg_tool_calls[k] = agg_tool_calls.get(k, 0) + v
            for k, v in (r.get("subagent_messages") or {}).items():
                agg_subagent[k] = agg_subagent.get(k, 0) + v
            total_tokens += r.get("approx_tokens", 0)
            if r.get("duration_seconds"):
                total_duration += r["duration_seconds"]
                completed += 1
        return JSONResponse(
            {
                "in_progress": in_progress,
                "recent": recent,
                "aggregate": {
                    "tool_calls": agg_tool_calls,
                    "subagent_messages": agg_subagent,
                    "total_approx_tokens": total_tokens,
                    "avg_duration_seconds": round(total_duration / completed, 2) if completed else None,
                    "request_count": len(recent),
                },
            }
        )
