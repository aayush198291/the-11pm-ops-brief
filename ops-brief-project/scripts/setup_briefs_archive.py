#!/usr/bin/env python3
"""Stage-4 (Memory + Tools via MCP/Vector Search) — briefs archive setup.

Creates:
  1. `dnb_hackathon_west_2.ops_brief.briefs_archive` — 14 synthetic past briefs
  2. Vector Search index `dnb_hackathon_west_2.ops_brief.briefs_archive_index`
     (Delta-Sync, embedding = databricks-gte-large-en)

Idempotent. Safe to re-run. If Vector Search endpoint provisioning fails or
takes too long, the Delta table is still created and the script emits a NOTE.

Usage:
    DATABRICKS_CONFIG_PROFILE=dnb-hackathon \
        uv run python ops-brief-project/scripts/setup_briefs_archive.py
"""
from __future__ import annotations

import configparser
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("setup_briefs_archive")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")

CATALOG = "dnb_hackathon_west_2"
SCHEMA = "ops_brief"
TABLE = "briefs_archive"
INDEX_NAME = f"{CATALOG}.{SCHEMA}.{TABLE}_index"
SOURCE_TABLE_FQN = f"{CATALOG}.{SCHEMA}.{TABLE}"
EMBEDDING_MODEL = "databricks-gte-large-en"
WAREHOUSE_ID = "110c24e02b899ae4"

PREFERRED_ENDPOINT_NAMES = ["dnb_hackathon", "ops_brief_vs"]
NEW_ENDPOINT_NAME = "ops_brief_vs"

# Polling
ENDPOINT_WAIT_SECS = 15 * 60   # 15 min
INDEX_WAIT_SECS = 10 * 60      # 10 min
POLL_INTERVAL_SECS = 15


# ----------------------------------------------------------------------------
# Auth — read the same ~/.databrickscfg profile used by the rest of the project
# ----------------------------------------------------------------------------
def load_profile(profile_name: str) -> dict[str, str]:
    """Read host + token out of ~/.databrickscfg [profile]."""
    cfg_path = Path.home() / ".databrickscfg"
    if not cfg_path.exists():
        raise SystemExit(f"~/.databrickscfg not found at {cfg_path}")
    parser = configparser.ConfigParser()
    parser.read(cfg_path)
    if profile_name not in parser:
        raise SystemExit(
            f"Profile [{profile_name}] not found in {cfg_path}. "
            f"Available: {parser.sections()}"
        )
    section = parser[profile_name]
    host = section.get("host", "").rstrip("/")
    token = section.get("token", "")
    if not host or not token:
        raise SystemExit(f"Profile [{profile_name}] missing host/token")
    return {"host": host, "token": token}


