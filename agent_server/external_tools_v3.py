"""Eight more external-signal data tools for The 11 PM Ops Brief agent.

Each tool returns a single string and never raises. First line is a banner:
  - "🟢 LIVE"                          — fresh data from upstream API
  - "🟡 SYNTHETIC (<reason>)"          — fallback because upstream failed

Sources added in this module:

  1. FAA TFR (Temporary Flight Restrictions) feed
  2. CBP border crossing wait times (US-Canada + US-Mexico, commercial truck lanes)
  3. HackerNews supply-chain story pulse (Algolia API)
  4. EIA weekly retail diesel + gasoline price series
  5. USGS Volcano Hazards Program elevated notifications
  6. NOAA / National Hurricane Center active named storms
  7. openFDA enforcement reports (food + drug recalls, last 30d)
  8. IMF PortWatch choke-point daily vessel flow (Suez, Panama, Hormuz, Malacca, Dover)

All tools are decorated with @tool so they can be bound to LangGraph subagents.
"""
from __future__ import annotations

import json
import logging
import os
import re
import statistics
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

USER_AGENT = "OpsBriefAgent/1.0 (apatel@shipbob.com)"
DEFAULT_TIMEOUT = 20  # seconds

LIVE_BANNER = "🟢 LIVE"


def _synth_banner(reason: str) -> str:
    return f"🟡 SYNTHETIC ({reason})"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hours_ago_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def _days_ago_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 1. FAA TFR — Temporary Flight Restrictions
# ---------------------------------------------------------------------------
_FREIGHT_HUBS_AIRPORTS = {
    # ICAO / airport name fragments associated with freight hubs.
    "MEM": "Memphis (FedEx Superhub)",
    "SDF": "Louisville (UPS Worldport)",
    "IND": "Indianapolis (FedEx Express)",
    "ANC": "Anchorage (Asia-NA freight gateway)",
    "LAX": "Los Angeles International",
    "ORD": "Chicago O'Hare",
    "JFK": "New York JFK",
    "EWR": "Newark",
    "ATL": "Atlanta Hartsfield",
    "DFW": "Dallas-Fort Worth",
    "MIA": "Miami International",
    "OAK": "Oakland (FedEx West)",
    "PHX": "Phoenix Sky Harbor",
}


def _impact_for_tfr(text: str) -> str | None:
    """If a TFR mentions a freight-hub airport, return a relevance note."""
    upper = (text or "").upper()
    for code, label in _FREIGHT_HUBS_AIRPORTS.items():
        # Match ICAO three-letter code as whole word (avoid matching substrings).
        if re.search(rf"\b{code}\b", upper):
            return f"freight-hub impact: {label}"
    return None


