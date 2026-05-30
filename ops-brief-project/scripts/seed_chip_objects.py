#!/usr/bin/env python3
"""Seed dedicated lookup tables that back the Composer's [OBJ:...] chips.

The Composer brief renders entity chips like `[OBJ:FC-HOU]`, `[OBJ:CAR-FDX]`,
`[OBJ:CUSTOMER-SMB-TIER]`. Clicking a chip in the UI hits
`GET /objects/lookup?id=<id>` -> `investigation_tools.lookup_object(id)`,
which reads from the four `chip_*` tables this script creates.

Why dedicated `chip_*` tables (instead of joining the live operational tables)?
  - Live operational tables (`fulfillment_centers`, `carriers`, ...) have
    schemas tuned for the analytics pipeline. They lack the human-readable
    fields a chip card needs (current_throughput, status flag, notes blob).
  - Chip resolution must be fast, deterministic, and never fail because of
    upstream schema drift in the analytics layer.
  - Mixing chip-display metadata into operational tables would tightly
    couple two independent consumers (analyst pipeline + UI drill-down).

Tables created (all in `dnb_hackathon_west_2.ops_brief`):
  - chip_fcs               (id, city, state, region, capacity_units_per_day,
                            current_throughput, status, notes)
  - chip_carriers          (id, name, mode, tier, on_time_rate_30d,
                            current_incidents_count, notes)
  - chip_customer_tiers    (id, tier_name, account_count, total_in_flight,
                            at_risk_revenue_usd, sla_target_hours, notes)
  - chip_geographies       (id, kind, label, state, region, notes)  -- covers
                            both GEO:* and LANE:* style IDs the brief mentions

All metadata is synthetic. This is a multi-tenant hackathon workspace -- no
real company data and no real company name in table comments / descriptions.

Usage:
    DATABRICKS_CONFIG_PROFILE=dnb-hackathon \
        uv run python ops-brief-project/scripts/seed_chip_objects.py

Idempotent. Safe to re-run -- MERGE INTO refreshes rows by primary key.
"""
from __future__ import annotations

import configparser
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("seed_chip_objects")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")

CATALOG = "dnb_hackathon_west_2"
SCHEMA = "ops_brief"
WAREHOUSE_ID = "110c24e02b899ae4"


# ----------------------------------------------------------------------------
# Auth -- reuse the same ~/.databrickscfg profile as setup_briefs_archive.py
# ----------------------------------------------------------------------------
def load_profile(profile_name: str) -> dict[str, str]:
    cfg_path = Path.home() / ".databrickscfg"
    if not cfg_path.exists():
        raise SystemExit(f"~/.databrickscfg not found at {cfg_path}")
    parser = configparser.ConfigParser()
    parser.read(cfg_path)
    if profile_name not in parser:
        raise SystemExit(f"Profile [{profile_name}] missing. Available: {parser.sections()}")
    s = parser[profile_name]
    host = s.get("host", "").rstrip("/")
    token = s.get("token", "")
    if not host or not token:
        raise SystemExit(f"Profile [{profile_name}] missing host/token")
    return {"host": host, "token": token}


def make_sql_connection(host: str, token: str, warehouse_id: str):
    from databricks import sql as dbsql
    server_hostname = host.replace("https://", "").replace("http://", "")
    return dbsql.connect(
        server_hostname=server_hostname,
        http_path=f"/sql/1.0/warehouses/{warehouse_id}",
        access_token=token,
    )


# ----------------------------------------------------------------------------
# Synthetic data -- realistic logistics-flavored values, no real company names
# ----------------------------------------------------------------------------
def build_fcs() -> list[dict[str, Any]]:
    """11 fulfillment centers covering existing + new IDs the Composer chips."""
    return [
        # id        city            state region    cap   thr  status        notes
        ("FC-LAX", "Los Angeles",   "CA", "West",    12000, 9800, "operational", "West Coast intake hub; primary import gateway from Port of LA / Long Beach."),
        ("FC-HOU", "Houston",       "TX", "South",    9500, 7200, "operational", "Gulf Coast distribution; secondary path for Port of Houston imports."),
        ("FC-DFW", "Dallas",        "TX", "South",   11000, 9100, "operational", "Central US fan-out; rail and air links to MEM, ORD, PHX."),
        ("FC-ANC", "Anchorage",     "AK", "West",     2400, 1850, "degraded",    "Aleutian + AK serving facility; weather-driven capacity swings common."),
        ("FC-MEM", "Memphis",       "TN", "South",   14000,12100, "operational", "Co-located with the dominant air-cargo hub; storm-exposed."),
        ("FC-IND", "Indianapolis",  "IN", "Central",  8800, 7400, "operational", "Central US ground hub; FedEx Ground SmartPost crossdock."),
        ("FC-ATL", "Atlanta",       "GA", "South",   10000, 8600, "operational", "Southeast distribution; UPS Worldport-adjacent ground network."),
        ("FC-EWR", "Newark",        "NJ", "Northeast",11000, 9200, "operational", "Northeast metro; marine-layer fog occasionally delays morning departs."),
        ("FC-ORD", "Chicago",       "IL", "Central",  10000, 8400, "operational", "Central US air + ground; winter storm exposure Nov-Mar."),
        ("FC-PHX", "Phoenix",       "AZ", "West",      7000, 5900, "operational", "Southwest desert hub; OnTrac last-mile partner."),
        ("FC-OAK", "Oakland",       "CA", "West",      9000, 7500, "operational", "Bay Area + NorCal; Port of Oakland alternate intake when LA pier dwell spikes."),
    ]


