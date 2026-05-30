"""Debug endpoints for the deployed agent app.

Provides:
  GET /debug/health  — env vars, tool list, recent error count, Lakebase status
  GET /debug/logs    — last 500 log lines from an in-process ring buffer

The chat UI can also fetch these to inline debug info into the page so the user
can copy-paste full failure context without needing CLI access.
"""
from __future__ import annotations
import collections, logging, os, sys, traceback
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse


# In-memory rolling log buffer
_LOG_BUFFER: collections.deque[str] = collections.deque(maxlen=500)
_ERROR_COUNT: int = 0


class _RingHandler(logging.Handler):
    """Capture every log record into the ring buffer."""
    def emit(self, record: logging.LogRecord) -> None:
        global _ERROR_COUNT
        try:
            ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S")
            msg = self.format(record)
            line = f"{ts} {record.levelname:7s} {record.name}: {msg}"
            _LOG_BUFFER.append(line)
            if record.levelno >= logging.ERROR:
                _ERROR_COUNT += 1
        except Exception:
            pass


def install_log_capture() -> None:
    """Attach the ring-buffer handler to the root logger."""
    handler = _RingHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    # avoid double-install
    if not any(isinstance(h, _RingHandler) for h in root.handlers):
        root.addHandler(handler)


def install_debug_endpoints(app: FastAPI) -> None:
    @app.get("/debug/health", include_in_schema=False)
    async def _health():
        env = {
            "DATABRICKS_APP_NAME": os.getenv("DATABRICKS_APP_NAME") or "(unset — local)",
            "MLFLOW_EXPERIMENT_ID": os.getenv("MLFLOW_EXPERIMENT_ID"),
            "GENIE_SPACE_ID": os.getenv("GENIE_SPACE_ID"),
            "LAKEBASE_INSTANCE_NAME": os.getenv("LAKEBASE_INSTANCE_NAME"),
            "LAKEBASE_AUTOSCALING_ENDPOINT": os.getenv("LAKEBASE_AUTOSCALING_ENDPOINT"),
            "DATABRICKS_HOST": os.getenv("DATABRICKS_HOST"),
        }
        # Try to introspect agent tool list
        tools_summary: list[str] = []
        try:
            from agent_server import agent as _agent  # noqa: F401
            # The agent factory is async; we can't await here easily. Just list module-level @tool fns.
            import inspect
            from agent_server import external_tools, brief_tools
            for mod in (external_tools, brief_tools):
                for name, obj in inspect.getmembers(mod):
                    if getattr(obj, "name", None) and getattr(obj, "description", None):
                        tools_summary.append(f"{mod.__name__.split('.')[-1]}.{name}")
        except Exception as e:
            tools_summary.append(f"(introspection error: {e})")

        return JSONResponse({
            "ok": True,
            "ts": datetime.now(timezone.utc).isoformat(),
            "env": env,
            "python": sys.version.split()[0],
            "error_count_since_boot": _ERROR_COUNT,
            "tools_module_level": tools_summary,
        })

    @app.get("/debug/logs", include_in_schema=False)
    async def _logs(limit: int = 200, level: str = ""):
        lines = list(_LOG_BUFFER)
        if level:
            wanted = level.upper()
            lines = [l for l in lines if f" {wanted:7s} " in l or f" {wanted} " in l]
        lines = lines[-limit:]
        return PlainTextResponse("\n".join(lines) or "(no logs captured yet)")