@tool
def fetch_faa_tfr() -> str:
    """Fetch FAA Temporary Flight Restrictions (TFRs) and surface ones close to
    major freight hubs (MEM, SDF, IND, ANC, LAX, ORD, JFK, EWR, ATL, DFW, etc.).

    Uses the FAA's JSON list endpoint (`tfrapi/exportTfrList`) consumed by the
    public TFR site (the legacy XML / HTML list paths return 404 since the
    site became a SPA). Returns total active TFR count + the top 3 most
    impactful (with date, location, reason). Falls back to plausible synthetic
    TFRs on network error.
    """
    url = "https://tfr.faa.gov/tfrapi/exportTfrList"
    items: list[dict[str, Any]] = []
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                         timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        rows = r.json()
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                items.append({
                    "notam": row.get("notam_id") or row.get("notam") or "?",
                    "date": row.get("creation_date") or row.get("effective_date") or "",
                    "type": row.get("type") or "",
                    "description": row.get("description") or "",
                    "facility": row.get("facility") or "",
                    "state": row.get("state") or "",
                })
    except Exception as e:
        logger.warning("FAA TFR fetch failed: %s — using synthetic", e)
        items = []

    if items:
        # Rank: freight-hub-relevant TFRs first, then by date desc.
        for it in items:
            blob = " ".join(str(it.get(k, "")) for k in ("description", "facility", "state"))
            it["_impact"] = _impact_for_tfr(blob)
        items.sort(key=lambda it: (0 if it["_impact"] else 1, it.get("date", "")), reverse=False)

        lines = [LIVE_BANNER, f"source: FAA TFR  fetched_at: {_now_iso()}"]
        lines.append(f"active TFRs: {len(items)}")
        for it in items[:3]:
            impact = f"  [{it['_impact']}]" if it.get("_impact") else ""
            desc = (it.get("description") or "")[:80]
            lines.append(
                f"- NOTAM {it.get('notam')}  {it.get('date','')}  "
                f"{it.get('type','')}  \"{desc}\"  ({it.get('facility','')}, "
                f"{it.get('state','')}){impact}"
            )
        return "\n".join(lines)

    # Synthetic fallback
    synth = [
        {
            "notam": "4/2871",
            "date": datetime.now(timezone.utc).strftime("%m/%d/%Y"),
            "type": "VIP MOVEMENT",
            "description": "Presidential VIP TFR — surface to 18,000 ft within 30nm",
            "facility": "ORD",
            "state": "IL",
            "_impact": _impact_for_tfr("ORD"),
        },
        {
            "notam": "4/3145",
            "date": datetime.now(timezone.utc).strftime("%m/%d/%Y"),
            "type": "HAZARDS",
            "description": "Wildfire firefighting operations — TFR active surface to 12,500 ft",
            "facility": "RDD",
            "state": "CA",
            "_impact": None,
        },
    ]
    lines = [_synth_banner("FAA TFR feed unavailable"), f"source: FAA TFR (synthetic)  fetched_at: {_now_iso()}"]
    lines.append(f"active TFRs: {len(synth)}")
    for it in synth:
        impact = f"  [{it['_impact']}]" if it.get("_impact") else ""
        lines.append(
            f"- NOTAM {it['notam']}  {it['date']}  {it['type']}  "
            f"\"{it['description']}\"  ({it['facility']}, {it['state']}){impact}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 2. CBP Border Crossing Wait Times
# ---------------------------------------------------------------------------
@tool
def fetch_cbp_border_waits() -> str:
    """Fetch CBP border crossing wait times for commercial truck lanes at the
    US-Canada and US-Mexico borders. Returns top 5 longest commercial waits.

    Public endpoint: https://bwt.cbp.gov/api/waittimes (JSON of all crossings).
    Filters to lanes with `commercial_vehicle_lanes` data; falls back to
    plausible synthetic crossings if upstream is unreachable.
    """
    url = "https://bwt.cbp.gov/api/waittimes"
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning("CBP wait-times fetch failed: %s — using synthetic", e)
        data = None

    if isinstance(data, list) and data:
        crossings: list[dict[str, Any]] = []
        for row in data:
            if not isinstance(row, dict):
                continue
            border = (row.get("border") or "").strip()  # "Canadian Border" / "Mexican Border"
            port = (row.get("port_name") or row.get("crossing_name") or "").strip()
            state = (row.get("port_status") or row.get("state") or "").strip()

            # CBP nests "commercial_vehicle_lanes" → { "standard_lanes": { "delay_minutes": "30", ... } }
            cv = row.get("commercial_vehicle_lanes") or {}
            std = cv.get("standard_lanes") if isinstance(cv, dict) else None
            if not isinstance(std, dict):
                continue
            try:
                delay = int(std.get("delay_minutes") or 0)
            except (TypeError, ValueError):
                delay = 0
            operational = (std.get("operational_status") or "").strip()
            if operational and operational.lower() not in ("open", "lanes open"):
                # Closed lane is itself notable — keep it but mark delay as 9999 for sort.
                effective = max(delay, 9999) if "closed" in operational.lower() else delay
            else:
                effective = delay

            crossings.append({
                "border": border,
                "port": port,
                "state": state,
                "delay_min": delay,
                "operational_status": operational,
                "_rank": effective,
            })

        crossings.sort(key=lambda c: c.get("_rank", 0), reverse=True)
        top = [c for c in crossings if c.get("_rank", 0) > 0][:5]

        lines = [LIVE_BANNER, f"source: CBP bwt.cbp.gov  fetched_at: {_now_iso()}"]
        lines.append(f"commercial truck-lane crossings reporting: {len(crossings)}")
        if not top:
            lines.append("- (all commercial lanes showing zero delay)")
        for c in top:
            lines.append(
                f"- {c['border']:>16}  {c['port']:>28}  "
                f"delay: {c['delay_min']} min  status: {c['operational_status'] or 'Open'}"
            )
        return "\n".join(lines)

    # Synthetic fallback
    synth = [
        {"border": "Mexican Border", "port": "Laredo - World Trade Bridge", "state": "TX",
         "delay_min": 95, "operational_status": "Open"},
        {"border": "Mexican Border", "port": "Otay Mesa", "state": "CA",
         "delay_min": 70, "operational_status": "Open"},
        {"border": "Canadian Border", "port": "Detroit - Ambassador Bridge", "state": "MI",
         "delay_min": 45, "operational_status": "Open"},
        {"border": "Canadian Border", "port": "Buffalo - Peace Bridge", "state": "NY",
         "delay_min": 35, "operational_status": "Open"},
        {"border": "Mexican Border", "port": "El Paso - Bridge of the Americas", "state": "TX",
         "delay_min": 30, "operational_status": "Open"},
    ]
    lines = [_synth_banner("CBP api unreachable"), f"source: CBP (synthetic)  fetched_at: {_now_iso()}"]
    lines.append(f"commercial truck-lane crossings reporting: {len(synth)} (synthetic)")
    for c in synth:
        lines.append(
            f"- {c['border']:>16}  {c['port']:>28}  "
            f"delay: {c['delay_min']} min  status: {c['operational_status']}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3. HackerNews supply-chain pulse (Algolia API)
# ---------------------------------------------------------------------------
@tool
def fetch_hackernews_supply_chain() -> str:
    """Search HackerNews via Algolia API for recent stories mentioning supply
    chain / freight / logistics over the past 72 hours.

    Endpoint: https://hn.algolia.com/api/v1/search (free, no auth).
    Returns top 8 stories with title, points score, comments, and URL.
    Falls back to synthetic HN-style headlines if Algolia is unreachable.
    """
    cutoff_unix = int((datetime.now(timezone.utc) - timedelta(days=3)).timestamp())
    # Algolia's `OR` syntax is unreliable when combined with `tags=story` filters,
    # so we issue three separate phrase queries and merge. `search_by_date` is
    # used (not `/search`) because the relevance-ranked endpoint silently
    # ignores `numericFilters` for these query shapes.
    url = "https://hn.algolia.com/api/v1/search_by_date"
    queries = ["\"supply chain\"", "freight", "logistics"]
    hits: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    error_count = 0
    for q in queries:
        params = {
            "query": q,
            "tags": "story",
            "numericFilters": f"created_at_i>{cutoff_unix}",
            "hitsPerPage": 20,
        }
        try:
            r = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=DEFAULT_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            for h in (data.get("hits") or []):
                oid = h.get("objectID")
                if not oid or oid in seen_ids:
                    continue
                seen_ids.add(oid)
                hits.append(h)
        except Exception as e:
            logger.warning("HackerNews Algolia fetch failed for q=%r: %s", q, e)
            error_count += 1
    if error_count >= len(queries):
        hits = []

    if hits:
        # Rank by points (HN score)
        hits.sort(key=lambda h: int(h.get("points") or 0), reverse=True)
        lines = [LIVE_BANNER, f"source: HackerNews / Algolia  fetched_at: {_now_iso()}"]
        lines.append(f"stories (past 72h) matching 'supply chain OR freight OR logistics': {len(hits)}")
        for h in hits[:8]:
            title = (h.get("title") or "")[:100]
            score = int(h.get("points") or 0)
            comments = int(h.get("num_comments") or 0)
            url_ = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
            created = (h.get("created_at") or "")[:19]
            lines.append(f"- [{score:>4} pts, {comments:>3} cmts]  {created}  \"{title}\"  → {url_}")
        return "\n".join(lines)

    synth = [
        {"title": "Show HN: I built an open-source TMS for small fleets",
         "points": 287, "num_comments": 94, "url": "https://news.ycombinator.com/item?id=99999991",
         "created_at": _hours_ago_iso(8)},
        {"title": "Why containerized shipping is on the verge of another shock",
         "points": 192, "num_comments": 142, "url": "https://news.ycombinator.com/item?id=99999992",
         "created_at": _hours_ago_iso(18)},
        {"title": "The hidden carbon cost of last-mile logistics",
         "points": 156, "num_comments": 67, "url": "https://news.ycombinator.com/item?id=99999993",
         "created_at": _hours_ago_iso(36)},
        {"title": "Ask HN: How are you handling tariffs in your supply chain stack?",
         "points": 124, "num_comments": 88, "url": "https://news.ycombinator.com/item?id=99999994",
         "created_at": _hours_ago_iso(48)},
        {"title": "Maersk Q3: Red Sea reroutes lifted spot rates 38% — what comes next",
         "points": 98, "num_comments": 41, "url": "https://news.ycombinator.com/item?id=99999995",
         "created_at": _hours_ago_iso(64)},
    ]
    lines = [_synth_banner("HN Algolia unavailable"), f"source: HackerNews (synthetic)  fetched_at: {_now_iso()}"]
    lines.append(f"stories (past 72h) matching 'supply chain OR freight OR logistics': {len(synth)} (synthetic)")
    for h in synth:
        lines.append(
            f"- [{h['points']:>4} pts, {h['num_comments']:>3} cmts]  "
            f"{h['created_at'][:19]}  \"{h['title']}\"  → {h['url']}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 4. EIA — Weekly retail gasoline + diesel prices
# ---------------------------------------------------------------------------
@tool
def fetch_eia_fuel_prices() -> str:
    """Fetch EIA weekly US retail gasoline + on-highway diesel prices (4 most
    recent weeks). Reports current $/gal, week-over-week delta, and direction.

    API: https://api.eia.gov/v2/petroleum/pri/gnd/data/

    Uses the EIA_API_KEY env var (registered key at eia.gov/opendata, ~5000
    req/hour quota). Falls back to DEMO_KEY (~30/hour, shared internet-wide)
    only if env var is unset, then to synthetic prices on quota-exhaust /
    network failure.

    Series of interest:
      - EPMR  = US Regular All Formulations Retail Gasoline Prices
      - EPD2D = US No 2 Diesel On-Highway Retail Prices
    """
    url = "https://api.eia.gov/v2/petroleum/pri/gnd/data/"
    api_key = (os.getenv("EIA_API_KEY") or "").strip() or "DEMO_KEY"
    params = {
        "api_key": api_key,
        "frequency": "weekly",
        "data[0]": "value",
        "facets[product][]": ["EPD2D", "EPMR"],
        "facets[duoarea][]": "NUS",  # National US
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "offset": "0",
        "length": "12",
    }
    try:
        r = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        payload = r.json()
        rows = ((payload.get("response") or {}).get("data")) or []
    except Exception as e:
        logger.warning(
            "EIA fetch failed (key=%s): %s — using synthetic",
            "registered" if api_key != "DEMO_KEY" else "DEMO_KEY",
            e,
        )
        rows = []

    if rows:
        # Bucket by product. EIA returns each product/period combination as a row.
        diesel: list[tuple[str, float]] = []
        gasoline: list[tuple[str, float]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            prod = row.get("product") or row.get("product-name") or ""
            period = (row.get("period") or "")[:10]
            try:
                val = float(row.get("value"))
            except (TypeError, ValueError):
                continue
            if "EPD2D" in str(prod) or "Diesel" in str(row.get("product-name") or ""):
                diesel.append((period, val))
            elif "EPMR" in str(prod) or "Regular" in str(row.get("product-name") or ""):
                gasoline.append((period, val))

        diesel.sort(reverse=True)
        gasoline.sort(reverse=True)

        def _format_series(label: str, series: list[tuple[str, float]]) -> list[str]:
            if not series:
                return [f"- {label}: (no data)"]
            this_period, this_val = series[0]
            wow = ""
            if len(series) > 1:
                prev_val = series[1][1]
                delta = this_val - prev_val
                arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
                wow = f"  (w/w {arrow} {delta:+.3f})"
            recent_str = ", ".join(f"{p}: ${v:.3f}" for p, v in series[:4])
            return [
                f"- {label}: ${this_val:.3f}/gal as of {this_period}{wow}",
                f"    recent 4 wks: {recent_str}",
            ]

        lines = [LIVE_BANNER, f"source: EIA api.eia.gov  fetched_at: {_now_iso()}"]
        lines.append(f"US weekly retail fuel prices ({len(rows)} rows across 2 series)")
        lines.extend(_format_series("Diesel (on-highway, US avg)", diesel))
        lines.extend(_format_series("Regular Gasoline (US avg)", gasoline))
        return "\n".join(lines)

    today = datetime.now(timezone.utc).date()

    def _wk(n: int) -> str:
        return (today - timedelta(weeks=n)).isoformat()

    # Synthetic anchored to recent EIA live values (May 2026 weekly averages
    # — diesel ~$5.50, regular gasoline ~$4.48). Re-anchor when the live API
    # is consistently in a different regime so synthetic failback doesn't
    # look obviously fake against current pump prices.
    diesel_synth = [
        (_wk(0), 5.523), (_wk(1), 5.596), (_wk(2), 5.541), (_wk(3), 5.498),
    ]
    gasoline_synth = [
        (_wk(0), 4.475), (_wk(1), 4.490), (_wk(2), 4.502), (_wk(3), 4.488),
    ]

    def _fmt(label: str, s: list[tuple[str, float]]) -> list[str]:
        this_p, this_v = s[0]
        delta = this_v - s[1][1]
        arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
        return [
            f"- {label}: ${this_v:.3f}/gal as of {this_p}  (w/w {arrow} {delta:+.3f})",
            f"    recent 4 wks: " + ", ".join(f"{p}: ${v:.3f}" for p, v in s),
        ]

    lines = [_synth_banner("EIA api unavailable"), f"source: EIA (synthetic)  fetched_at: {_now_iso()}"]
    lines.append("US weekly retail fuel prices (synthetic)")
    lines.extend(_fmt("Diesel (on-highway, US avg)", diesel_synth))
    lines.extend(_fmt("Regular Gasoline (US avg)", gasoline_synth))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5. USGS Volcano Hazards — elevated notifications
# ---------------------------------------------------------------------------
_ALERT_COLOR_RANK = {"GREEN": 0, "YELLOW": 1, "ORANGE": 2, "RED": 3}


@tool
def fetch_usgs_volcano_alerts() -> str:
    """Fetch USGS Volcano Hazards Program elevated notifications (Volcano Alert
    Level + Aviation Color Code) for all US volcanoes currently above normal.

    Endpoint: https://volcanoes.usgs.gov/hans-public/api/volcano/getElevatedVolcanoes
    (no auth). Returns count and top 3 most-elevated. Falls back to synthetic
    plausible alerts (Mauna Loa / Kīlauea / Mt. St. Helens) if unreachable.

    The legacy `getElevatedNotifications` path 302's to a broken endpoint —
    `getElevatedVolcanoes` is the current public-facing JSON API consumed by
    the USGS VHP map widget.
    """
    url = "https://volcanoes.usgs.gov/hans-public/api/volcano/getElevatedVolcanoes"
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning("USGS volcano fetch failed: %s — using synthetic", e)
        data = None

    rows: list[dict[str, Any]] = []
    if isinstance(data, list):
        rows = [r for r in data if isinstance(r, dict)]
    elif isinstance(data, dict):
        # Some endpoints wrap in { "data": [...] } or { "Notifications": [...] }
        for key in ("data", "Notifications", "notifications", "results"):
            v = data.get(key)
            if isinstance(v, list):
                rows = [r for r in v if isinstance(r, dict)]
                break

    if rows:
        def _rank(v: dict[str, Any]) -> int:
            color = str(
                v.get("color_code") or v.get("aviation_color_code")
                or v.get("ColorCode") or v.get("colorCode") or ""
            ).upper()
            return _ALERT_COLOR_RANK.get(color, -1)

        rows.sort(key=_rank, reverse=True)
        lines = [LIVE_BANNER, f"source: USGS volcanoes.usgs.gov  fetched_at: {_now_iso()}"]
        lines.append(f"elevated volcano notifications (US): {len(rows)}")
        for v in rows[:3]:
            name = v.get("volcano_name") or v.get("name") or v.get("Volcano") or "Unknown"
            state = (
                v.get("state") or v.get("subregion") or v.get("region")
                or v.get("obs_fullname") or v.get("obs_abbr") or ""
            )
            level = v.get("alert_level") or v.get("AlertLevel") or v.get("alertLevel") or "?"
            color = (
                v.get("color_code") or v.get("aviation_color_code")
                or v.get("ColorCode") or v.get("colorCode") or "?"
            )
            lines.append(f"- {name} ({state})  Alert: {level}  Color: {color}")
        return "\n".join(lines)

    synth = [
        {"name": "Kīlauea", "state": "HI", "alert_level": "WATCH", "color_code": "ORANGE"},
        {"name": "Mount Spurr", "state": "AK", "alert_level": "ADVISORY", "color_code": "YELLOW"},
    ]
    lines = [_synth_banner("USGS HANS api unavailable"), f"source: USGS Volcano (synthetic)  fetched_at: {_now_iso()}"]
    lines.append(f"elevated volcano notifications (US): {len(synth)} (synthetic)")
    for v in synth:
        lines.append(f"- {v['name']} ({v['state']})  Alert: {v['alert_level']}  Color: {v['color_code']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 6. NOAA / National Hurricane Center — active named storms
# ---------------------------------------------------------------------------
@tool
def fetch_nhc_hurricanes() -> str:
    """Fetch the NOAA National Hurricane Center active named-storms feed.

    Endpoint: https://www.nhc.noaa.gov/CurrentStorms.json (no auth).
    Returns count of active named storms in all basins (Atlantic, E.Pacific,
    Central Pacific) and top 3 with name, basin, classification, and landfall
    region if forecast data is present. Falls back to a plausible synthetic
    late-season Atlantic system if NOAA is unreachable.
    """
    url = "https://www.nhc.noaa.gov/CurrentStorms.json"
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning("NHC fetch failed: %s — using synthetic", e)
        data = None

    storms: list[dict[str, Any]] = []
    if isinstance(data, dict):
        storms = data.get("activeStorms") or data.get("storms") or []
    elif isinstance(data, list):
        storms = data

    storms = [s for s in storms if isinstance(s, dict)]

    if data is not None:
        lines = [LIVE_BANNER, f"source: NHC nhc.noaa.gov  fetched_at: {_now_iso()}"]
        lines.append(f"active named storms (all basins): {len(storms)}")
        if not storms:
            lines.append("- (no active named storms — basin is quiet)")
        for s in storms[:3]:
            name = s.get("name") or s.get("stormName") or "Unnamed"
            basin = s.get("basin") or s.get("basinId") or s.get("basin_name") or "?"
            class_ = (
                s.get("classification") or s.get("stormType")
                or s.get("intensity") or s.get("class") or "?"
            )
            intensity = s.get("intensity") or s.get("wallaceIntensity") or ""
            advisory = s.get("forecastTrackArea") or s.get("publicAdvisory") or ""
            adv_str = ""
            if isinstance(advisory, dict):
                title = advisory.get("title") or advisory.get("name") or ""
                adv_str = f"  next-advisory: {title}" if title else ""
            elif isinstance(advisory, str) and advisory:
                adv_str = f"  next-advisory: {advisory[:60]}"
            lines.append(f"- {name}  [{basin}]  {class_}  {intensity}{adv_str}")
        return "\n".join(lines)

    # Synthetic — plausible Atlantic late-season system
    synth = [
        {"name": "Tropical Storm Otto", "basin": "AL", "classification": "Tropical Storm",
         "intensity": "55 kt", "publicAdvisory": "Public Advisory 12 — moving NW at 9 kt; "
                                                  "tracking toward central Florida"},
    ]
    lines = [_synth_banner("NHC api unavailable"), f"source: NHC (synthetic)  fetched_at: {_now_iso()}"]
    lines.append(f"active named storms (all basins): {len(synth)} (synthetic)")
    for s in synth:
        lines.append(
            f"- {s['name']}  [{s['basin']}]  {s['classification']}  {s['intensity']}  "
            f"next-advisory: {s['publicAdvisory'][:60]}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 7. openFDA enforcement reports (food + drug recalls)
# ---------------------------------------------------------------------------
def _parse_rss20(xml_text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items
    channel = root.find("channel")
    if channel is None:
        # Some feeds wrap items at root.
        for item in root.findall(".//item"):
            items.append(_rss_item_to_dict(item))
        return items
    for item in channel.findall("item"):
        items.append(_rss_item_to_dict(item))
    return items


def _rss_item_to_dict(item: ET.Element) -> dict[str, Any]:
    def _t(tag: str) -> str:
        el = item.find(tag)
        return (el.text or "").strip() if el is not None and el.text else ""

    return {
        "title": _t("title"),
        "link": _t("link"),
        "pub_date": _t("pubDate"),
        "description": _t("description"),
        "guid": _t("guid"),
    }


@tool
def fetch_fda_recalls() -> str:
    """Fetch recent FDA recalls (food + drug) via openFDA Enforcement Reports.

    Endpoints (no auth required, generous quotas, AWS-fronted CDN):
      - https://api.fda.gov/food/enforcement.json
      - https://api.fda.gov/drug/enforcement.json

    Supply-chain relevance: FDA recalls translate directly into ops actions —
    pull units from inventory at FCs, notify customers who bought a recalled
    SKU, reverse-logistics intake, replacement orders. Class I = "reasonable
    probability of serious adverse health consequence" (drop everything);
    Class II = temporary or medically reversible; Class III = labeling/quality
    without health risk. Falls back to a synthetic shape on transient failure.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y%m%d")
    feeds = [
        ("food", f"https://api.fda.gov/food/enforcement.json?search=report_date:[{cutoff}+TO+9999]&limit=15"),
        ("drug", f"https://api.fda.gov/drug/enforcement.json?search=report_date:[{cutoff}+TO+9999]&limit=10"),
    ]
    all_recalls: list[dict[str, Any]] = []
    fetch_errors: list[str] = []
    for kind, url in feeds:
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=DEFAULT_TIMEOUT)
            if r.status_code == 404:
                # openFDA returns 404 with {"error":{"code":"NOT_FOUND"...}} when zero matches.
                # That's a successful zero-result, not an outage.
                continue
            r.raise_for_status()
            payload = r.json()
            for row in (payload.get("results") or []):
                if isinstance(row, dict):
                    row["_kind"] = kind
                    all_recalls.append(row)
        except Exception as e:
            fetch_errors.append(f"{kind}={e!s}")

    if all_recalls:
        # Sort by report_date desc. openFDA dates are YYYYMMDD strings.
        all_recalls.sort(key=lambda x: x.get("report_date") or "", reverse=True)
        class_counts: dict[str, int] = {}
        for row in all_recalls:
            cls = (row.get("classification") or "?").strip()
            class_counts[cls] = class_counts.get(cls, 0) + 1
        class_summary = ", ".join(f"{v} {k}" for k, v in sorted(class_counts.items()))
        lines = [LIVE_BANNER, f"source: openFDA api.fda.gov  fetched_at: {_now_iso()}"]
        lines.append(f"FDA recalls (last 30d, food + drug): {len(all_recalls)} active — {class_summary}")
        for row in all_recalls[:8]:
            d = (row.get("report_date") or "")[:8]
            cls = (row.get("classification") or "?").replace("Class ", "C")
            kind = row.get("_kind", "")
            product = (row.get("product_description") or "")[:80]
            reason = (row.get("reason_for_recall") or "")[:90]
            firm = (row.get("recalling_firm") or "")[:40]
            lines.append(f"- {d}  {cls:>4s}  [{kind}] {firm}: {product} — {reason}")
        return "\n".join(lines)

    # No results AND no errors = a genuinely quiet 30 days (rare but possible).
    # No results WITH errors = upstream failure → synthetic.
    if fetch_errors:
        logger.warning("openFDA fetch failed: %s — using synthetic", "; ".join(fetch_errors))
        synth = [
            {"date": _days_ago_iso(3)[:10].replace("-", ""),  "cls": "CI",  "kind": "food",
             "firm": "Acme Frozen Foods",
             "product": "Frozen spinach 16oz, lot 4421",
             "reason": "Listeria monocytogenes detected in routine FDA testing"},
            {"date": _days_ago_iso(7)[:10].replace("-", ""),  "cls": "CII", "kind": "drug",
             "firm": "Generic Pharma Inc.",
             "product": "Metoprolol Tartrate 50 mg tablets, lot M-22318",
             "reason": "Out-of-specification dissolution result at 9-month stability test"},
            {"date": _days_ago_iso(11)[:10].replace("-", ""), "cls": "CII", "kind": "food",
             "firm": "Sunshine Snacks Co.",
             "product": "Trail mix 8oz, multiple lots",
             "reason": "Undeclared peanuts — allergen labeling failure"},
        ]
        lines = [_synth_banner("openFDA unreachable"), f"source: openFDA (synthetic)  fetched_at: {_now_iso()}"]
        lines.append(f"FDA recalls (last 30d, synthetic): {len(synth)} active — 1 Class I, 2 Class II")
        for it in synth:
            lines.append(f"- {it['date']}  {it['cls']:>4s}  [{it['kind']}] {it['firm']}: {it['product']} — {it['reason']}")
        return "\n".join(lines)

    # Truly zero recent recalls — render as live but quiet.
    lines = [LIVE_BANNER, f"source: openFDA api.fda.gov  fetched_at: {_now_iso()}"]
    lines.append("FDA recalls (last 30d, food + drug): 0 active (quiet window)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 8. IMF PortWatch — choke-point daily vessel flow (Suez / Panama / Hormuz / Malacca / Dover)
# ---------------------------------------------------------------------------
_CHOKE_POINTS = {
    # Substrings to match in the `chokepoint` field returned by ArcGIS feature service.
    "suez": "Suez Canal",
    "panama": "Panama Canal",
    "hormuz": "Strait of Hormuz",
    "malacca": "Strait of Malacca",
    "dover": "Strait of Dover",
    "bab": "Bab-el-Mandeb",  # tracked alongside Suez for Red Sea anomalies
}


@tool
def fetch_marine_traffic_choke_points() -> str:
    """Fetch IMF PortWatch daily vessel transits at maritime choke points, filter
    to Suez, Panama, Hormuz, Malacca, Dover (+ Bab-el-Mandeb), and flag any
    point where current vessel count is > 2 standard deviations above the
    rolling baseline.

    Endpoint: ArcGIS Feature Service hosted by IMF PortWatch
    (`Daily_Chokepoints_Data` on services9.arcgis.com/weJ1QsnbMYJlCHdG). Falls
    back to synthetic if unreachable. Fields used: `portname`, `n_total`, `date`.
    """
    url = (
        "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/"
        "Daily_Chokepoints_Data/FeatureServer/0/query"
    )
    # Filter server-side to the relevant chokepoints; pull recent dates only.
    # ArcGIS feature limit defaults to 2000; we ask for ~30d × 6 points = ~180.
    name_clause = " OR ".join(
        [f"portname LIKE '%{key.capitalize()}%'" for key in _CHOKE_POINTS.keys()]
        + ["portname LIKE '%Suez%'", "portname LIKE '%Panama%'",
           "portname LIKE '%Hormuz%'", "portname LIKE '%Malacca%'",
           "portname LIKE '%Dover%'", "portname LIKE '%Mandeb%'"]
    )
    params = {
        "where": f"({name_clause})",
        "outFields": "portname,n_total,date",
        "f": "json",
        "resultRecordCount": "1000",
        "orderByFields": "date DESC",
        "returnGeometry": "false",
    }
    try:
        r = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        payload = r.json()
        features = payload.get("features") or []
    except Exception as e:
        logger.warning("PortWatch choke-point fetch failed: %s — using synthetic", e)
        features = []

    if features:
        # Bucket vessel counts by choke point. Field names vary by service, so we
        # look at all attributes and pull the obvious ones.
        per_point: dict[str, list[tuple[str, float]]] = {}
        for feat in features:
            attrs = feat.get("attributes") or {}
            # Try several plausible field names for choke-point label.
            label_raw = (
                attrs.get("chokepoint") or attrs.get("ChokePoint")
                or attrs.get("CHOKE_POINT") or attrs.get("chokepoint_name")
                or attrs.get("Name") or attrs.get("portname") or ""
            )
            if not isinstance(label_raw, str):
                continue
            low = label_raw.lower()
            matched = None
            for key, nice in _CHOKE_POINTS.items():
                if key in low:
                    matched = nice
                    break
            if not matched:
                continue
            # Find a numeric vessel count field
            count = None
            for k in (
                "n_total", "total_vessels", "vessel_count", "vesselCount",
                "Total", "vessels", "n_vessels", "transits", "n_transits",
                "value", "Value", "Count",
            ):
                v = attrs.get(k)
                if isinstance(v, (int, float)):
                    count = float(v)
                    break
            if count is None:
                continue
            # Date — ArcGIS commonly uses epoch ms in a `date` field
            date_field = attrs.get("date") or attrs.get("Date") or attrs.get("period") or attrs.get("day")
            if isinstance(date_field, (int, float)):
                date_iso = datetime.fromtimestamp(date_field / 1000, tz=timezone.utc).date().isoformat()
            elif isinstance(date_field, str):
                date_iso = date_field[:10]
            else:
                date_iso = "?"
            per_point.setdefault(matched, []).append((date_iso, count))

        anomalies: list[dict[str, Any]] = []
        summaries: list[dict[str, Any]] = []
        for cp, series in per_point.items():
            series.sort(reverse=True)
            today_date, today_val = series[0]
            baseline = [v for _, v in series[1:31]]  # rolling ~30d baseline
            if len(baseline) >= 5:
                mean = statistics.mean(baseline)
                stdev = statistics.pstdev(baseline) or 1.0
                z = (today_val - mean) / stdev
            else:
                mean = today_val
                stdev = 0.0
                z = 0.0
            summary = {
                "chokepoint": cp,
                "today_date": today_date,
                "today_vessels": today_val,
                "baseline_mean": mean,
                "baseline_stdev": stdev,
                "z_score": z,
            }
            summaries.append(summary)
            if abs(z) > 2.0:
                anomalies.append(summary)

        summaries.sort(key=lambda s: abs(s["z_score"]), reverse=True)

        lines = [LIVE_BANNER, f"source: IMF PortWatch (ArcGIS)  fetched_at: {_now_iso()}"]
        lines.append(
            f"choke points tracked: {len(per_point)} / 6  (Suez, Panama, Hormuz, Malacca, Dover, Bab-el-Mandeb)"
        )
        lines.append(f"anomalies (|z| > 2.0 vs ~30d baseline): {len(anomalies)}")
        for s in summaries[:5]:
            flag = "  [!]" if abs(s["z_score"]) > 2.0 else ""
            lines.append(
                f"- {s['chokepoint']:>22}  {s['today_date']}  "
                f"vessels: {s['today_vessels']:.0f}  "
                f"baseline: {s['baseline_mean']:.0f} ± {s['baseline_stdev']:.0f}  "
                f"z: {s['z_score']:+.2f}{flag}"
            )
        if not summaries:
            lines.append("- (no choke-point records matched the configured list)")
        return "\n".join(lines)

    # Synthetic fallback
    today = datetime.now(timezone.utc).date().isoformat()
    synth = [
        {"chokepoint": "Suez Canal", "today_date": today,
         "today_vessels": 28, "baseline_mean": 56, "baseline_stdev": 4, "z_score": -7.0},
        {"chokepoint": "Bab-el-Mandeb", "today_date": today,
         "today_vessels": 14, "baseline_mean": 38, "baseline_stdev": 3, "z_score": -8.0},
        {"chokepoint": "Panama Canal", "today_date": today,
         "today_vessels": 36, "baseline_mean": 33, "baseline_stdev": 5, "z_score": 0.6},
    ]
    anomalies = [s for s in synth if abs(s["z_score"]) > 2.0]
    lines = [_synth_banner("PortWatch ArcGIS unavailable"), f"source: IMF PortWatch (synthetic)  fetched_at: {_now_iso()}"]
    lines.append("choke points tracked: 3 / 6 (synthetic sample)")
    lines.append(f"anomalies (|z| > 2.0 vs ~30d baseline): {len(anomalies)}")
    for s in synth:
        flag = "  [!]" if abs(s["z_score"]) > 2.0 else ""
        lines.append(
            f"- {s['chokepoint']:>22}  {s['today_date']}  "
            f"vessels: {s['today_vessels']}  "
            f"baseline: {s['baseline_mean']} ± {s['baseline_stdev']}  "
            f"z: {s['z_score']:+.2f}{flag}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 9. Aviation Weather PIREPs — Pilot Reports near major US freight hubs
# ---------------------------------------------------------------------------
# Map of freight-hub airport ICAO/IATA → (lat, lon, label).  Used to bucket
# PIREPs by proximity (great-circle distance in nautical miles).
_PIREP_FREIGHT_HUBS: dict[str, tuple[float, float, str]] = {
    "MEM": (35.0424, -89.9767, "Memphis (FedEx Superhub)"),
    "SDF": (38.1744, -85.7361, "Louisville (UPS Worldport)"),
    "IND": (39.7173, -86.2944, "Indianapolis (FedEx Express)"),
    "ANC": (61.1744, -149.9961, "Anchorage (Asia-NA freight)"),
    "LAX": (33.9425, -118.4081, "Los Angeles International"),
    "ORD": (41.9742, -87.9073, "Chicago O'Hare"),
    "ATL": (33.6407, -84.4277, "Atlanta Hartsfield"),
    "DFW": (32.8998, -97.0403, "Dallas-Fort Worth"),
    "EWR": (40.6925, -74.1687, "Newark"),
}

_PIREP_RADIUS_NM = 150.0  # nautical miles around each hub
_NM_PER_DEG_LAT = 60.0    # 1° latitude ≈ 60 nm (good enough for binning)


def _great_circle_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance between two points in nautical miles."""
    import math
    r_nm = 3440.065  # Earth's mean radius in nautical miles
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r_nm * c


def _nearest_hub(lat: float, lon: float) -> tuple[str, str, float] | None:
    """Return (hub_code, hub_label, distance_nm) for the closest freight hub
    within _PIREP_RADIUS_NM, or None if no hub is within range."""
    best: tuple[str, str, float] | None = None
    for code, (hlat, hlon, label) in _PIREP_FREIGHT_HUBS.items():
        # Cheap bbox prefilter: 150nm ≈ 2.5° lat.  If |Δlat| > 4°, skip Haversine.
        if abs(lat - hlat) > 4.0:
            continue
        d = _great_circle_nm(lat, lon, hlat, hlon)
        if d <= _PIREP_RADIUS_NM and (best is None or d < best[2]):
            best = (code, label, d)
    return best


def _classify_pirep(report: str) -> str | None:
    """Tag a PIREP with the dominant hazard mentioned in the raw text.

    Returns 'turbulence', 'icing', 'wind shear', 'IMC', or None.
    """
    if not report:
        return None
    s = report.upper()
    # Order matters — wind shear is more specific than turbulence.
    if "WS " in s or "WIND SHEAR" in s or "/WS" in s:
        return "wind shear"
    if "TB " in s or "TURB" in s or "/TB" in s or "CHOP" in s:
        return "turbulence"
    if "IC " in s or "ICG" in s or "ICING" in s or "/IC" in s:
        return "icing"
    if "IMC" in s or "OVC" in s or " BKN" in s:
        return "IMC"
    return None


@tool
def fetch_aviation_weather_pireps() -> str:
    """Fetch recent pilot reports (PIREPs) of turbulence, icing, and adverse
    in-flight conditions near major US air-freight hubs (MEM, SDF, IND, ANC,
    LAX, ORD, ATL, DFW, EWR). Helps surface airborne disruption risk that
    affects overnight freight ops.

    Endpoint: https://aviationweather.gov/api/data/pirep?format=json&hoursBeforeNow=3&bbox=...
    (no auth, no rate limit).  The API requires either a bounding box or
    station IDs — we issue two bbox queries (CONUS + Alaska for ANC) and
    merge. Filters to PIREPs within ~150 nm of a freight hub and tags the
    dominant hazard.  Falls back to plausible synthetic PIREPs on network
    error so the rail always shows something.
    """
    # API requires a bounding box. CONUS covers MEM/SDF/IND/LAX/ORD/ATL/DFW/EWR;
    # ANC needs a separate Alaska bbox.
    bboxes = [
        "24,-125,49,-65",   # CONUS
        "55,-170,72,-130",  # Alaska (for ANC)
    ]
    rows: list[dict[str, Any]] = []
    errors = 0
    for bbox in bboxes:
        url = (
            "https://aviationweather.gov/api/data/pirep"
            f"?format=json&hoursBeforeNow=3&bbox={bbox}"
        )
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                             timeout=DEFAULT_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                rows.extend(row for row in data if isinstance(row, dict))
        except Exception as e:
            logger.warning("Aviation Weather PIREP bbox %s fetch failed: %s", bbox, e)
            errors += 1

    if rows:
        scoped: list[dict[str, Any]] = []
        for row in rows:
            lat = row.get("lat")
            lon = row.get("lon")
            if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
                continue
            hub = _nearest_hub(float(lat), float(lon))
            if hub is None:
                continue
            raw_report = (row.get("rawOb") or row.get("rawObs") or row.get("report") or "")
            # API exposes intensity fields directly — prefer those over regex on rawOb.
            hazard = None
            if (row.get("tbInt1") or row.get("tbInt2") or "").strip():
                hazard = "turbulence"
            elif (row.get("icgInt1") or row.get("icgInt2") or "").strip():
                hazard = "icing"
            else:
                hazard = _classify_pirep(raw_report)
            fltlvl = row.get("fltLvl") or row.get("fltlvl") or row.get("altitude") or ""
            scoped.append({
                "hub_code": hub[0],
                "hub_label": hub[1],
                "distance_nm": hub[2],
                "alt_ft": f"FL{fltlvl}" if isinstance(fltlvl, (int, float)) and fltlvl else str(fltlvl or ""),
                "obs_time": row.get("obsTime") or row.get("receiptTime") or "",
                "report": (raw_report or "").strip()[:80],
                "hazard": hazard,
            })

        # Sort: hazardous reports first (turbulence/icing/wind shear), then by distance asc.
        _HAZARD_RANK = {"wind shear": 0, "turbulence": 1, "icing": 1, "IMC": 2}
        scoped.sort(key=lambda it: (_HAZARD_RANK.get(it.get("hazard") or "", 3), it.get("distance_nm", 999)))

        lines = [LIVE_BANNER, f"source: aviationweather.gov PIREP  fetched_at: {_now_iso()}"]
        lines.append(
            f"PIREPs within 150nm of freight hubs (past 3h): {len(scoped)} "
            f"(of {len(rows)} total)"
        )
        # Hub-level rollup: count of hazardous PIREPs per hub.
        per_hub: dict[str, int] = {}
        for it in scoped:
            if it.get("hazard"):
                per_hub[it["hub_code"]] = per_hub.get(it["hub_code"], 0) + 1
        if per_hub:
            roll = ", ".join(f"{k}={v}" for k, v in sorted(per_hub.items(), key=lambda kv: -kv[1])[:5])
            lines.append(f"hazardous-PIREP rollup: {roll}")
        for it in scoped[:5]:
            tag = f"  [{it['hazard']}]" if it.get("hazard") else ""
            lines.append(
                f"- {it['hub_code']:>3} ({it['hub_label']})  "
                f"d={it['distance_nm']:.0f}nm  alt={it.get('alt_ft','')}  "
                f"\"{it['report']}\"{tag}"
            )
        if not scoped:
            lines.append("- (no PIREPs within 150nm of any tracked freight hub this cycle)")
        return "\n".join(lines)

    # Synthetic fallback — plausible PIREPs near MEM and ORD
    synth = [
        {"hub_code": "MEM", "hub_label": _PIREP_FREIGHT_HUBS["MEM"][2],
         "distance_nm": 42, "alt_ft": "FL310",
         "report": "UA /OV MEM090020 /TM 0345 /FL310 /TP B763 /TB MOD CHOP",
         "hazard": "turbulence"},
        {"hub_code": "ORD", "hub_label": _PIREP_FREIGHT_HUBS["ORD"][2],
         "distance_nm": 88, "alt_ft": "FL240",
         "report": "UA /OV ORD250040 /TM 0410 /FL240 /TP A320 /IC MOD RIME",
         "hazard": "icing"},
        {"hub_code": "SDF", "hub_label": _PIREP_FREIGHT_HUBS["SDF"][2],
         "distance_nm": 30, "alt_ft": "FL280",
         "report": "UA /OV SDF180015 /TM 0428 /FL280 /TP B752 /TB LGT-MOD",
         "hazard": "turbulence"},
    ]
    lines = [_synth_banner("Aviation Weather PIREP feed unavailable"),
             f"source: aviationweather.gov PIREP (synthetic)  fetched_at: {_now_iso()}"]
    lines.append(f"PIREPs within 150nm of freight hubs (past 3h): {len(synth)} (synthetic)")
    lines.append("hazardous-PIREP rollup: MEM=1, ORD=1, SDF=1")
    for it in synth:
        tag = f"  [{it['hazard']}]" if it.get("hazard") else ""
        lines.append(
            f"- {it['hub_code']:>3} ({it['hub_label']})  "
            f"d={it['distance_nm']}nm  alt={it['alt_ft']}  "
            f"\"{it['report']}\"{tag}"
        )
    return "\n".join(lines)


# Convenience export list for binding into the LangGraph agent
EXTRA_TOOLS_V3 = [
    fetch_faa_tfr,
    fetch_cbp_border_waits,
    fetch_hackernews_supply_chain,
    fetch_eia_fuel_prices,
    fetch_usgs_volcano_alerts,
    fetch_nhc_hurricanes,
    fetch_fda_recalls,
    fetch_marine_traffic_choke_points,
    fetch_aviation_weather_pireps,
]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for fn in EXTRA_TOOLS_V3:
        print("===", fn.name, "===")
        print(fn.invoke({}))
        print()