def build_carriers() -> list[dict[str, Any]]:
    """7 carriers covering ground, air, and ocean."""
    return [
        # id          name           mode      tier         otd30   incidents notes
        ("CAR-FDX",   "FedEx",       "ground", "tier1",     0.942,  2, "Largest US small-parcel ground + air partner."),
        ("CAR-UPS",   "UPS",         "ground", "tier1",     0.935,  1, "Dominant Worldport SDF hub; commercial ground network."),
        ("CAR-USPS",  "USPS",        "ground", "tier2",     0.881,  3, "Last-mile reach; volume-spike sensitivity around holidays."),
        ("CAR-DHL",   "DHL",         "air",    "tier2",     0.951,  0, "International express; CVG hub."),
        ("CAR-OTC",   "OnTrac",      "ground", "tier2",     0.908,  2, "Regional West Coast last-mile; PHX/LAX depot occasional dwell."),
        ("CAR-HMM",   "HMM",         "ocean",  "tier1",     0.872,  1, "Korean ocean container line; Trans-Pacific service to West Coast."),
        ("CAR-MAERSK","Maersk",      "ocean",  "tier1",     0.889,  0, "Largest global ocean container line; Trans-Pacific + Suez routings."),
    ]


def build_customer_tiers() -> list[dict[str, Any]]:
    """3 customer tier rollups."""
    return [
        # id                            tier_name      accts  inflight at_risk_rev  sla_hrs notes
        ("CUSTOMER-SMB-TIER",           "SMB",          2026,    1820,    284100.0, 72,    "Small/medium e-commerce merchants; tier-specific 72h B2C SLA."),
        ("CUSTOMER-ENTERPRISE-TIER",    "Enterprise",    375,    1340,    742000.0, 48,    "Enterprise contracts with stricter 48h SLA and dedicated CSM coverage."),
        ("CUSTOMER-CONSUMER-TIER",      "Consumer",     5599,    1432,     61400.0, 96,    "Direct-to-consumer accounts; standard 96h delivery promise."),
    ]


def build_geographies() -> list[dict[str, Any]]:
    """Minimal stubs for GEO:* and LANE:* IDs the Composer occasionally emits."""
    return [
        # id                              kind     label                                state    region    notes
        ("GEO:TX",                        "geo",   "Texas",                             "TX",    "South",   "State-level rollup; covers FC-HOU + FC-DFW catchments."),
        ("GEO:CA",                        "geo",   "California",                        "CA",    "West",    "State-level rollup; covers FC-LAX + FC-OAK catchments."),
        ("GEO:GREAT-SITKIN-AK",           "geo",   "Great Sitkin, AK",                  "AK",    "West",    "Remote Aleutian Islands locality; serviced via FC-ANC + air."),
        ("LANE:BAB-EL-MANDEB",            "lane",  "Bab-el-Mandeb strait",              None,    None,      "Critical ocean chokepoint feeding Suez transits; HMM + Maersk routings."),
        ("LANE:ALEUTIAN-CORRIDOR",        "lane",  "Aleutian Corridor",                 None,    "West",    "Trans-Pacific great-circle air corridor; FC-ANC tech-stop fuel route."),
    ]


# ----------------------------------------------------------------------------
# DDL + MERGE
# ----------------------------------------------------------------------------
def lit_str(s: str | None) -> str:
    if s is None:
        return "NULL"
    return "'" + s.replace("'", "''") + "'"


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")


