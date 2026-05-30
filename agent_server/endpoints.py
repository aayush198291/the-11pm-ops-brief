"""Custom FastAPI endpoints for the operational console UI.

  GET  /signals/latest    — current snapshot of all 9 external signal sources
  GET  /actions/queue     — pending action proposals (Composer write-backs)
  POST /actions/approve   — operator approves a pending action
  POST /actions/reject    — operator rejects a pending action
  GET  /decisions         — recent decision-log entries

These power the Signals rail, Action Queue, and audit-trail panes of the UI.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agent_server.action_tools import (
    approve_action,
    get_action_queue,
    get_decision_log,
    reject_action,
)
from agent_server.investigation_tools import lookup_object

logger = logging.getLogger(__name__)


# Signal snapshot is cached for 60s so concurrent UI tabs don't hammer the APIs.
_SIGNAL_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_SIGNAL_CACHE_TTL = 60.0  # seconds

# Last-known-good cache: per-source parsed signal dict from the most recent
# successful (status='live') fetch. Survives across snapshots so that when a
# transient upstream error happens (e.g. GDELT cold connection), we can show
# the previous good payload tagged 'stale' rather than a generic error.
_LAST_GOOD: dict[str, dict] = {}

# Default per-tool timeout for the parallel fan-out. Bumped from 25s → 45s
# because cold-connection latency for GDELT/OpenSky regularly straddles 25s.
_DEFAULT_TIMEOUT = 45.0

# Per-tool timeout overrides. GDELT is notoriously slow on first-hit; give it
# more rope before falling back to LKG.
_PER_TOOL_TIMEOUT: dict[str, float] = {
    "gdelt": 60.0,
}

# Dedicated thread pool sized for our 17-source fan-out. Default loop pool maxes
# out at min(32, cpu+4); on a 4-core Apps container that's only 8 workers, so
# half the tools queue and time out on the 25s budget. 20 workers = all tools
# start at t=0 and only network latency gates them.
import concurrent.futures as _cf
_SIGNAL_POOL = _cf.ThreadPoolExecutor(max_workers=20, thread_name_prefix="signal")


def _signal_now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M UTC")


async def _gather_signals() -> dict:
    """Fan out across the 17 signal tools concurrently. Each tool already has
    a synthetic fallback so we always return *something*."""
    from agent_server.external_tools import (
        fetch_weather_alerts,
        fetch_news_pulse,
        fetch_port_traffic,
        fetch_carrier_diversions,
    )
    from agent_server.external_tools_extra import (
        fetch_usgs_earthquakes,
        fetch_fema_disasters,
        fetch_nasa_eonet,
        fetch_reddit_supply_chain_pulse,
        fetch_google_news_supply_chain,
    )
    from agent_server.external_tools_v3 import (
        fetch_faa_tfr,
        fetch_cbp_border_waits,
        fetch_hackernews_supply_chain,
        fetch_eia_fuel_prices,
        fetch_usgs_volcano_alerts,
        fetch_nhc_hurricanes,
        fetch_fda_recalls,
        fetch_marine_traffic_choke_points,
        fetch_aviation_weather_pireps,
    )

    # Each invocation runs in a thread because the underlying tools are sync `requests`.
    sources: list[tuple[str, str, Any, dict]] = [
        ("nws", "NWS Alerts", fetch_weather_alerts, {"states": ["TN", "TX", "FL", "GA", "CA", "NY", "IL", "WA"]}),
        ("gdelt", "GDELT News", fetch_news_pulse, {}),
        ("portwatch", "IMF PortWatch", fetch_port_traffic, {"ports": ["Los Angeles", "Long Beach", "Savannah", "Houston", "New York"]}),
        # OpenSky removed — endpoint perpetually 4xx in production; PIREPs (free, no auth)
        # provides better signal for air-freight hub disruption anyway.
        ("usgs", "USGS Quakes", fetch_usgs_earthquakes, {}),
        ("fema", "FEMA Disasters", fetch_fema_disasters, {}),
        ("eonet", "NASA EONET", fetch_nasa_eonet, {}),
        ("reddit", "Reddit r/supplychain", fetch_reddit_supply_chain_pulse, {}),
        ("gnews", "Google News", fetch_google_news_supply_chain, {}),
        ("faa_tfr", "FAA TFRs", fetch_faa_tfr, {}),
        ("cbp", "CBP Border Waits", fetch_cbp_border_waits, {}),
        ("hn", "HackerNews", fetch_hackernews_supply_chain, {}),
        ("eia", "EIA Fuel Prices", fetch_eia_fuel_prices, {}),
        ("volcano", "USGS Volcanoes", fetch_usgs_volcano_alerts, {}),
        ("nhc", "NOAA Hurricanes", fetch_nhc_hurricanes, {}),
        ("fda_recalls", "FDA Recalls", fetch_fda_recalls, {}),
        ("chokepoints", "Maritime Chokepoints", fetch_marine_traffic_choke_points, {}),
        ("pireps", "Aviation PIREPs", fetch_aviation_weather_pireps, {}),
    ]

    async def _one(label_id: str, label: str, fn, kwargs):
        loop = asyncio.get_event_loop()
        timeout = _PER_TOOL_TIMEOUT.get(label_id, _DEFAULT_TIMEOUT)
        try:
            text = await asyncio.wait_for(
                loop.run_in_executor(_SIGNAL_POOL, lambda: fn.invoke(kwargs)),
                timeout=timeout,
            )
        except Exception as exc:
            logger.warning("Signal %s failed (timeout=%.0fs): %s", label_id, timeout, exc)
            text = f"🟡 SYNTHETIC (fetch error: {exc!s})\nno data this cycle"

        parsed = _parse_signal_text(label_id, label, text)
        # Stash the raw tool output so /signals/<id>/raw can serve a drill-in
        # view (sig-tile click → side panel). Cap to ~16KB so a pathologically
        # large response doesn't bloat memory.
        parsed["raw"] = (text or "")[:16000]

        # If this cycle was a live success, update LKG. If this cycle errored
        # AND we have a previously good payload, swap in the LKG entry tagged
        # as 'stale' so the rail shows useful content instead of an error.
        if parsed.get("status") == "live":
            _LAST_GOOD[label_id] = {**parsed, "lkg_ts": _signal_now()}
        elif parsed.get("status") == "error" and label_id in _LAST_GOOD:
            lkg = _LAST_GOOD[label_id]
            stale_detail = lkg.get("detail") or "(prior data)"
            lkg_ts = lkg.get("lkg_ts") or lkg.get("ts") or "?"
            parsed = {
                **lkg,
                "status": "stale",
                "ts": _signal_now(),
                "detail": f"{stale_detail} (LKG from {lkg_ts})"[:200],
            }
        return parsed

    results = await asyncio.gather(*(_one(*s) for s in sources), return_exceptions=False)
    return {"sources": list(results), "ts": _signal_now()}


_LIVE_TAG = "🟢 LIVE"
_SYNTH_TAG = "🟡 SYNTHETIC"


def _strip_raw(source: dict) -> dict:
    """Return a copy of a source dict without the (potentially huge) raw text.
    Used for the /signals/latest rail snapshot — raw is served per-source on
    demand by /signals/{id}/raw instead."""
    if not isinstance(source, dict):
        return source
    out = dict(source)
    out.pop("raw", None)
    return out

# Map JSON array-fields → (canonical noun, item-formatter)
_JSON_LIST_FIELDS: list[tuple[str, str]] = [
    ("alerts", "alerts"),
    ("articles", "stories"),
    ("declarations", "declarations"),
    ("events", "events"),
    ("posts", "posts"),
    ("storms", "storms"),
    ("hubs", "hubs"),
    ("ports", "ports"),
    ("crossings", "crossings"),
    ("tfrs", "TFRs"),
    ("advisories", "advisories"),
    ("eonet", "events"),
    ("notifications", "notifications"),
    ("anomalies", "anomalies"),
]


def _parse_signal_text(source_id: str, label: str, text: str) -> dict:
    """Extract count + status + 1-line detail from a signal tool's output.

    Tools come in two formats:
      A. Plaintext with 🟢 LIVE / 🟡 SYNTHETIC header (external_tools_extra, _v3)
      B. Raw JSON (external_tools.py — NWS, GDELT, PortWatch, OpenSky)

    Status is canonical: 'live' | 'synthetic' | 'error' | 'stale'.
    Detail is normalized so the UI shows one consistent phrase per state.
    """
    body = (text or "").strip()
    if not body:
        return {
            "id": source_id, "label": label, "count": 0, "status": "stale",
            "ts": _signal_now(), "detail": "(no output)",
        }

    # Path B: raw JSON output (NWS/GDELT/PortWatch/OpenSky)
    if body[0] == "{":
        return _parse_json_signal(source_id, label, body)

    # Path A: plaintext header format
    return _parse_text_signal(source_id, label, body)


def _parse_json_signal(source_id: str, label: str, body: str) -> dict:
    """Parse the JSON-format tool output from external_tools.py."""
    import json as _json
    try:
        payload = _json.loads(body)
    except Exception as exc:
        return {
            "id": source_id, "label": label, "count": 0, "status": "error",
            "ts": _signal_now(), "detail": f"(parse error: {exc!s})",
        }

    src = (payload.get("source") or "").strip()
    has_source = bool(src and payload.get("fetched_at"))
    is_synthetic = "synthetic" in src.lower() or payload.get("synthetic") is True
    status = "synthetic" if is_synthetic else ("live" if has_source else "stale")

    # Find the first non-empty list-like field
    count = 0
    detail = ""
    for key, noun in _JSON_LIST_FIELDS:
        items = payload.get(key)
        if isinstance(items, list):
            count = len(items)
            if items:
                first = items[0]
                detail = _summarize_json_item(first, source_id) or f"{count} {noun}"
            break

    # Tool-specific extras for richer detail lines
    if source_id == "portwatch" and isinstance(payload.get("ports"), list):
        ps = payload["ports"]
        # count = total vessels across ports for at-a-glance load signal
        try:
            tot = sum(int(p.get("vessel_count_total") or 0) for p in ps)
            if tot:
                top = max(ps, key=lambda p: int(p.get("vessel_count_total") or 0))
                detail = f"{len(ps)} ports · top: {top.get('port')} {top.get('vessel_count_total')} vessels"
                count = len(ps)
        except Exception:
            pass
    elif source_id == "opensky" and isinstance(payload.get("hubs"), list):
        hubs = payload["hubs"]
        try:
            tot = sum(int(h.get("aircraft_total") or 0) for h in hubs)
            if tot:
                top = max(hubs, key=lambda h: int(h.get("aircraft_total") or 0))
                detail = f"{len(hubs)} hubs · busiest: {top.get('hub','?')} ({tot} aircraft)"
                count = tot
        except Exception:
            pass

    if not detail:
        if status == "synthetic":
            detail = "(no data this cycle — synthetic fallback)"
        elif count == 0:
            detail = "(quiet — nothing flagged)"
        else:
            detail = f"{count} items"

    return {
        "id": source_id, "label": label, "count": count, "status": status,
        "ts": _signal_now(), "detail": detail[:140],
    }


def _summarize_json_item(item: Any, source_id: str) -> str:
    """Best-effort 1-line summary of the first item in a JSON list."""
    if not isinstance(item, dict):
        return str(item)[:80]
    # Try common shapes
    if "headline" in item:
        return str(item["headline"])
    if "title" in item:
        return str(item["title"])
    if "event" in item and "state" in item:
        return f"{item.get('state')} — {item.get('event')}"
    if "place" in item and "mag" in item:
        return f"M{item.get('mag')} {item.get('place')}"
    if "incidentType" in item:
        return f"{item.get('state','')} {item.get('incidentType','')}: {item.get('declarationTitle','')}"
    if "name" in item and "category" in item:
        return f"{item.get('name')} ({item.get('category')})"
    if "port" in item:
        return f"{item.get('port')} · {item.get('vessel_count_total','?')} vessels"
    if "hub" in item:
        return f"{item.get('hub')} · {item.get('aircraft_total','?')} aircraft"
    # Generic — first short string value
    for v in item.values():
        if isinstance(v, str) and 5 < len(v) < 120:
            return v
    return ""


def _parse_text_signal(source_id: str, label: str, body: str) -> dict:
    """Parse the plaintext '🟢 LIVE\\n...' format from external_tools_extra / _v3."""
    lines = body.splitlines()
    first = lines[0] if lines else ""

    if "fetch error" in body.lower()[:200] or "(fetch error" in body.lower()[:200]:
        status = "error"
    elif _LIVE_TAG in first:
        status = "live"
    elif _SYNTH_TAG in first:
        status = "synthetic"
    else:
        status = "stale"

    import re
    nouns = (
        r"alerts?|stories|headlines|events?|posts?|quakes?|declarations?|"
        r"articles?|ships?|vessels?|tfrs?|crossings|advisories|storms?|"
        r"choke ?points?|notifications?|anomalies|hubs?|ports?|aircraft|rows?|"
        r"recalls?|enforcement"  # openFDA enforcement reports / recall actions
    )
    # Two patterns: (A) "14 alerts" and (B) "active TFRs: 133" / "...: N event(s)"
    # NOTE: pat_b's char window was 40 — too tight. NASA EONET's line
    # "open events (past 3d) in wildfires/severeStorms/volcanoes: 1" needs ~60
    # chars between the noun and the colon. Bumped to 100 to cover similar
    # multi-qualifier headers across all sources.
    pat_a = re.compile(r"\b(\d{1,5})\s+\b(?:" + nouns + r")\b", re.IGNORECASE)
    pat_b = re.compile(r"\b(?:" + nouns + r")\b[^:\n]{0,100}:\s*(\d{1,5})\b", re.IGNORECASE)
    pat_c = re.compile(r":\s*(\d{1,5})\s+\b(?:" + nouns + r")\b", re.IGNORECASE)
    count = None
    detail = ""
    for line in lines[:8]:
        # Skip the provenance header — it has timestamps that look like big numbers.
        if line.lower().startswith("source:") or line.startswith(("🟢", "🟡")):
            continue
        if count is None:
            for p in (pat_a, pat_b, pat_c):
                m = p.search(line)
                if m:
                    count = int(m.group(1))
                    break
        if (not detail and line and line != first
                and not line.startswith(("---", "#", "==="))):
            detail = line.strip().lstrip("-•* ")[:140]

    count = count if count is not None else 0

    if not detail:
        if status == "error":
            detail = "(source unreachable this cycle)"
        elif status == "synthetic":
            detail = "(no data this cycle — synthetic fallback)"
        elif count == 0:
            detail = "(quiet — nothing flagged)"
        else:
            detail = "(see source)"

    return {
        "id": source_id, "label": label, "count": count, "status": status,
        "ts": _signal_now(), "detail": detail,
    }


# ────────────────────────────────────────────────────────────────────────────
# Endpoint installer
# ────────────────────────────────────────────────────────────────────────────


class ApproveRequest(BaseModel):
    action_id: str
    approver: Optional[str] = ""


class RejectRequest(BaseModel):
    action_id: str
    approver: Optional[str] = ""
    reason: Optional[str] = ""


async def _warm_start_signals(delay: float = 5.0) -> None:
    """One-shot background task: wait `delay` seconds after server startup,
    then run _gather_signals() to populate the LKG cache before any user hits
    the rail. Errors are swallowed — this is best-effort priming only."""
    try:
        await asyncio.sleep(delay)
        logger.info("Signal warm-start: priming LKG cache via _gather_signals()…")
        data = await _gather_signals()
        # Seed the snapshot cache too so the first user request is instant.
        _SIGNAL_CACHE["ts"] = time.time()
        _SIGNAL_CACHE["data"] = data
        live = sum(1 for s in data.get("sources", []) if s.get("status") == "live")
        logger.info(
            "Signal warm-start complete: %d/%d sources live, LKG seeded for %d sources.",
            live, len(data.get("sources", [])), len(_LAST_GOOD),
        )
    except Exception as exc:
        logger.warning("Signal warm-start failed (non-fatal): %s", exc)


def install_endpoints(app: FastAPI) -> None:
    # NOTE: warm-start of the signal LKG cache is scheduled from start_server.py's
    # custom `_lifespan` (which wraps the original lifespan). We don't register
    # an `on_event("startup")` here to avoid double-priming.

    @app.get("/signals/latest", include_in_schema=False)
    async def signals_latest():
        now = time.time()
        cached = _SIGNAL_CACHE.get("data")
        cached_ts = _SIGNAL_CACHE.get("ts", 0.0)
        if cached and (now - cached_ts) < _SIGNAL_CACHE_TTL:
            # Strip raw text from the rail-snapshot payload — it's served on
            # demand via /signals/{id}/raw and would otherwise bloat every poll.
            return JSONResponse({**cached, "sources": [_strip_raw(s) for s in cached.get("sources", [])], "cached": True})
        data = await _gather_signals()
        _SIGNAL_CACHE["ts"] = now
        _SIGNAL_CACHE["data"] = data
        return JSONResponse({**data, "sources": [_strip_raw(s) for s in data.get("sources", [])], "cached": False})

    @app.get("/signals/{source_id}/raw", include_in_schema=False)
    async def signals_raw(source_id: str):
        """Return the raw tool output for a single source — fed to the
        sig-tile click-to-drill side panel so operators can see the actual
        alert content, not just the parsed count + 1-line summary."""
        cached = _SIGNAL_CACHE.get("data") or {}
        for s in cached.get("sources", []):
            if s.get("id") == source_id:
                return JSONResponse({
                    "id": source_id,
                    "label": s.get("label"),
                    "status": s.get("status"),
                    "ts": s.get("ts"),
                    "count": s.get("count"),
                    "detail": s.get("detail"),
                    "raw": s.get("raw") or "(no raw text cached for this source)",
                })
        # Fall back to LKG if the live cache doesn't include it
        lkg = _LAST_GOOD.get(source_id)
        if lkg:
            return JSONResponse({
                "id": source_id,
                "label": lkg.get("label"),
                "status": "stale",
                "ts": lkg.get("lkg_ts") or lkg.get("ts"),
                "count": lkg.get("count"),
                "detail": lkg.get("detail"),
                "raw": lkg.get("raw") or "(no raw text in LKG)",
            })
        raise HTTPException(status_code=404, detail=f"signal '{source_id}' not in cache")

    @app.get("/actions/queue", include_in_schema=False)
    async def actions_queue(limit: int = 20):
        return JSONResponse({"actions": get_action_queue(limit=limit)})

    @app.post("/actions/approve", include_in_schema=False)
    async def actions_approve(req: ApproveRequest):
        result = approve_action(req.action_id, approver=req.approver or "")
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        return JSONResponse(result)

    @app.post("/actions/reject", include_in_schema=False)
    async def actions_reject(req: RejectRequest):
        result = reject_action(req.action_id, approver=req.approver or "", reason=req.reason or "")
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        return JSONResponse(result)

    @app.get("/decisions", include_in_schema=False)
    async def decisions(limit: int = 20):
        return JSONResponse({"decisions": get_decision_log(limit=limit)})

    @app.get("/objects/lookup", include_in_schema=False)
    async def object_lookup(id: str):
        """Drill-down on a brief object chip (FC-LAX, CARRIER-FEDEX, etc.).

        Runs SELECT queries against the warehouse to assemble a Foundry-style
        object card with properties + links to other entities.
        """
        if not id:
            raise HTTPException(status_code=400, detail="id query param required")
        # SQL execution is sync; run in default executor so we don't block the loop.
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lookup_object, id)
        return JSONResponse(result)
