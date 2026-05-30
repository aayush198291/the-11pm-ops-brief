"""Additional external-signal data tools for The 11 PM Ops Brief agent.

Five additional real-time disruption sources, each with a deterministic
synthetic fallback so the demo never fails because an upstream API is down:

  - USGS Earthquake Hazards Program (GeoJSON, no auth)
  - FEMA OpenFEMA disaster declarations (no auth)
  - NASA EONET (Earth Observatory Natural Event Tracker, no auth)
  - Reddit RSS pulse over r/supplychain + r/logistics + r/freight (no auth, UA required)
  - Google News RSS supply-chain headlines (no auth, no rate limit)

Each tool returns a single string. First line is a banner:
  - "🟢 LIVE"                       — fresh data from upstream API
  - "🟡 SYNTHETIC (api unavailable)" — fallback because upstream failed

All tools are decorated with @tool so they can be bound to LangGraph.
"""
from __future__ import annotations
import json
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

USER_AGENT = "OpsBriefAgent/1.0 (apatel@shipbob.com)"
DEFAULT_TIMEOUT = 20  # seconds

LIVE_BANNER = "🟢 LIVE"
SYNTH_BANNER = "🟡 SYNTHETIC (api unavailable)"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hours_ago_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 1. USGS Earthquake Hazards Program — GeoJSON, no auth
# ---------------------------------------------------------------------------
@tool
def fetch_usgs_earthquakes() -> str:
    """Fetch USGS Earthquake Hazards Program 'all earthquakes past day' GeoJSON feed,
    filtered to magnitude >= 4.0 in US / US territories. Free, no auth.

    Returns a skimmable multi-line string with total count and the top 5 by magnitude
    (location, magnitude, time). Falls back to synthetic CA/AK quakes if upstream fails.
    """
    url = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson"
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        feats = data.get("features") or []
        us_quakes: list[dict[str, Any]] = []
        # Country/territory tokens to keep — USGS 'place' strings end with these
        us_tokens = (
            "California", "CA", "Alaska", "AK", "Nevada", "NV", "Hawaii", "HI",
            "Oregon", "OR", "Washington", "WA", "Utah", "UT", "Idaho", "ID",
            "Wyoming", "WY", "Montana", "MT", "Oklahoma", "OK", "Texas", "TX",
            "Arkansas", "AR", "Tennessee", "TN", "Missouri", "MO", "Arizona", "AZ",
            "New Mexico", "NM", "Colorado", "CO", "Kansas", "KS", "Puerto Rico",
            "Virgin Islands", "Guam", "Northern Mariana", "American Samoa",
        )
        for f in feats:
            props = f.get("properties") or {}
            mag = props.get("mag")
            place = (props.get("place") or "")
            if mag is None or mag < 4.0:
                continue
            if not any(tok in place for tok in us_tokens):
                continue
            ts_ms = props.get("time")
            ts_iso = (
                datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")
                if isinstance(ts_ms, (int, float)) else None
            )
            us_quakes.append({
                "mag": float(mag),
                "place": place,
                "time": ts_iso,
                "url": props.get("url"),
            })
        us_quakes.sort(key=lambda q: q["mag"], reverse=True)

        lines = [LIVE_BANNER, f"source: USGS earthquake.usgs.gov  fetched_at: {_now_iso()}"]
        lines.append(f"M>=4.0 in US / territories (past 24h): {len(us_quakes)} event(s)")
        if not us_quakes:
            lines.append("- (no M>=4.0 US events in the past 24h)")
        for q in us_quakes[:5]:
            lines.append(f"- M{q['mag']:.1f}  {q['place']}  @ {q['time']}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning("USGS fetch failed: %s — using synthetic", e)

    # Synthetic fallback — realistic-magnitude CA/AK events within last 24h
    synth = [
        {"mag": 4.6, "place": "12km SSE of Petrolia, CA", "time": _hours_ago_iso(3)},
        {"mag": 4.3, "place": "82km SE of Sand Point, AK", "time": _hours_ago_iso(8)},
        {"mag": 4.1, "place": "5km NW of Ridgecrest, CA", "time": _hours_ago_iso(14)},
    ]
    lines = [SYNTH_BANNER, f"source: USGS (synthetic fallback)  fetched_at: {_now_iso()}"]
    lines.append(f"M>=4.0 in US / territories (past 24h): {len(synth)} event(s)")
    for q in synth:
        lines.append(f"- M{q['mag']:.1f}  {q['place']}  @ {q['time']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 2. FEMA OpenFEMA Disaster Declarations
# ---------------------------------------------------------------------------
@tool
def fetch_fema_disasters() -> str:
    """Fetch FEMA OpenFEMA Disaster Declarations Summaries for the past 7 days.
    No auth. Returns count by state and a breakdown of incident types
    (severe storm, fire, flood, etc.). Falls back to synthetic declarations
    if OpenFEMA is unreachable.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    url = (
        "https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries"
        f"?$filter=declarationDate%20ge%20%27{cutoff}%27&$top=50"
        "&$orderby=declarationDate%20desc"
    )
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        rows = data.get("DisasterDeclarationsSummaries") or []

        by_state: dict[str, int] = {}
        by_type: dict[str, int] = {}
        recent: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for row in rows:
            st = (row.get("state") or "??").upper()
            inc = row.get("incidentType") or "Unknown"
            decl_date = row.get("declarationDate") or ""
            # Collapse duplicate disasters declared across counties
            key = f"{st}|{inc}|{decl_date[:10]}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            by_state[st] = by_state.get(st, 0) + 1
            by_type[inc] = by_type.get(inc, 0) + 1
            recent.append({
                "state": st,
                "incident_type": inc,
                "title": row.get("declarationTitle"),
                "declaration_date": decl_date,
                "disaster_number": row.get("disasterNumber"),
            })

        lines = [LIVE_BANNER, f"source: FEMA OpenFEMA  fetched_at: {_now_iso()}"]
        lines.append(f"declarations since {cutoff}: {len(recent)} unique event(s)")
        if by_state:
            top_states = sorted(by_state.items(), key=lambda x: -x[1])[:8]
            lines.append("by state: " + ", ".join(f"{s}={n}" for s, n in top_states))
        if by_type:
            top_types = sorted(by_type.items(), key=lambda x: -x[1])[:6]
            lines.append("by type: " + ", ".join(f"{t}={n}" for t, n in top_types))
        if not recent:
            lines.append("- (no declarations in the past 7 days)")
        for d in recent[:5]:
            lines.append(
                f"- {d['state']}  {d['incident_type']}  "
                f"\"{(d['title'] or '')[:80]}\"  ({d['declaration_date'][:10]})"
            )
        return "\n".join(lines)
    except Exception as e:
        logger.warning("FEMA fetch failed: %s — using synthetic", e)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    synth = [
        {"state": "TX", "incident_type": "Severe Storm",
         "title": "Texas Severe Storms and Flooding", "declaration_date": today},
        {"state": "CA", "incident_type": "Fire",
         "title": "California Wildfires", "declaration_date": today},
        {"state": "FL", "incident_type": "Flood",
         "title": "Florida Coastal Flooding", "declaration_date": today},
        {"state": "OK", "incident_type": "Severe Storm",
         "title": "Oklahoma Tornadoes and Severe Storms", "declaration_date": today},
    ]
    by_state = {d["state"]: 1 for d in synth}
    by_type: dict[str, int] = {}
    for d in synth:
        by_type[d["incident_type"]] = by_type.get(d["incident_type"], 0) + 1

    lines = [SYNTH_BANNER, f"source: FEMA (synthetic fallback)  fetched_at: {_now_iso()}"]
    lines.append(f"declarations since (past 7d): {len(synth)} unique event(s)")
    lines.append("by state: " + ", ".join(f"{s}={n}" for s, n in by_state.items()))
    lines.append("by type: " + ", ".join(f"{t}={n}" for t, n in by_type.items()))
    for d in synth:
        lines.append(
            f"- {d['state']}  {d['incident_type']}  \"{d['title']}\"  ({d['declaration_date']})"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3. NASA EONET — Natural Event Tracker
# ---------------------------------------------------------------------------
@tool
def fetch_nasa_eonet() -> str:
    """Fetch open natural events from NASA EONET (Earth Observatory Natural Event
    Tracker) over the past 3 days. No auth. Filters to categories relevant to
    supply-chain ops: wildfires, severe storms, volcanoes. Returns counts by
    category and the top 3 most-recent active events with coordinates.
    Falls back to synthetic events if EONET is unreachable.
    """
    url = "https://eonet.gsfc.nasa.gov/api/v3/events"
    params = {"status": "open", "days": "3"}
    interest = {"wildfires", "severeStorms", "volcanoes"}

    try:
        r = requests.get(
            url, params=params, headers={"User-Agent": USER_AGENT}, timeout=DEFAULT_TIMEOUT
        )
        r.raise_for_status()
        data = r.json()
        events = data.get("events") or []

        kept: list[dict[str, Any]] = []
        by_cat: dict[str, int] = {}
        for ev in events:
            cats = ev.get("categories") or []
            cat_ids = {c.get("id") for c in cats if c}
            if not (cat_ids & interest):
                continue
            cat_title = cats[0].get("title") if cats else "Unknown"
            geoms = ev.get("geometry") or []
            last = geoms[-1] if geoms else {}
            coords = last.get("coordinates")
            when = last.get("date")
            kept.append({
                "title": ev.get("title"),
                "category": cat_title,
                "coords": coords,
                "date": when,
                "link": ev.get("link"),
            })
            by_cat[cat_title] = by_cat.get(cat_title, 0) + 1

        kept.sort(key=lambda e: e.get("date") or "", reverse=True)

        lines = [LIVE_BANNER, f"source: NASA EONET v3  fetched_at: {_now_iso()}"]
        lines.append(
            f"open events (past 3d) in wildfires/severeStorms/volcanoes: {len(kept)}"
        )
        if by_cat:
            lines.append(
                "by category: " + ", ".join(f"{c}={n}" for c, n in sorted(by_cat.items()))
            )
        if not kept:
            lines.append("- (no events of interest in the past 3 days)")
        for ev in kept[:3]:
            coords = ev.get("coords")
            coord_str = (
                f"[{coords[1]:.2f}, {coords[0]:.2f}]"
                if isinstance(coords, list) and len(coords) >= 2
                and all(isinstance(c, (int, float)) for c in coords[:2])
                else "[?,?]"
            )
            lines.append(
                f"- {ev['category']}  \"{(ev['title'] or '')[:70]}\"  "
                f"{coord_str}  @ {ev.get('date')}"
            )
        return "\n".join(lines)
    except Exception as e:
        logger.warning("EONET fetch failed: %s — using synthetic", e)

    synth = [
        {"title": "Bonita Fire, California", "category": "Wildfires",
         "coords": [-118.42, 34.21], "date": _hours_ago_iso(6)},
        {"title": "Pine Ridge Fire, Oregon", "category": "Wildfires",
         "coords": [-122.71, 44.05], "date": _hours_ago_iso(20)},
        {"title": "Tropical Storm Carla", "category": "Severe Storms",
         "coords": [-87.50, 25.10], "date": _hours_ago_iso(2)},
    ]
    lines = [SYNTH_BANNER, f"source: NASA EONET (synthetic fallback)  fetched_at: {_now_iso()}"]
    lines.append(f"open events (past 3d) in wildfires/severeStorms/volcanoes: {len(synth)}")
    by_cat: dict[str, int] = {}
    for e in synth:
        by_cat[e["category"]] = by_cat.get(e["category"], 0) + 1
    lines.append("by category: " + ", ".join(f"{c}={n}" for c, n in by_cat.items()))
    for ev in synth:
        c = ev["coords"]
        lines.append(
            f"- {ev['category']}  \"{ev['title']}\"  [{c[1]:.2f}, {c[0]:.2f}]  @ {ev['date']}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 4. Reddit RSS — supply chain / logistics / freight pulse
# ---------------------------------------------------------------------------
_NEGATIVE_KEYWORDS = (
    "delay", "delays", "delayed", "strike", "shortage", "recall",
    "closure", "closed", "outage", "disruption", "lockout", "shutdown",
    "ground stop", "stranded", "backlog", "bottleneck", "missing", "stolen",
)


def _parse_reddit_rss(xml_text: str, sub: str) -> list[dict[str, Any]]:
    """Parse an Atom/RSS feed from reddit. Reddit serves Atom-style XML."""
    items: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items
    # Reddit uses Atom namespace
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entries = root.findall("a:entry", ns)
    for e in entries[:20]:
        title_el = e.find("a:title", ns)
        link_el = e.find("a:link", ns)
        updated_el = e.find("a:updated", ns)
        items.append({
            "title": (title_el.text or "").strip() if title_el is not None else "",
            "url": link_el.get("href") if link_el is not None else None,
            "updated": (updated_el.text or "").strip() if updated_el is not None else None,
            "subreddit": sub,
        })
    return items


@tool
def fetch_reddit_supply_chain_pulse() -> str:
    """Pull the latest posts from r/supplychain, r/logistics, r/freight via public RSS.
    No auth required (User-Agent header is mandatory or Reddit returns 429).
    Returns the top 10 posts across subs, flags posts containing negative-sentiment
    keywords (delay, strike, shortage, recall, closure, outage, disruption).
    Falls back to synthetic threads if Reddit is unreachable / blocks.
    """
    subs = ["supplychain", "logistics", "freight"]
    all_items: list[dict[str, Any]] = []
    errors = 0
    for sub in subs:
        url = f"https://www.reddit.com/r/{sub}/new.rss"
        try:
            r = requests.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/atom+xml,application/xml,text/xml"},
                timeout=DEFAULT_TIMEOUT,
            )
            r.raise_for_status()
            parsed = _parse_reddit_rss(r.text, sub)
            if not parsed:
                errors += 1
            all_items.extend(parsed)
        except Exception as e:
            logger.warning("Reddit fetch failed for r/%s: %s", sub, e)
            errors += 1

    if all_items and errors < len(subs):
        # Sort by updated desc; tolerate missing timestamps
        all_items.sort(key=lambda x: x.get("updated") or "", reverse=True)
        top = all_items[:10]
        flagged = 0
        lines = [LIVE_BANNER, f"source: Reddit RSS (r/{', r/'.join(subs)})  fetched_at: {_now_iso()}"]
        lines.append(
            f"posts: {len(top)} (of {len(all_items)} total across {len(subs)} subs); "
            f"negative-keyword flags below marked with [!]"
        )
        for it in top:
            title = it.get("title") or ""
            low = title.lower()
            hit = next((k for k in _NEGATIVE_KEYWORDS if k in low), None)
            tag = "[!] " if hit else "    "
            if hit:
                flagged += 1
            updated = (it.get("updated") or "")[:19]
            lines.append(f"{tag}r/{it['subreddit']:>12}  {updated}  \"{title[:90]}\"")
        lines.append(f"negative-sentiment flagged: {flagged}/{len(top)}")
        return "\n".join(lines)

    # Synthetic fallback — realistic threads
    synth = [
        {"sub": "supplychain",
         "title": "Major carrier suspending pickups in upper Midwest due to driver strike vote",
         "updated": _hours_ago_iso(2)},
        {"sub": "logistics",
         "title": "Anyone else seeing massive delays at Long Beach this week?",
         "updated": _hours_ago_iso(4)},
        {"sub": "freight",
         "title": "FedEx Ground recall on damaged trailer fleet — anyone got more info?",
         "updated": _hours_ago_iso(7)},
        {"sub": "supplychain",
         "title": "Maersk Newark terminal closure rumor — any confirmation?",
         "updated": _hours_ago_iso(10)},
        {"sub": "logistics",
         "title": "Q2 freight rate forecasting — methodology question",
         "updated": _hours_ago_iso(14)},
    ]
    lines = [
        SYNTH_BANNER,
        f"source: Reddit RSS (synthetic fallback)  fetched_at: {_now_iso()}",
        f"posts: {len(synth)} (synthetic across r/supplychain, r/logistics, r/freight)",
    ]
    flagged = 0
    for it in synth:
        low = it["title"].lower()
        hit = next((k for k in _NEGATIVE_KEYWORDS if k in low), None)
        tag = "[!] " if hit else "    "
        if hit:
            flagged += 1
        lines.append(f"{tag}r/{it['sub']:>12}  {it['updated'][:19]}  \"{it['title']}\"")
    lines.append(f"negative-sentiment flagged: {flagged}/{len(synth)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5. Google News RSS — supply chain disruption headlines
# ---------------------------------------------------------------------------
def _parse_google_news_rss(xml_text: str) -> list[dict[str, Any]]:
    """Parse Google News RSS 2.0 feed."""
    items: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items
    channel = root.find("channel")
    if channel is None:
        return items
    for item in channel.findall("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        pub_el = item.find("pubDate")
        # Google News RSS embeds source as <source url="...">Name</source>
        src_el = item.find("source")
        items.append({
            "title": (title_el.text or "").strip() if title_el is not None else "",
            "url": (link_el.text or "").strip() if link_el is not None else None,
            "pub_date": (pub_el.text or "").strip() if pub_el is not None else None,
            "source": (src_el.text or "").strip() if src_el is not None else "Unknown",
        })
    return items


# Multi-query strategy: Google News RSS limits each search to ~100 results
# and biases toward popular/historical articles, so we run 3 narrower queries
# that each pull from different result pools, dedupe, then rank.
# Each list is one Google News query (OR'd together).
_GNEWS_QUERIES = [
    # Pool 1: macro disruptions (port/rail/strike/closure)
    (
        '"port strike" OR "freight strike" OR "trucking strike" OR "rail strike" '
        'OR "port closure" OR "port congestion" OR "rail derailment" OR "rail outage" '
        'OR "supply chain disruption" OR "logistics disruption" OR "bridge closure"'
    ),
    # Pool 2: parcel/ground carrier operational impacts. Broader than the
    # ultra-specific "FedEx hub" wording because Google News articles use
    # varied phrasing. Aerospace "delivery delays" cluster gets killed by
    # the aerospace demoter list, not by query narrowness.
    (
        '"FedEx" delay OR delays OR strike OR outage '
        'OR "UPS" delay OR delays OR strike OR outage '
        'OR "USPS" suspended OR delays '
        'OR "Amazon delivery" delay OR delays '
        'OR "parcel" delays OR strike '
        'OR "shipment delays"'
    ),
    # Pool 3: facility/operational incidents
    (
        '"warehouse fire" OR "factory fire" OR "plant shutdown" OR "plant closure" '
        'OR "production halt" OR "manufacturing shutdown" OR "distribution center" '
        'OR "fulfillment center" OR "freight delays" OR "shipping delays" '
        'OR "trucking halted"'
    ),
    # Pool 4: catch-all broad query — relies entirely on the boost/demote
    # scoring to filter. Backstop for nights when the narrower queries
    # return only stale historical articles.
    (
        '"supply chain" OR "supply-chain" OR logistics OR freight OR trucking '
        'OR "ocean freight" OR "air cargo"'
    ),
]

# Relevance boosters (any of these in the title → +2 score per match)
_GNEWS_BOOST = {
    "strike", "disruption", "delay", "delays", "outage", "fire",
    "closure", "closed", "shutdown", "derailment", "congestion",
    "crash", "halt", "halted", "suspended", "stranded", "backlog",
    "crisis", "evacuation", "recall", "grounded", "blockade",
    "spill", "explosion", "collision", "blackout",
}

# Relevance demoters (any of these → -3 score; pushes BizDev noise down)
_GNEWS_DEMOTE = {
    # BizDev / leadership noise
    "leadership", "ceo", "executive", "appointment", "appoints", "promote",
    "hires", "hire", "award", "honored", "fulbright", "professor",
    "earnings", "quarterly", "quarter", "revenue", "profit", "guidance",
    "acquires", "acquisition", "merger", "partnership", "partners with",
    "invests", "investment", "raises", "funding", "venture", "ipo",
    "expands services", "launches new", "announces", "unveils",
    # Explainer / opinion / listicle / historical analysis (not news of an event)
    "best practices", "tips", "how to", "explained", "report finds",
    "guide to", "charts show", "lessons", "what to do about",
    "history of", "deep dive", "case study", "5 ways", "top 10",
    "perspective", "opinion", "op-ed", "commentary", "analysis:",
    # Aerospace / defense — technically supply-chain stories but useless
    # for ecom/3PL ops. Drop the "Airbus A350 delivery delays" cluster.
    "airbus", "boeing", "a350", "a320", "a380", "777", "787", "737",
    "tomahawk", "missile", "fighter jet", "f-35", "f35", "fighter aircraft",
    "submarine", "warship", "destroyer", "frigate", "navy ship",
    "pentagon", "armament", "munitions", "ammunition", "weapons system",
    "tank delivery", "armored vehicle",
}


# US-locality boosters (any of these → +1; specific named US places that
# almost certainly mean the article is about US supply chains)
_GNEWS_US_BOOST = {
    # US states / common geo markers
    "u.s.", "united states", "america", "american",
    "memphis", "atlanta", "louisville", "indianapolis", "anchorage",
    "los angeles", "long beach", "savannah", "houston", "new york", "chicago",
    "dallas", "miami", "seattle", "san francisco", "oakland", "newark",
    "norfolk", "baltimore", "philadelphia", "boston", "phoenix",
    "california", "texas", "florida", "georgia", "tennessee", "ohio",
    "pennsylvania", "illinois", "michigan", "indiana",
    # US-only carriers / agencies
    "usps", "amtrak", "norfolk southern", "csx", "union pacific", "bnsf",
    "teamsters", "ila", "ilwu",
}

# Clearly-foreign location demoters (any → -2; story is about non-US supply
# chain even if it appears in our US-locale RSS feed)
_GNEWS_FOREIGN_DEMOTE = {
    "kyiv", "ukraine", "ukrainian", "russia", "russian", "moscow",
    "sabah", "malaysia", "borneo", "indonesia", "thailand", "vietnam",
    "uk businesses", "uk firms", "british", "england", "wales", "scotland",
    "italy", "italian", "france", "french", "germany", "german",
    "spain", "spanish", "portugal", "netherlands", "belgium", "denmark",
    "australia", "new zealand", "south africa", "nigeria",
    "japan", "japanese", "korea", "korean", "tokyo",
    "argentina", "chile", "peru", "venezuela", "colombia",
    "iran", "iraq", "syria", "yemen", "afghanistan",
}


def _gnews_score(title: str, source: str = "") -> int:
    """Score a headline by disruption relevance + US locality.
    Higher = more operationally relevant to a US ops director."""
    low = (title or "").lower()
    src_low = (source or "").lower()
    score = 0
    # Disruption signal
    for w in _GNEWS_BOOST:
        if w in low:
            score += 2
    # BizDev / explainer / aerospace noise
    for w in _GNEWS_DEMOTE:
        if w in low:
            score -= 3
    # US locality boost
    for w in _GNEWS_US_BOOST:
        if w in low or w in src_low:
            score += 1
            break  # one boost is enough; don't double-count if multiple states
    # Foreign locality demote
    for w in _GNEWS_FOREIGN_DEMOTE:
        if w in low or w in src_low:
            score -= 2
    # Non-ASCII title (Cyrillic, heavy non-English accents) → almost
    # certainly not an English-language article → hard demote.
    non_ascii = sum(1 for c in title if ord(c) > 127)
    if non_ascii > 3:
        score -= 4
    return score


def _gnews_dedupe_key(title: str) -> str:
    """Normalize to dedupe near-identical headlines from multiple outlets."""
    low = (title or "").lower()
    # Strip the trailing "— Publisher" Google News attaches
    if " - " in low:
        low = low.rsplit(" - ", 1)[0]
    elif " — " in low:
        low = low.rsplit(" — ", 1)[0]
    # Collapse whitespace and take the first 60 chars
    return " ".join(low.split())[:60]


@tool
def fetch_google_news_supply_chain() -> str:
    """Pull recent operational supply-chain disruption headlines from Google News RSS.

    Query strategy: 20 disruption-specific phrases (port strike, warehouse fire,
    rail derailment, carrier delays, etc.) restricted to the last 7 days, US
    locale. Drops the generic "supply chain" term which used to drown the
    results in leadership announcements and earnings releases.

    Post-fetch ranking:
      - score each headline by disruption keywords (strike, fire, delay,
        outage, closure, derailment, halt, suspended, backlog, etc.)
      - demote BizDev noise (CEO appointment, earnings, partnership, etc.)
      - dedupe near-identical headlines from multiple outlets

    Returns the top 12 by relevance, with score visible so the operator can
    judge how operationally-relevant each headline is.
    """
    from urllib.parse import quote
    from email.utils import parsedate_to_datetime
    # `tbs=qdr:w` is the Google "past week" filter — works on the RSS endpoint
    # where `when:7d` inside the query string is silently dropped.
    all_items: list[dict[str, Any]] = []
    total_raw = 0
    for query in _GNEWS_QUERIES:
        url = (
            "https://news.google.com/rss/search"
            f"?q={quote(query)}&hl=en-US&gl=US&ceid=US:en&tbs=qdr:w"
        )
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=DEFAULT_TIMEOUT)
            r.raise_for_status()
            pool = _parse_google_news_rss(r.text)
            total_raw += len(pool)
            all_items.extend(pool)
        except Exception as e:
            logger.warning("Google News pool fetch failed: %s", e)
    try:
        if all_items:
            # Date filter — last 21 days. Google's RSS bias toward popular
            # historical articles means strict 7d drops 95%+ of results; 21d
            # catches genuinely-ongoing events even when the headline first
            # broke 2-3 weeks ago (port strikes drag on, recalls expand, etc.)
            # The boost/demote scoring still ranks fresh items higher within
            # the window (secondary sort key is _dt desc).
            cutoff = datetime.now(timezone.utc) - timedelta(days=21)
            fresh: list[dict[str, Any]] = []
            for it in all_items:
                pd = it.get("pub_date") or ""
                try:
                    dt = parsedate_to_datetime(pd)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt >= cutoff:
                        it["_dt"] = dt
                        fresh.append(it)
                except Exception:
                    # Unparseable date — exclude (we'd rather miss a story
                    # than include something potentially years old).
                    pass
            # Dedupe by normalized title (the 3 pools overlap heavily on
            # macro stories like a major port strike — keep one copy).
            seen: set[str] = set()
            uniq: list[dict[str, Any]] = []
            for it in fresh:
                key = _gnews_dedupe_key(it.get("title") or "")
                if key in seen:
                    continue
                seen.add(key)
                it["_score"] = _gnews_score(it.get("title") or "", it.get("source") or "")
                uniq.append(it)
            # Sort by score desc, then by recency.
            uniq.sort(
                key=lambda x: (
                    x.get("_score", 0),
                    x.get("_dt") or datetime.min.replace(tzinfo=timezone.utc),
                ),
                reverse=True,
            )
            # Drop items with strongly-negative score (BizDev / leadership /
            # explainer / aerospace noise). Keep score >= -1 so a single
            # demoter word doesn't kill an otherwise-relevant story.
            relevant = [x for x in uniq if x.get("_score", 0) >= -1]
            top = relevant[:15]
            lines = [LIVE_BANNER, f"source: Google News RSS  fetched_at: {_now_iso()}"]
            lines.append(
                f"supply-chain disruption stories (last 21d, US, 4 query pools): "
                f"{len(relevant)} relevant of {len(uniq)} unique "
                f"({total_raw} raw across pools, showing top {len(top)})"
            )
            for it in top:
                title = (it.get("title") or "")[:140]
                src = it.get("source") or "Unknown"
                pub = (it.get("pub_date") or "")[:25]
                sc = it.get("_score", 0)
                score_tag = f"+{sc}" if sc > 0 else str(sc)
                lines.append(f"- [{score_tag:>3s}] [{src}] {pub}  \"{title}\"")
            if relevant:
                return "\n".join(lines)
    except Exception as e:
        logger.warning("Google News post-processing failed: %s — using synthetic", e)

    synth = [
        {"title": "FedEx warns of weekend delays at Memphis hub as severe storms approach",
         "source": "Reuters", "pub_date": _hours_ago_iso(1)},
        {"title": "UPS Teamsters authorize strike vote at Louisville sort facility",
         "source": "WSJ", "pub_date": _hours_ago_iso(4)},
        {"title": "Maersk reroutes Asia-Europe service amid Red Sea security flare-up",
         "source": "Bloomberg", "pub_date": _hours_ago_iso(6)},
        {"title": "Port of Long Beach reports congestion easing after 5-day backlog",
         "source": "Journal of Commerce", "pub_date": _hours_ago_iso(9)},
        {"title": "Norfolk Southern derailment closes I-81 corridor freight lane in Virginia",
         "source": "FreightWaves", "pub_date": _hours_ago_iso(12)},
    ]
    lines = [SYNTH_BANNER, f"source: Google News RSS (synthetic fallback)  fetched_at: {_now_iso()}"]
    lines.append(
        f"supply-chain disruption stories (synthetic): {len(synth)} relevant"
    )
    for it in synth:
        sc = _gnews_score(it["title"])
        score_tag = f"+{sc}" if sc > 0 else str(sc)
        lines.append(f"- [{score_tag:>3s}] [{it['source']}] {it['pub_date']}  \"{it['title']}\"")
    return "\n".join(lines)


# Convenience export list for binding into the LangGraph agent
EXTRA_TOOLS = [
    fetch_usgs_earthquakes,
    fetch_fema_disasters,
    fetch_nasa_eonet,
    fetch_reddit_supply_chain_pulse,
    fetch_google_news_supply_chain,
]


if __name__ == "__main__":
    # Quick local smoke test
    logging.basicConfig(level=logging.INFO)
    for fn in EXTRA_TOOLS:
        print("===", fn.name, "===")
        print(fn.invoke({}))
        print()