def upsert_fcs(conn, rows: list[tuple]) -> int:
    fqn = f"{CATALOG}.{SCHEMA}.chip_fcs"
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {fqn} (
                id                      STRING,
                city                    STRING,
                state                   STRING,
                region                  STRING,
                capacity_units_per_day  INT,
                current_throughput      INT,
                status                  STRING,
                notes                   STRING
            ) USING DELTA
        """)
        values = ",\n".join(
            f"({lit_str(r[0])}, {lit_str(r[1])}, {lit_str(r[2])}, {lit_str(r[3])}, "
            f"{int(r[4])}, {int(r[5])}, {lit_str(r[6])}, {lit_str(r[7])})"
            for r in rows
        )
        cur.execute(f"""
            MERGE INTO {fqn} t
            USING (
              SELECT * FROM VALUES
              {values}
              AS s(id, city, state, region, capacity_units_per_day,
                   current_throughput, status, notes)
            ) s
            ON t.id = s.id
            WHEN MATCHED THEN UPDATE SET *
            WHEN NOT MATCHED THEN INSERT *
        """)
        cur.execute(f"SELECT COUNT(*) FROM {fqn}")
        count = cur.fetchone()[0]
    logger.info("chip_fcs: %d rows", count)
    return count


def upsert_carriers(conn, rows: list[tuple]) -> int:
    fqn = f"{CATALOG}.{SCHEMA}.chip_carriers"
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {fqn} (
                id                       STRING,
                name                     STRING,
                mode                     STRING,
                tier                     STRING,
                on_time_rate_30d         DOUBLE,
                current_incidents_count  INT,
                notes                    STRING
            ) USING DELTA
        """)
        values = ",\n".join(
            f"({lit_str(r[0])}, {lit_str(r[1])}, {lit_str(r[2])}, {lit_str(r[3])}, "
            f"CAST({float(r[4])} AS DOUBLE), {int(r[5])}, {lit_str(r[6])})"
            for r in rows
        )
        cur.execute(f"""
            MERGE INTO {fqn} t
            USING (
              SELECT * FROM VALUES
              {values}
              AS s(id, name, mode, tier, on_time_rate_30d,
                   current_incidents_count, notes)
            ) s
            ON t.id = s.id
            WHEN MATCHED THEN UPDATE SET *
            WHEN NOT MATCHED THEN INSERT *
        """)
        cur.execute(f"SELECT COUNT(*) FROM {fqn}")
        count = cur.fetchone()[0]
    logger.info("chip_carriers: %d rows", count)
    return count


def upsert_customer_tiers(conn, rows: list[tuple]) -> int:
    fqn = f"{CATALOG}.{SCHEMA}.chip_customer_tiers"
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {fqn} (
                id                    STRING,
                tier_name             STRING,
                account_count         INT,
                total_in_flight       INT,
                at_risk_revenue_usd   DOUBLE,
                sla_target_hours      INT,
                notes                 STRING
            ) USING DELTA
        """)
        values = ",\n".join(
            f"({lit_str(r[0])}, {lit_str(r[1])}, {int(r[2])}, {int(r[3])}, "
            f"CAST({float(r[4])} AS DOUBLE), {int(r[5])}, {lit_str(r[6])})"
            for r in rows
        )
        cur.execute(f"""
            MERGE INTO {fqn} t
            USING (
              SELECT * FROM VALUES
              {values}
              AS s(id, tier_name, account_count, total_in_flight,
                   at_risk_revenue_usd, sla_target_hours, notes)
            ) s
            ON t.id = s.id
            WHEN MATCHED THEN UPDATE SET *
            WHEN NOT MATCHED THEN INSERT *
        """)
        cur.execute(f"SELECT COUNT(*) FROM {fqn}")
        count = cur.fetchone()[0]
    logger.info("chip_customer_tiers: %d rows", count)
    return count


def upsert_geographies(conn, rows: list[tuple]) -> int:
    fqn = f"{CATALOG}.{SCHEMA}.chip_geographies"
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {fqn} (
                id      STRING,
                kind    STRING,
                label   STRING,
                state   STRING,
                region  STRING,
                notes   STRING
            ) USING DELTA
        """)
        values = ",\n".join(
            f"({lit_str(r[0])}, {lit_str(r[1])}, {lit_str(r[2])}, "
            f"{lit_str(r[3])}, {lit_str(r[4])}, {lit_str(r[5])})"
            for r in rows
        )
        cur.execute(f"""
            MERGE INTO {fqn} t
            USING (
              SELECT * FROM VALUES
              {values}
              AS s(id, kind, label, state, region, notes)
            ) s
            ON t.id = s.id
            WHEN MATCHED THEN UPDATE SET *
            WHEN NOT MATCHED THEN INSERT *
        """)
        cur.execute(f"SELECT COUNT(*) FROM {fqn}")
        count = cur.fetchone()[0]
    logger.info("chip_geographies: %d rows", count)
    return count


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main() -> int:
    profile = os.getenv("DATABRICKS_CONFIG_PROFILE", "dnb-hackathon")
    creds = load_profile(profile)
    os.environ["DATABRICKS_HOST"] = creds["host"]
    os.environ["DATABRICKS_TOKEN"] = creds["token"]
    logger.info("Profile=%s host=%s warehouse=%s", profile, creds["host"], WAREHOUSE_ID)

    conn = make_sql_connection(creds["host"], creds["token"], WAREHOUSE_ID)
    try:
        ensure_schema(conn)
        n_fcs = upsert_fcs(conn, build_fcs())
        n_car = upsert_carriers(conn, build_carriers())
        n_tier = upsert_customer_tiers(conn, build_customer_tiers())
        n_geo = upsert_geographies(conn, build_geographies())
    finally:
        conn.close()

    summary = {
        "chip_fcs_rows": n_fcs,
        "chip_carriers_rows": n_car,
        "chip_customer_tiers_rows": n_tier,
        "chip_geographies_rows": n_geo,
    }
    print("\n=== seed_chip_objects summary ===")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