# ----------------------------------------------------------------------------
# Synthetic data — 14 past briefs, May 14–27 2026
# ----------------------------------------------------------------------------
def build_briefs() -> list[dict[str, Any]]:
    """14 synthetic briefs with intentionally recurring + one-off themes.

    Recurrences (so VS recall has something interesting to find):
        MEM weather        → 5/15, 5/16, 5/27   (3 of 14)
        Port of LA labor   → 5/18, 5/19, 5/20   (3 consecutive)
        OnTrac outage      → 5/22               (one-off)
    Plus assorted carrier / weather / news one-offs.

    Severity ranges 28–82 across the 14 entries.
    """
    rows: list[dict[str, Any]] = []

    def add(
        d: str,
        sev: int,
        headline: str,
        body: str,
        themes: list[str],
        in_flight: int,
        at_risk: int,
        rev: float,
    ) -> None:
        bid = f"BRF-{d.replace('-', '')}-{sev:02d}"
        rows.append({
            "brief_id": bid,
            "brief_date": d,
            "severity_score": sev,
            "headline": headline,
            "body_markdown": body,
            "themes": themes,
            "in_flight_count": in_flight,
            "at_risk_count": at_risk,
            "revenue_at_risk_usd": rev,
            # created_at is logically "the night the brief was sent" — use 23:00 UTC of that day
            "created_at": f"{d}T23:00:00+00:00",
        })

    # 5/14 — quiet
    add(
        "2026-05-14", 31,
        "Nominal night — minor EWR fog delays only",
        (
            "## Ops Brief — Wed May 14, 2026\n"
            "**Severity:** 31/100 (nominal)\n\n"
            "Quiet night across the network. **FC-EWR** reporting marine-layer fog with ~25-min "
            "morning departure delays on FedEx and UPS Newark hub. No revenue impact projected. "
            "**FC-LAX**, **FC-MEM**, **FC-DFW**, **FC-ATL** all green. No action required."
        ),
        ["EWR fog", "nominal"],
        4120, 38, 41200.0,
    )

    # 5/15 — MEM weather #1
    add(
        "2026-05-15", 58,
        "Memphis severe thunderstorms — FedEx World Hub at risk",
        (
            "## Ops Brief — Thu May 15, 2026\n"
            "**Severity:** 58/100 (elevated)\n\n"
            "NWS Severe Thunderstorm Watch over Shelby County, TN through 04Z. "
            "**FC-MEM** is co-located with the FedEx World Hub; expect ground-stop risk on "
            "FedEx Express overnight sort. 73 high-value B2B shipments routed through MEM "
            "tonight, ~$118K at risk. Recommended: pre-stage 25 priority parcels at **FC-DFW** "
            "as overflow. UPS Worldport (SDF) unaffected."
        ),
        ["MEM weather", "FedEx hub risk"],
        4380, 73, 118400.0,
    )

    # 5/16 — MEM weather #2 (recurring)
    add(
        "2026-05-16", 64,
        "Memphis weather — 2nd consecutive night; FedEx delayed sort",
        (
            "## Ops Brief — Fri May 16, 2026\n"
            "**Severity:** 64/100 (elevated)\n\n"
            "**RECURRING: 2nd consecutive night of MEM weather impact.** "
            "FedEx confirmed a 90-min delayed sort departure overnight from Memphis World Hub. "
            "**FC-MEM** outbound volume held; 92 B2B parcels with revenue ~$164K now at risk of "
            "missing 2-day SLA. Suggested actions: notify top 5 affected customers, expedite via "
            "**FC-DFW**/UPS Next Day Air for 30 most time-sensitive. Storms expected to clear by 06Z."
        ),
        ["MEM weather", "FedEx delayed sort", "SLA risk"],
        4510, 92, 164300.0,
    )

    # 5/17 — random one-off
    add(
        "2026-05-17", 42,
        "OnTrac last-mile delays in Phoenix metro",
        (
            "## Ops Brief — Sat May 17, 2026\n"
            "**Severity:** 42/100 (elevated)\n\n"
            "OnTrac reporting depot congestion at PHX-1; outbound last-mile from **FC-LAX** to "
            "Phoenix MSAs running 1 day late. 34 e-commerce parcels affected, ~$22K rev. "
            "Suggest swapping to LaserShip for next 48h on Phoenix ZIPs. Low broader-network impact."
        ),
        ["OnTrac PHX delays", "carrier swap"],
        3870, 34, 22100.0,
    )

    # 5/18 — Port of LA #1
    add(
        "2026-05-18", 71,
        "Port of LA labor unrest — ILWU informational picket at Pier 400",
        (
            "## Ops Brief — Sun May 18, 2026\n"
            "**Severity:** 71/100 (critical)\n\n"
            "ILWU launched an informational picket at Pier 400 (Port of Los Angeles) this evening "
            "ahead of contract negotiations Tuesday. **FC-LAX** drayage windows compressed; "
            "expect 18-24h container dwell increase. 41 import containers in transit, ~$310K of "
            "merchant inventory delayed. Carriers affected: primarily ocean container lines, "
            "with downstream impact to FedEx Freight and UPS Ground. Long Beach unaffected so far."
        ),
        ["Port of LA labor unrest", "drayage delay", "inbound at-risk"],
        4790, 41, 310400.0,
    )

    # 5/19 — Port of LA #2 (recurring)
    add(
        "2026-05-19", 78,
        "Port of LA labor — picket spreads to Pier 300; full slowdown",
        (
            "## Ops Brief — Mon May 19, 2026\n"
            "**Severity:** 78/100 (critical)\n\n"
            "**RECURRING: 2nd night of Port of LA labor unrest.** "
            "Informational picket spread to Pier 300; ILWU reported ~40% gang attendance on "
            "swing shift. **FC-LAX** intake throughput down 35%. 67 import containers now in "
            "queue ($482K merchant inventory). Recommended: divert 8 priority containers to "
            "**Port of Oakland → FC-LAX trucking**; notify affected merchants of 3-5 day delay; "
            "begin contingency planning for FC-LAX-to-FC-DFW B2B handoff if Tuesday talks fail."
        ),
        ["Port of LA labor unrest", "ILWU", "drayage delay"],
        4830, 67, 482100.0,
    )

    # 5/20 — Port of LA #3 (recurring)
    add(
        "2026-05-20", 82,
        "Port of LA labor — talks collapse; 72h work stoppage announced",
        (
            "## Ops Brief — Tue May 20, 2026\n"
            "**Severity:** 82/100 (critical)\n\n"
            "**RECURRING: 3rd consecutive night, Port of LA labor unrest.** "
            "ILWU declared a 72h work stoppage starting 06Z Wednesday after talks collapsed. "
            "Highest-severity brief of the week. **FC-LAX** intake will halt by Wednesday 12Z. "
            "104 import containers stranded ($740K merchant inventory). Recommended actions: "
            "(1) reroute 22 priority containers to Port of Oakland with truck handoff to FC-LAX, "
            "(2) issue customer notifications for all West Coast merchants, "
            "(3) escalate to CSM Lead and Ops Director for direct merchant calls."
        ),
        ["Port of LA labor unrest", "work stoppage", "West Coast disruption"],
        4920, 104, 740800.0,
    )

    # 5/21 — recovery
    add(
        "2026-05-21", 49,
        "Port of LA labor — tentative agreement; backlog clearing",
        (
            "## Ops Brief — Wed May 21, 2026\n"
            "**Severity:** 49/100 (elevated)\n\n"
            "Tentative agreement reached overnight; ILWU resumed full gangs at 18Z. "
            "Port of LA backlog of 104 containers being worked off, estimated 48h to clear. "
            "**FC-LAX** intake ramping back up. No new at-risk shipments tonight. "
            "Continuing to monitor; West Coast network stabilizing."
        ),
        ["Port of LA recovery", "backlog clearing"],
        4710, 28, 168000.0,
    )

    # 5/22 — OnTrac outage (one-off)
    add(
        "2026-05-22", 67,
        "OnTrac scan-system outage — ~6h tracking blackout",
        (
            "## Ops Brief — Thu May 22, 2026\n"
            "**Severity:** 67/100 (elevated)\n\n"
            "OnTrac confirmed a system-wide scan/tracking outage 18Z–24Z. ~2,100 parcels in OnTrac "
            "network from **FC-LAX**, **FC-ORD**, **FC-DFW** went un-scanned during the window. "
            "Customer-facing tracking pages will show stalled-but-not-late status overnight. "
            "Suggested: queue proactive customer notifications for the 320 highest-value parcels; "
            "evaluate USPS as overflow carrier for tomorrow. ~$98K revenue exposed to customer-"
            "support volume spike."
        ),
        ["OnTrac system outage", "tracking blackout"],
        4630, 78, 98200.0,
    )

    # 5/23 — random
    add(
        "2026-05-23", 38,
        "Atlanta thunderstorms — minor FC-ATL outbound delays",
        (
            "## Ops Brief — Fri May 23, 2026\n"
            "**Severity:** 38/100 (nominal)\n\n"
            "Scattered thunderstorms over Fulton County delayed UPS pickups at **FC-ATL** by "
            "~45 min. 18 parcels at risk of missing 2-day SLA, ~$11K. Network otherwise green. "
            "FedEx Memphis nominal. Port of LA fully recovered."
        ),
        ["ATL weather", "minor delay"],
        4280, 18, 11400.0,
    )

    # 5/24 — random
    add(
        "2026-05-24", 28,
        "Quiet night — full network green",
        (
            "## Ops Brief — Sat May 24, 2026\n"
            "**Severity:** 28/100 (nominal)\n\n"
            "All 6 FCs (LAX, MEM, DFW, ATL, ORD, EWR) green. No active weather alerts in any "
            "FC metro. All carriers (FedEx, UPS, USPS, OnTrac, LaserShip) reporting on-time "
            "service. No action required."
        ),
        ["nominal", "all green"],
        4020, 12, 7300.0,
    )

    # 5/25 — USPS volume spike
    add(
        "2026-05-25", 44,
        "USPS Memorial Day weekend volume spike — delivery slip risk",
        (
            "## Ops Brief — Sun May 25, 2026\n"
            "**Severity:** 44/100 (elevated)\n\n"
            "USPS reporting Memorial-Day-weekend volume 22% above forecast. **FC-ORD** and "
            "**FC-EWR** outbound USPS Ground Advantage parcels facing 1-day SLA slip risk. "
            "47 affected parcels, ~$28K. Recommended: shift premium-tier parcels to UPS SurePost "
            "for Tuesday cutoff."
        ),
        ["USPS volume spike", "Memorial Day"],
        4470, 47, 28100.0,
    )

    # 5/26 — random LaserShip
    add(
        "2026-05-26", 36,
        "LaserShip Northeast routing — minor reroute via Allentown",
        (
            "## Ops Brief — Mon May 26, 2026\n"
            "**Severity:** 36/100 (nominal)\n\n"
            "LaserShip rerouting NJ/NY metro deliveries through Allentown depot due to "
            "Edison-NJ facility maintenance. ~22 parcels from **FC-EWR** delayed half-day, ~$14K. "
            "No customer-facing impact projected; auto-tracking updates will catch it. "
            "All other carriers nominal."
        ),
        ["LaserShip reroute", "minor delay"],
        4180, 22, 14200.0,
    )

    # 5/27 — MEM weather #3 (recurring, 3 of 14)
    add(
        "2026-05-27", 69,
        "Memphis severe weather AGAIN — recurring storm pattern this month",
        (
            "## Ops Brief — Tue May 27, 2026\n"
            "**Severity:** 69/100 (elevated)\n\n"
            "**RECURRING: 3rd MEM weather event in 14 days** (after 5/15 and 5/16). "
            "NWS Severe Thunderstorm Warning over Shelby County, TN through 05Z. FedEx World "
            "Hub bracing for delayed sort. **FC-MEM** holding outbound; 88 B2B parcels at risk "
            "(~$142K rev). Suggested: per the 5/16 playbook, pre-stage overflow at **FC-DFW**, "
            "notify top customers, expedite 30 most time-sensitive via UPS Next Day Air. "
            "Pattern note: Memphis has been a repeat exposure point this month — consider "
            "permanent dual-route strategy for B2B routed through FC-MEM."
        ),
        ["MEM weather", "FedEx hub risk", "recurring pattern"],
        4690, 88, 142400.0,
    )

    return rows


