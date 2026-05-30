"""Vector-Search-backed semantic recall over past nightly ops briefs.

Stage 4 of the Databricks AI maturity ladder: real semantic memory.
The Historian subagent uses `recall_similar_briefs` to find past briefs
similar to a natural-language query — e.g. "Memphis weather impact"
should hit the recurring MEM weather entries.

Backing store:
    Delta table:    dnb_hackathon_west_2.ops_brief.briefs_archive
    Vector index:   dnb_hackathon_west_2.ops_brief.briefs_archive_index
    Embedding:      databricks-gte-large-en
    Endpoint:       prefer existing shared endpoint; falls back to ops_brief_vs

Fallback chain (each step is best-effort, never raises):
    1. Vector Search similarity_search → 🟢 LIVE results
    2. Delta SELECT last N briefs by date → 🟡 SYNTHETIC fallback
    3. Hard-coded "no recall available" string

Per project convention every output is a single string prefixed with
either "🟢 LIVE" or "🟡 SYNTHETIC".
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

CATALOG = os.getenv("OPS_BRIEF_UC_CATALOG", "dnb_hackathon_west_2")
SCHEMA = os.getenv("OPS_BRIEF_UC_SCHEMA", "ops_brief")
TABLE = "briefs_archive"
INDEX_NAME = f"{CATALOG}.{SCHEMA}.{TABLE}_index"
SOURCE_TABLE_FQN = f"{CATALOG}.{SCHEMA}.{TABLE}"
WAREHOUSE_ID = os.getenv("OPS_BRIEF_WAREHOUSE_ID", "110c24e02b899ae4")

PREFERRED_ENDPOINTS = ["ops_brief_vs", "dnb_hackathon"]
LIVE_BANNER = "🟢 LIVE"
SYNTH_BANNER = "🟡 SYNTHETIC"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ----------------------------------------------------------------------------
# Vector Search path
# ----------------------------------------------------------------------------
def _resolve_endpoint() -> Optional[str]:
    """Return the name of a usable VS endpoint, or None.

    Preference order: shared `dnb_hackathon` (if present), then `ops_brief_vs`,
    then whichever endpoint actually hosts our index.
    """
    try:
        from databricks.vector_search.client import VectorSearchClient
        vsc = VectorSearchClient(disable_notice=True)
        endpoints = vsc.list_endpoints().get("endpoints", []) or []
        names = [e.get("name") for e in endpoints]
        for n in PREFERRED_ENDPOINTS:
            if n in names:
                return n
        # Last resort — find an endpoint that hosts our index
        for n in names:
            try:
                idxs = vsc.list_indexes(n).get("vector_indexes", []) or []
                if any(i.get("name") == INDEX_NAME for i in idxs):
                    return n
            except Exception:
                continue
    except Exception as exc:
        logger.warning("recall_tools: VS endpoint resolution failed: %s", exc)
    return None


def _vs_similarity_search(query: str, num_results: int = 3) -> Optional[list[dict[str, Any]]]:
    """Run similarity_search against the briefs index. Returns None on any failure."""
    try:
        from databricks.vector_search.client import VectorSearchClient
        vsc = VectorSearchClient(disable_notice=True)
        endpoint = _resolve_endpoint()
        if not endpoint:
            logger.info("recall_tools: no VS endpoint available")
            return None
        ix = vsc.get_index(endpoint_name=endpoint, index_name=INDEX_NAME)
        result = ix.similarity_search(
            query_text=query,
            columns=[
                "brief_id",
                "brief_date",
                "severity_score",
                "headline",
                "body_markdown",
                "themes",
            ],
            num_results=num_results,
        )
        # result["result"]["data_array"] is a list of rows. Column order matches result["manifest"]["columns"].
        manifest = result.get("manifest", {}) or {}
        cols = [c.get("name") for c in (manifest.get("columns") or [])]
        rows = (result.get("result", {}) or {}).get("data_array") or []
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(zip(cols, row))
            out.append(item)
        return out
    except Exception as exc:
        logger.warning("recall_tools: VS similarity_search failed: %s", exc)
        return None


# ----------------------------------------------------------------------------
# Delta fallback path
# ----------------------------------------------------------------------------
def _load_creds() -> Optional[dict[str, str]]:
    """Pull host+token from env or ~/.databrickscfg. Returns None if unavailable.

    Used for the databricks-sql-connector fallback path (which still needs an
    explicit PAT). In Databricks Apps the runtime injects DATABRICKS_CLIENT_ID +
    DATABRICKS_CLIENT_SECRET (SP auth), which dbsql doesn't directly accept —
    that path is handled by the SDK-based variant in _delta_select_recent.
    """
    host = os.getenv("DATABRICKS_HOST") or os.getenv("DATABRICKS_SERVER_HOSTNAME")
    token = os.getenv("DATABRICKS_TOKEN")
    if host and token:
        return {"host": host.rstrip("/"), "token": token}
    profile = os.getenv("DATABRICKS_CONFIG_PROFILE")
    if not profile:
        return None
    try:
        import configparser
        from pathlib import Path
        cfg = configparser.ConfigParser()
        cfg.read(Path.home() / ".databrickscfg")
        if profile not in cfg:
            return None
        s = cfg[profile]
        return {"host": s.get("host", "").rstrip("/"), "token": s.get("token", "")}
    except Exception as exc:
        logger.warning("recall_tools: cfg load failed: %s", exc)
        return None


def _delta_select_recent(limit: int = 5) -> Optional[list[dict[str, Any]]]:
    """SELECT last N briefs by date from the Delta table — non-semantic fallback.

    Tries two auth paths so this works both locally (PAT) and in Databricks Apps
    (auto-injected service-principal OAuth):
      1. databricks-sdk StatementExecutionAPI — auto-discovers any auth flavor
         the SDK supports (SP OAuth, PAT, U2M, etc.). This is the production path.
      2. databricks-sql-connector with an explicit PAT — local-dev fallback.
    """
    # Path 1: SDK + Statement Execution API (works with SP OAuth in Apps).
    try:
        from databricks.sdk import WorkspaceClient
        from databricks.sdk.service.sql import StatementState
        w = WorkspaceClient()
        resp = w.statement_execution.execute_statement(
            statement=(
                f"SELECT brief_id, brief_date, severity_score, headline, "
                f"body_markdown, themes "
                f"FROM {SOURCE_TABLE_FQN} "
                f"ORDER BY brief_date DESC LIMIT {int(limit)}"
            ),
            warehouse_id=WAREHOUSE_ID,
            wait_timeout="30s",
        )
        if resp.status and resp.status.state == StatementState.SUCCEEDED:
            cols = [c.name for c in (resp.manifest.schema.columns or [])] if resp.manifest and resp.manifest.schema else []
            rows = (resp.result.data_array if resp.result else []) or []
            return [dict(zip(cols, r)) for r in rows]
        logger.warning("recall_tools: SDK SQL returned non-SUCCEEDED state: %s", resp.status)
    except Exception as exc:
        logger.warning("recall_tools: SDK Delta fallback failed: %s", exc)

    # Path 2: dbsql connector with explicit PAT (local dev).
    creds = _load_creds()
    if not creds or not creds.get("host") or not creds.get("token"):
        logger.info("recall_tools: no PAT creds for dbsql fallback either")
        return None
    try:
        from databricks import sql as dbsql
        server = creds["host"].replace("https://", "").replace("http://", "")
        with dbsql.connect(
            server_hostname=server,
            http_path=f"/sql/1.0/warehouses/{WAREHOUSE_ID}",
            access_token=creds["token"],
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT brief_id, brief_date, severity_score, headline, "
                    f"body_markdown, themes "
                    f"FROM {SOURCE_TABLE_FQN} "
                    f"ORDER BY brief_date DESC LIMIT {int(limit)}"
                )
                cols = [c[0] for c in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        logger.warning("recall_tools: dbsql Delta fallback failed: %s", exc)
        return None


# ----------------------------------------------------------------------------
# Formatting
# ----------------------------------------------------------------------------
def _format_hits(
    banner: str,
    source_note: str,
    hits: list[dict[str, Any]],
    days_back: int,
    include_score: bool,
) -> str:
    lines = [
        banner,
        f"source: {source_note}  fetched_at: {_now_iso()}",
        f"recall window: last {days_back} day(s)  hits: {len(hits)}",
    ]
    if not hits:
        lines.append("- (no past briefs matched)")
        return "\n".join(lines)

    for i, h in enumerate(hits, 1):
        date = h.get("brief_date") or "(no date)"
        sev = h.get("severity_score")
        headline = h.get("headline") or "(no headline)"
        themes = h.get("themes") or []
        if isinstance(themes, str):
            # Sometimes the VS layer returns arrays as JSON-ish strings
            themes_disp = themes
        else:
            themes_disp = ", ".join(themes) if themes else "(none)"

        body = (h.get("body_markdown") or "").strip().replace("\n", " ")
        # 1-line excerpt — first ~180 chars after stripping headers/whitespace
        excerpt = body[:180] + ("…" if len(body) > 180 else "")

        score_part = ""
        if include_score and h.get("score") is not None:
            try:
                score_part = f"  (similarity {float(h['score']):.3f})"
            except Exception:
                pass

        lines.append(f"{i}. **{date}**  sev={sev}  themes=[{themes_disp}]{score_part}")
        lines.append(f"   {headline}")
        lines.append(f"   _{excerpt}_")
    return "\n".join(lines)


def _filter_by_days_back(rows: list[dict[str, Any]], days_back: int) -> list[dict[str, Any]]:
    """Best-effort: drop rows older than `days_back` days from now. If the date
    can't be parsed, keep the row (don't accidentally hide a hit)."""
    if not rows:
        return rows
    try:
        from datetime import date, datetime as _dt, timedelta
        cutoff = (_dt.now(timezone.utc) - timedelta(days=days_back)).date()
        out = []
        for r in rows:
            d = r.get("brief_date")
            if isinstance(d, str):
                try:
                    d = _dt.fromisoformat(d).date()
                except Exception:
                    out.append(r)
                    continue
            if hasattr(d, "year"):
                if d >= cutoff:
                    out.append(r)
            else:
                out.append(r)
        return out
    except Exception:
        return rows


# ----------------------------------------------------------------------------
# Tool entrypoint
# ----------------------------------------------------------------------------
@tool
def recall_similar_briefs(query: str, days_back: int = 14) -> str:
    """Semantically recall past nightly ops briefs similar to `query`.

    Returns the top 3 most similar briefs from the briefs_archive Vector Search
    index over the last `days_back` days. Each hit shows the brief date,
    headline, severity, themes, a 1-line body excerpt, and (when available)
    the similarity score.

    Use this to detect recurring themes ("Memphis weather appeared in 3 of
    last 14 briefs"), find prior similar disruption playbooks, or surface
    past responses to one-off events. The agent should treat the dates and
    themes as authoritative recall, and use the excerpts as paraphrasable
    context rather than verbatim citations.

    Args:
        query: natural-language description of what to recall, e.g.
            "Memphis weather impact" or "FedEx hub disruption".
        days_back: maximum age in days for returned briefs (default 14).

    Returns:
        A single multi-line string. First line is a status banner:
        "🟢 LIVE" when Vector Search returned hits, "🟡 SYNTHETIC" when
        the tool fell through to a Delta-table SELECT or no-data path.
    """
    if not query or not query.strip():
        return f"{SYNTH_BANNER}\nsource: recall_similar_briefs\n- (empty query)"

    days_back = max(1, min(int(days_back or 14), 365))
    # Capture first failures so the user-visible synthetic banner can explain WHY
    # both paths failed (otherwise we lose the signal — app logs aren't visible
    # from PAT auth and the agent only sees the tool's stringified return).
    vs_error: Optional[str] = None
    delta_error: Optional[str] = None

    # 1) Try Vector Search
    try:
        vs_hits = _vs_similarity_search(query.strip(), num_results=3)
        if vs_hits is None:
            vs_error = "VS returned None (endpoint resolution or permission issue)"
    except Exception as exc:
        logger.warning("recall_similar_briefs: VS path raised unexpectedly: %s", exc)
        vs_hits = None
        vs_error = f"VS raised: {exc!s}"

    if vs_hits:
        filtered = _filter_by_days_back(vs_hits, days_back)
        # If VS returned hits but they're all older than window, still show them
        # under SYNTHETIC banner so the agent can reason about it
        if filtered:
            return _format_hits(
                LIVE_BANNER,
                f"Databricks Vector Search ({INDEX_NAME})",
                filtered,
                days_back,
                include_score=True,
            )
        # All hits older than window — degrade gracefully
        return _format_hits(
            SYNTH_BANNER,
            f"Vector Search returned hits older than {days_back}d window",
            vs_hits,
            days_back,
            include_score=True,
        )

    # 2) Delta fallback (non-semantic)
    try:
        rows = _delta_select_recent(limit=5)
        if rows is None:
            delta_error = "Delta SELECT returned None (no auth path succeeded)"
    except Exception as exc:
        logger.warning("recall_similar_briefs: Delta fallback raised: %s", exc)
        rows = None
        delta_error = f"Delta raised: {exc!s}"

    if rows:
        filtered = _filter_by_days_back(rows, days_back)
        return _format_hits(
            SYNTH_BANNER,
            f"Delta SELECT fallback (VS unavailable) — {SOURCE_TABLE_FQN}",
            filtered or rows,
            days_back,
            include_score=False,
        )

    # 3) Nothing worked — surface the actual failure modes so we can diagnose
    diag = "; ".join(filter(None, [vs_error, delta_error])) or "unknown cause"
    return (
        f"{SYNTH_BANNER}\n"
        f"source: recall_similar_briefs  fetched_at: {_now_iso()}\n"
        f"- (no recall available — both paths failed: {diag}; "
        "Historian should proceed without prior-brief context)"
    )


RECALL_TOOLS = [recall_similar_briefs]
