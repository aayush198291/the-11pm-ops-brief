"""Agent server entry point. load_dotenv must run before agent imports (auth config)."""

# ruff: noqa: E402
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# Load env vars from .env before any other imports (agent needs auth config)
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=True)

import logging

from databricks_ai_bridge.long_running import LongRunningAgentServer
from mlflow.genai.agent_server import setup_mlflow_git_based_version_tracking

logger = logging.getLogger(__name__)

# Need to import the agent to register the functions with the server
import agent_server.agent  # noqa: F401

from agent_server.agent import LAKEBASE_CONFIG, mark_lakebase_available
from agent_server.utils import replace_fake_id
from agent_server.utils_memory import run_lakebase_setup


class AgentServer(LongRunningAgentServer):
    def transform_stream_event(self, event, response_id):
        return replace_fake_id(event, response_id)


# NOTE: db_instance_name etc. are intentionally None — the deployed SP doesn't
# have Lakebase grant yet, so the LongRunningAgentServer's task-tracking DB
# would hang for 30s on every request. Without DB, the server runs synchronously
# which is fine for our demo (sub-90s responses).
agent_server = AgentServer(
    "ResponsesAgent",
    enable_chat_proxy=False,
    db_instance_name=None,
    db_autoscaling_endpoint=None,
    db_project=None,
    db_branch=None,
    task_timeout_seconds=float(os.getenv("TASK_TIMEOUT_SECONDS", "3600")),
    poll_interval_seconds=float(os.getenv("POLL_INTERVAL_SECONDS", "1.0")),
)

# Define the app as a module level variable to enable multiple workers
app = agent_server.app  # noqa: F841

# Install UI + debug + operational-console endpoints
from agent_server.chat_ui import install_chat_ui  # noqa: E402
from agent_server.debug_view import install_log_capture, install_debug_endpoints  # noqa: E402
from agent_server.endpoints import install_endpoints, _warm_start_signals  # noqa: E402
from agent_server.cost_telemetry import install_cost_telemetry  # noqa: E402

install_log_capture()
install_chat_ui(app)
install_debug_endpoints(app)
install_endpoints(app)
install_cost_telemetry(app)

setup_mlflow_git_based_version_tracking()

_original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _lifespan(app):
    # Probe Lakebase once at startup. If migrations succeed, mark it available so
    # stream_handler can use AsyncCheckpointSaver. If they fail (no SP grant,
    # unreachable, etc.) leave LAKEBASE_AVAILABLE=False so stream_handler skips
    # Lakebase entirely and uses InMemorySaver — avoiding 30s pool waits per request.
    if LAKEBASE_CONFIG is None:
        logger.info("Lakebase config not set; memory features degraded by design.")
        mark_lakebase_available(False)
    else:
        try:
            await run_lakebase_setup(LAKEBASE_CONFIG)
            mark_lakebase_available(True)
            logger.info("Lakebase probe succeeded; long-term memory enabled.")
        except Exception as exc:
            logger.warning(
                "Lakebase probe failed (%s). Memory falls back to InMemorySaver.", exc
            )
            mark_lakebase_available(False)

    # Fire-and-forget warm-start: prime the LKG cache ~5s after boot so the
    # first user hit on /signals/latest doesn't pay full cold-fan-out latency,
    # and any flaky source already has a prior-good entry for fallback.
    import asyncio as _asyncio
    _asyncio.create_task(_warm_start_signals(delay=5.0))

    try:
        async with _original_lifespan(app):
            yield
    except Exception as exc:
        logger.warning("Long-running DB initialization failed: %s. Background mode disabled.", exc)
        yield


app.router.lifespan_context = _lifespan


def main():
    agent_server.run(app_import_string="agent_server.start_server:app")