# ----------------------------------------------------------------------------
# SQL helpers — use databricks.sql.connect (warehouse, token-based)
# ----------------------------------------------------------------------------
def make_sql_connection(host: str, token: str, warehouse_id: str):
    from databricks import sql as dbsql

    server_hostname = host.replace("https://", "").replace("http://", "")
    http_path = f"/sql/1.0/warehouses/{warehouse_id}"
    return dbsql.connect(
        server_hostname=server_hostname,
        http_path=http_path,
        access_token=token,
    )


def ensure_schema_and_table(conn) -> None:
    """Create the schema (if missing) + table (if missing). DDL is idempotent."""
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {SOURCE_TABLE_FQN} (
                brief_id            STRING,
                brief_date          DATE,
                severity_score      INT,
                headline            STRING,
                body_markdown       STRING,
                themes              ARRAY<STRING>,
                in_flight_count     INT,
                at_risk_count       INT,
                revenue_at_risk_usd DOUBLE,
                created_at          TIMESTAMP
            )
            USING DELTA
            TBLPROPERTIES (
                'delta.enableChangeDataFeed' = 'true'
            )
        """)
        # Required for Delta-Sync Vector Search indexes
        cur.execute(
            f"ALTER TABLE {SOURCE_TABLE_FQN} "
            "SET TBLPROPERTIES (delta.enableChangeDataFeed = true)"
        )
    logger.info("Schema + table ensured: %s", SOURCE_TABLE_FQN)


def upsert_briefs(conn, rows: list[dict[str, Any]]) -> int:
    """Idempotent MERGE — re-running this script just refreshes rows by brief_id."""
    with conn.cursor() as cur:
        # Build a VALUES clause; keep parameterization simple — these are synthetic.
        def lit_array(a: list[str]) -> str:
            esc = [s.replace("'", "''") for s in a]
            return "ARRAY(" + ", ".join(f"'{s}'" for s in esc) + ")"

        def lit_str(s: str) -> str:
            return "'" + s.replace("'", "''") + "'"

        union_sql = "\n  UNION ALL\n  ".join(
            f"SELECT {lit_str(r['brief_id'])} AS brief_id, "
            f"DATE'{r['brief_date']}' AS brief_date, "
            f"CAST({int(r['severity_score'])} AS INT) AS severity_score, "
            f"{lit_str(r['headline'])} AS headline, "
            f"{lit_str(r['body_markdown'])} AS body_markdown, "
            f"{lit_array(r['themes'])} AS themes, "
            f"CAST({int(r['in_flight_count'])} AS INT) AS in_flight_count, "
            f"CAST({int(r['at_risk_count'])} AS INT) AS at_risk_count, "
            f"CAST({float(r['revenue_at_risk_usd'])} AS DOUBLE) AS revenue_at_risk_usd, "
            f"TIMESTAMP'{r['created_at'].replace('+00:00', '')}' AS created_at"
            for r in rows
        )

        merge_sql = f"""
            MERGE INTO {SOURCE_TABLE_FQN} t
            USING (
              {union_sql}
            ) s
            ON t.brief_id = s.brief_id
            WHEN MATCHED THEN UPDATE SET *
            WHEN NOT MATCHED THEN INSERT *
        """
        cur.execute(merge_sql)

        cur.execute(f"SELECT COUNT(*) FROM {SOURCE_TABLE_FQN}")
        count = cur.fetchone()[0]
    logger.info("MERGE complete — %s now has %d rows", SOURCE_TABLE_FQN, count)
    return count


# ----------------------------------------------------------------------------
# Vector Search
# ----------------------------------------------------------------------------
def get_vector_search_client():
    """Instantiate the VectorSearchClient using the current Databricks auth."""
    from databricks.vector_search.client import VectorSearchClient
    return VectorSearchClient(disable_notice=True)


def pick_endpoint(vsc, host: str, token: str) -> tuple[str, str]:
    """Return (endpoint_name, state) — reuse a shared one if present, else create.

    Strategy:
      1. List endpoints and prefer any in PREFERRED_ENDPOINT_NAMES that already exists.
      2. If none found, create `ops_brief_vs` (STORAGE_OPTIMIZED).
      3. Poll until READY (up to ENDPOINT_WAIT_SECS). Return current state regardless.
    """
    try:
        endpoints = vsc.list_endpoints().get("endpoints", [])
    except Exception as exc:
        logger.error("list_endpoints failed: %s", exc)
        endpoints = []
    existing_by_name = {ep.get("name"): ep for ep in endpoints}
    logger.info("Existing VS endpoints: %s", list(existing_by_name.keys()))

    chosen = None
    for n in PREFERRED_ENDPOINT_NAMES:
        if n in existing_by_name:
            chosen = n
            logger.info("Reusing existing VS endpoint: %s", n)
            break

    if chosen is None:
        logger.info("Creating new VS endpoint: %s (STORAGE_OPTIMIZED)", NEW_ENDPOINT_NAME)
        try:
            vsc.create_endpoint(
                name=NEW_ENDPOINT_NAME,
                endpoint_type="STORAGE_OPTIMIZED",
            )
        except Exception as exc:
            # Race condition if someone else just created it — re-list.
            logger.warning("create_endpoint raised (%s); checking if it now exists", exc)
        chosen = NEW_ENDPOINT_NAME

    # Poll for READY
    state = poll_endpoint_state(vsc, chosen, ENDPOINT_WAIT_SECS)
    return chosen, state


def poll_endpoint_state(vsc, name: str, max_secs: int) -> str:
    deadline = time.time() + max_secs
    last = "UNKNOWN"
    while time.time() < deadline:
        try:
            ep = vsc.get_endpoint(name)
            last = ep.get("endpoint_status", {}).get("state") or ep.get("state") or "UNKNOWN"
        except Exception as exc:
            logger.warning("get_endpoint(%s) failed: %s", name, exc)
            last = "ERROR"
        logger.info("  endpoint=%s state=%s", name, last)
        if last in ("ONLINE", "READY"):
            return last
        if last in ("OFFLINE", "FAILED"):
            return last
        time.sleep(POLL_INTERVAL_SECS)
    logger.warning("Endpoint %s didn't reach READY within %ds (last=%s)", name, max_secs, last)
    return last


def ensure_index(vsc, endpoint_name: str) -> tuple[str, str]:
    """Create Delta-Sync VS index if missing; poll until ONLINE. Return (action, state)."""
    try:
        existing = vsc.list_indexes(endpoint_name).get("vector_indexes", [])
    except Exception as exc:
        logger.warning("list_indexes failed: %s", exc)
        existing = []

    names = [ix.get("name") for ix in existing]
    if INDEX_NAME in names:
        logger.info("Index %s already exists — skipping creation", INDEX_NAME)
        action = "exists"
    else:
        logger.info("Creating Delta-Sync index %s …", INDEX_NAME)
        try:
            vsc.create_delta_sync_index(
                endpoint_name=endpoint_name,
                source_table_name=SOURCE_TABLE_FQN,
                index_name=INDEX_NAME,
                pipeline_type="TRIGGERED",
                primary_key="brief_id",
                embedding_source_column="body_markdown",
                embedding_model_endpoint_name=EMBEDDING_MODEL,
            )
            action = "created"
        except Exception as exc:
            logger.error("create_delta_sync_index failed: %s", exc)
            return ("create_failed", "ERROR")

    state = poll_index_state(vsc, INDEX_NAME, INDEX_WAIT_SECS)

    # Kick a sync if it's online but empty / stale
    if state == "ONLINE":
        try:
            ix = vsc.get_index(endpoint_name=endpoint_name, index_name=INDEX_NAME)
            ix.sync()
            logger.info("Triggered sync on %s", INDEX_NAME)
        except Exception as exc:
            logger.warning("sync() raised: %s", exc)

    return (action, state)


def poll_index_state(vsc, index_name: str, max_secs: int) -> str:
    deadline = time.time() + max_secs
    last = "UNKNOWN"
    while time.time() < deadline:
        try:
            desc = vsc.get_index(index_name=index_name)
            status = desc.describe() if hasattr(desc, "describe") else {}
            last = (
                status.get("status", {}).get("detailed_state")
                or status.get("status", {}).get("ready", None)
                or "UNKNOWN"
            )
            ready_flag = status.get("status", {}).get("ready")
            if ready_flag is True:
                return "ONLINE"
        except Exception as exc:
            logger.warning("get_index(%s) failed: %s", index_name, exc)
            last = "ERROR"
        logger.info("  index=%s detailed_state=%s", index_name, last)
        if isinstance(last, str) and last.upper() in ("ONLINE", "READY"):
            return "ONLINE"
        if isinstance(last, str) and last.upper() in ("FAILED",):
            return last
        time.sleep(POLL_INTERVAL_SECS)
    logger.warning("Index %s didn't reach ONLINE within %ds (last=%s)", index_name, max_secs, last)
    return str(last)


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main() -> int:
    profile = os.getenv("DATABRICKS_CONFIG_PROFILE", "dnb-hackathon")
    creds = load_profile(profile)
    os.environ["DATABRICKS_HOST"] = creds["host"]
    os.environ["DATABRICKS_TOKEN"] = creds["token"]
    # Also set workspace-client compatible vars for the VS SDK
    os.environ.setdefault("DATABRICKS_CONFIG_PROFILE", profile)

    logger.info("Profile=%s host=%s warehouse=%s", profile, creds["host"], WAREHOUSE_ID)

    # ── Step 1: Delta table ──────────────────────────────────────────────────
    logger.info("─── Step 1: ensure Delta table %s ───", SOURCE_TABLE_FQN)
    conn = make_sql_connection(creds["host"], creds["token"], WAREHOUSE_ID)
    try:
        ensure_schema_and_table(conn)
        rows = build_briefs()
        row_count = upsert_briefs(conn, rows)
    finally:
        conn.close()

    # ── Step 2: Vector Search endpoint ───────────────────────────────────────
    logger.info("─── Step 2: ensure VS endpoint ───")
    endpoint_state = "SKIPPED"
    index_state = "SKIPPED"
    endpoint_name = ""
    try:
        vsc = get_vector_search_client()
        endpoint_name, endpoint_state = pick_endpoint(vsc, creds["host"], creds["token"])
        if endpoint_state not in ("ONLINE", "READY"):
            logger.warning(
                "NOTE: VS endpoint %s not ready (state=%s). Skipping index creation.",
                endpoint_name, endpoint_state,
            )
        else:
            logger.info("─── Step 3: ensure VS index ───")
            action, index_state = ensure_index(vsc, endpoint_name)
            logger.info("Index action=%s state=%s", action, index_state)
    except Exception as exc:
        logger.error("Vector Search setup failed: %s", exc, exc_info=True)
        logger.warning(
            "NOTE: Delta table is created and populated; VS index skipped due to error. "
            "The recall_similar_briefs tool will still work via Delta-SELECT fallback."
        )

    # ── Summary ──────────────────────────────────────────────────────────────
    summary = {
        "table": SOURCE_TABLE_FQN,
        "row_count": row_count,
        "vs_endpoint_name": endpoint_name,
        "vs_endpoint_state": endpoint_state,
        "vs_index_name": INDEX_NAME,
        "vs_index_state": index_state,
        "embedding_model": EMBEDDING_MODEL,
    }
    print("\n=== setup_briefs_archive summary ===")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
