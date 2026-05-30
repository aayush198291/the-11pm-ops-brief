"""Object investigation / drill-down endpoint backing the UI's object chips.

When the Composer's brief contains `[OBJ:FC-HOU]`, `[OBJ:CAR-FDX]`,
`[OBJ:CUSTOMER-SMB-TIER]`, or `[OBJ:LANE:BAB-EL-MANDEB]`, clicking that chip
in the UI hits `/objects/lookup?id=<id>` and gets back a structured drill-down
JSON that the UI renders into the right rail.

Design notes:
  * Chip resolution reads dedicated `chip_*` tables seeded by
    `ops-brief-project/scripts/seed_chip_objects.py`. We do NOT join the live
    operational tables (`fulfillment_centers`, `carriers`, ...) here -- those
    have schemas tuned for the analytics pipeline and are subject to drift.
    Keeping the chip resolver on its own dedicated tables means chip click
    UX never regresses when the analytics schema changes upstream.
  * Unknown IDs return a friendly stub card instead of an error. A
    "not yet indexed" card is strictly better UX than a 404 -- the UI still
    has something to render.
  * The supervisor's COMPOSER_PROMPT restricts the Composer to a small set of
    well-known prefixes. This module is forgiving regardless: any unrecognised
    ID resolves to a stub.
  * Auth: prefers the Databricks SDK's WorkspaceClient + Statement Execution
    API (works with service-principal OAuth in deployed Databricks Apps);
    falls back to the dbsql connector + PAT for local dev. The previous
    PAT-only path silently failed in the deployed app and every chip showed
    "warehouse unavailable in this env".
"""
from __future__ import annotations

import configparser
import logging
import os
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)

CATALOG = os.getenv("OPS_BRIEF_UC_CATALOG", "dnb_hackathon_west_2")
SCHEMA = os.getenv("OPS_BRIEF_UC_SCHEMA", "ops_brief")
WAREHOUSE_ID = os.getenv("OPS_BRIEF_WAREHOUSE_ID", "110c24e02b899ae4")


# ────────────────────────────────────────────────────────────────────────────
# Query helper — SDK first (SP OAuth), then PAT fallback for local dev
# ────────────────────────────────────────────────────────────────────────────


def _profile_credentials() -> Optional[tuple[str, str]]:
    """Return (host, token) from the active Databricks CLI profile, or None."""
    profile = os.getenv("DATABRICKS_CONFIG_PROFILE")
    if not profile:
        return None
    cfg = configparser.ConfigParser()
    cfg.read(os.path.expanduser("~/.databrickscfg"))
    if profile not in cfg:
        return None
    section = cfg[profile]
    host = section.get("host", "").replace("https://", "").rstrip("/")
    token = section.get("token", "")
    return (host, token) if host and token else None


def _run_sql_sdk(sql_text: str, params: Optional[dict] = None) -> Optional[list[dict]]:
    """Execute via WorkspaceClient.statement_execution — works with SP OAuth in Apps."""
    try:
        from databricks.sdk import WorkspaceClient
        from databricks.sdk.service.sql import StatementParameterListItem, StatementState
    except ImportError as exc:
        logger.warning("investigation_tools: databricks-sdk not available: %s", exc)
        return None

    sdk_params = None
    if params:
        sdk_params = [
            StatementParameterListItem(name=k, value=("" if v is None else str(v)))
            for k, v in params.items()
        ]

    try:
        w = WorkspaceClient()
        resp = w.statement_execution.execute_statement(
            statement=sql_text,
            warehouse_id=WAREHOUSE_ID,
            wait_timeout="30s",
            parameters=sdk_params,
        )
        if not resp.status or resp.status.state != StatementState.SUCCEEDED:
            logger.warning(
                "investigation_tools: SDK statement non-SUCCEEDED: %s",
                resp.status.state if resp.status else "no-status",
            )
            return None
        cols = (
            [c.name for c in (resp.manifest.schema.columns or [])]
            if resp.manifest and resp.manifest.schema
            else []
        )
        rows = (resp.result.data_array if resp.result else []) or []
        return [dict(zip(cols, r)) for r in rows]
    except Exception as exc:
        logger.warning("investigation_tools: SDK SQL failed: %s", exc)
        return None


def _run_sql_pat(sql_text: str, params: Optional[dict] = None) -> Optional[list[dict]]:
    """Execute via dbsql connector + PAT — local-dev fallback."""
    creds = _profile_credentials()
    if creds is None:
        return None
    host, token = creds
    try:
        from databricks import sql as dbsql
    except ImportError:
        return None
    try:
        with dbsql.connect(
            server_hostname=host,
            http_path=f"/sql/1.0/warehouses/{WAREHOUSE_ID}",
            access_token=token,
        ) as conn, conn.cursor() as cur:
            cur.execute(sql_text, params or {})
            cols = [c[0] for c in cur.description] if cur.description else []
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        logger.warning("investigation_tools: PAT SQL failed: %s", exc)
        return None


# In the deployed Databricks App env, named-param syntax differs between the
# two query paths. The SDK Statement Execution API expects `:name` markers;
# the dbsql connector accepts `%(name)s`. We unify on `:name` and rewrite
# for the PAT path. (All call sites use the bind helper below.)


def _run_sql(sql_template: str, params: Optional[dict] = None) -> Optional[list[dict]]:
    """Execute SQL using whichever auth path is available.

    `sql_template` uses `:name` named-parameter markers. Returns list[dict],
    or None if neither auth path is usable / both error out.
    """
    rows = _run_sql_sdk(sql_template, params)
    if rows is not None:
        return rows
    # Rewrite :name → %(name)s for the dbsql connector's paramstyle.
    pat_sql = sql_template
    if params:
        for k in params:
            pat_sql = pat_sql.replace(f":{k}", f"%({k})s")
    return _run_sql_pat(pat_sql, params)


# ────────────────────────────────────────────────────────────────────────────
# Object-kind resolver
# ────────────────────────────────────────────────────────────────────────────


def _resolve_kind(object_id: str) -> str:
    """Map an ID to a known kind, or 'unknown'.

    Recognised prefixes (kept in sync with COMPOSER_PROMPT in supervisor.py):
      FC-*                       -> fulfillment_center
      CAR-* / CARRIER-*          -> carrier
      CUSTOMER-*-TIER            -> customer_tier
      CUST-* / CUSTOMER-* (other)-> customer (legacy individual customer)
      SHIPMENT-* / SHIP-*        -> shipment
      DISR-* / DISRUPTION-*      -> disruption
      GEO:*                      -> geography
      LANE:*                     -> lane
    """
    upper = object_id.upper()
    if upper.startswith("FC-"):
        return "fulfillment_center"
    if upper.startswith("CAR-") or upper.startswith("CARRIER-"):
        return "carrier"
    if upper.startswith("CUSTOMER-") and upper.endswith("-TIER"):
        return "customer_tier"
    if upper.startswith("CUST-") or upper.startswith("CUSTOMER-"):
        return "customer"
    if upper.startswith("SHIPMENT-") or upper.startswith("SHIP-"):
        return "shipment"
    if upper.startswith("DISR-") or upper.startswith("DISRUPTION-"):
        return "disruption"
    if upper.startswith("GEO:") or upper.startswith("LANE:"):
        return "geography"
    return "unknown"


def lookup_object(object_id: str) -> dict:
    """Return a Foundry-style object card for the given id.

    Always returns a renderable dict -- even for unknown IDs, missing rows,
    or warehouse failures. The UI's renderInvestigationCard never sees an
    empty payload from this endpoint.
    """
    object_id = (object_id or "").strip()
    kind = _resolve_kind(object_id)
    base = {
        "id": object_id,
        "kind": kind,
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    if not object_id:
        return {**base, **_unknown_stub(object_id, reason="empty id")}

    fetcher_by_kind: dict[str, Callable[[str], dict]] = {
        "fulfillment_center": _fetch_fc,
        "carrier": _fetch_carrier,
        "customer_tier": _fetch_customer_tier,
        "geography": _fetch_geography,
        "customer": _fetch_customer,
        "shipment": _fetch_shipment,
        "disruption": _fetch_disruption,
    }
    fetcher = fetcher_by_kind.get(kind)
    if fetcher is None:
        return {**base, **_unknown_stub(object_id, reason="unrecognized id prefix")}

    try:
        return {**base, **fetcher(object_id)}
    except Exception as exc:
        logger.warning("Object lookup failed for %s: %s", object_id, exc)
        return {**base, **_unknown_stub(object_id, reason=f"lookup error: {exc!s}")}


# ────────────────────────────────────────────────────────────────────────────
# Per-kind fetchers — all read from chip_* tables (chip-display schema)
# ────────────────────────────────────────────────────────────────────────────


def _fetch_fc(fc_id: str) -> dict:
    rows = _run_sql(
        f"SELECT id, city, state, region, capacity_units_per_day, "
        f"current_throughput, status, notes "
        f"FROM {CATALOG}.{SCHEMA}.chip_fcs WHERE id = :id",
        {"id": fc_id},
    )
    if rows is None:
        return _unknown_stub(fc_id, reason="warehouse unreachable")
    if not rows:
        return _unknown_stub(fc_id, reason="FC not yet indexed")
    fc = rows[0]
    cap = _safe_int(fc.get("capacity_units_per_day"))
    thr = _safe_int(fc.get("current_throughput"))
    utilization = round(100.0 * thr / cap, 1) if cap else None
    return {
        "label": f"{fc['id']} — {fc.get('city','—')}, {fc.get('state','—')}",
        "properties": {
            "FC ID": fc["id"],
            "City": fc.get("city") or "—",
            "State": fc.get("state") or "—",
            "Region": fc.get("region") or "—",
            "Capacity (units/day)": f"{cap:,}" if cap else "—",
            "Current throughput": f"{thr:,}",
            "Utilization": f"{utilization}%" if utilization is not None else "—",
            "Status": fc.get("status") or "—",
            "Notes": fc.get("notes") or "",
        },
        "links": {},
    }


def _fetch_carrier(raw_id: str) -> dict:
    # Accept both CAR-FDX (new canonical) and CARRIER-FEDEX (legacy chip text).
    upper = raw_id.upper()
    if upper.startswith("CARRIER-"):
        frag = upper.replace("CARRIER-", "", 1).split("-")[0]
        rows = _run_sql(
            f"SELECT id, name, mode, tier, on_time_rate_30d, "
            f"current_incidents_count, notes "
            f"FROM {CATALOG}.{SCHEMA}.chip_carriers "
            f"WHERE UPPER(name) LIKE :k OR UPPER(id) LIKE :k LIMIT 1",
            {"k": f"%{frag}%"},
        )
    else:
        rows = _run_sql(
            f"SELECT id, name, mode, tier, on_time_rate_30d, "
            f"current_incidents_count, notes "
            f"FROM {CATALOG}.{SCHEMA}.chip_carriers WHERE id = :id",
            {"id": raw_id},
        )
    if rows is None:
        return _unknown_stub(raw_id, reason="warehouse unreachable")
    if not rows:
        return _unknown_stub(raw_id, reason="carrier not yet indexed")
    c = rows[0]
    on_time = _safe_float(c.get("on_time_rate_30d"))
    return {
        "label": f"{c['id']} — {c.get('name','—')}",
        "properties": {
            "Carrier ID": c["id"],
            "Name": c.get("name") or "—",
            "Mode": c.get("mode") or "—",
            "Tier": c.get("tier") or "—",
            "On-time rate (30d)": f"{on_time * 100:.1f}%" if on_time is not None else "—",
            "Current incidents": c.get("current_incidents_count") if c.get("current_incidents_count") is not None else "—",
            "Notes": c.get("notes") or "",
        },
        "links": {},
    }


def _fetch_customer_tier(raw_id: str) -> dict:
    rows = _run_sql(
        f"SELECT id, tier_name, account_count, total_in_flight, "
        f"at_risk_revenue_usd, sla_target_hours, notes "
        f"FROM {CATALOG}.{SCHEMA}.chip_customer_tiers WHERE id = :id",
        {"id": raw_id},
    )
    if rows is None:
        return _unknown_stub(raw_id, reason="warehouse unreachable")
    if not rows:
        return _unknown_stub(raw_id, reason="tier not yet indexed")
    t = rows[0]
    accounts = _safe_int(t.get("account_count"))
    in_flight = _safe_int(t.get("total_in_flight"))
    return {
        "label": f"{t.get('tier_name','—')} tier",
        "properties": {
            "Tier": t.get("tier_name") or "—",
            "Tier ID": t["id"],
            "Accounts": f"{accounts:,}",
            "In-flight shipments": f"{in_flight:,}",
            "Revenue at risk": f"${_safe_float(t.get('at_risk_revenue_usd')) or 0:,.0f}",
            "SLA target": f"{t.get('sla_target_hours','—')}h",
            "Notes": t.get("notes") or "",
        },
        "links": {},
    }


def _fetch_geography(raw_id: str) -> dict:
    rows = _run_sql(
        f"SELECT id, kind, label, state, region, notes "
        f"FROM {CATALOG}.{SCHEMA}.chip_geographies WHERE id = :id",
        {"id": raw_id},
    )
    if rows is None:
        return _unknown_stub(raw_id, reason="warehouse unreachable")
    if not rows:
        return _unknown_stub(raw_id, reason="geography not yet indexed")
    g = rows[0]
    return {
        "label": g.get("label") or g["id"],
        "properties": {
            "ID": g["id"],
            "Kind": g.get("kind") or "—",
            "Label": g.get("label") or "",
            "State": g.get("state") or "—",
            "Region": g.get("region") or "—",
            "Notes": g.get("notes") or "",
        },
        "links": {},
    }


# ────────────────────────────────────────────────────────────────────────────
# Per-kind fetchers — still backed by live operational tables (kept for
# `[OBJ:CUST-1234]`, `[OBJ:SHIPMENT-...]`, `[OBJ:DISR-...]` drill-downs that
# don't have a corresponding chip_* table.)
# ────────────────────────────────────────────────────────────────────────────


def _fetch_customer(raw_id: str) -> dict:
    key = raw_id.replace("CUSTOMER-", "", 1).replace("CUST-", "", 1)
    rows = _run_sql(
        f"SELECT customer_id, customer_tier, state, region, lifetime_value "
        f"FROM {CATALOG}.{SCHEMA}.customers "
        f"WHERE customer_id = :id LIMIT 1",
        {"id": key},
    )
    if rows is None:
        return _unknown_stub(raw_id, reason="warehouse unreachable")
    if not rows:
        return _unknown_stub(raw_id, reason="customer not found")
    c = rows[0]
    return {
        "label": f"Customer {c['customer_id']}",
        "properties": {
            "Customer ID": c["customer_id"],
            "Tier": c.get("customer_tier") or "—",
            "State": c.get("state") or "—",
            "Region": c.get("region") or "—",
            "Lifetime value": f"${_safe_float(c.get('lifetime_value')) or 0:,.0f}",
        },
        "links": {},
    }


def _fetch_shipment(raw_id: str) -> dict:
    sid = raw_id.replace("SHIPMENT-", "", 1).replace("SHIP-", "", 1)
    rows = _run_sql(
        f"SELECT shipment_id, status, fc_id, dest_state, carrier_id, lane, "
        f"promised_eta_ts, delay_hours, is_at_risk "
        f"FROM {CATALOG}.{SCHEMA}.shipments WHERE shipment_id = :id LIMIT 1",
        {"id": sid},
    )
    if rows is None:
        return _unknown_stub(raw_id, reason="warehouse unreachable")
    if not rows:
        return _unknown_stub(raw_id, reason="shipment not found")
    s = rows[0]
    return {
        "label": f"Shipment {str(s['shipment_id'])[:14]}…",
        "properties": {
            "Status": s.get("status") or "—",
            "Origin FC": s.get("fc_id") or "—",
            "Destination": s.get("dest_state") or "—",
            "Carrier": s.get("carrier_id") or "—",
            "Lane": s.get("lane") or "—",
            "Promised ETA": str(s.get("promised_eta_ts") or "—"),
            "Delay (hr)": s.get("delay_hours"),
            "At risk": s.get("is_at_risk"),
        },
        "links": {
            "FC": [{"id": s["fc_id"]}] if s.get("fc_id") else [],
            "Carrier": [{"id": s["carrier_id"]}] if s.get("carrier_id") else [],
        },
    }


def _fetch_disruption(raw_id: str) -> dict:
    did = raw_id.replace("DISRUPTION-", "", 1).replace("DISR-", "", 1)
    rows = _run_sql(
        f"SELECT event_id, event_type, severity, description, geography, "
        f"affected_carrier_id, start_ts, end_ts, source, source_url "
        f"FROM {CATALOG}.{SCHEMA}.disruption_events WHERE event_id = :id LIMIT 1",
        {"id": did},
    )
    if rows is None:
        return _unknown_stub(raw_id, reason="warehouse unreachable")
    if not rows:
        return _unknown_stub(raw_id, reason="disruption not found")
    d = rows[0]
    return {
        "label": f"{d.get('event_type','event')} disruption",
        "properties": {
            "Event ID": d["event_id"],
            "Type": d.get("event_type") or "—",
            "Severity": d.get("severity") or "—",
            "Geography": d.get("geography") or "—",
            "Started": str(d.get("start_ts") or "—"),
            "Ended": str(d.get("end_ts") or "ongoing"),
            "Source": d.get("source") or "—",
            "Description": d.get("description") or "",
        },
        "links": {
            "Carrier": (
                [{"id": d["affected_carrier_id"]}]
                if d.get("affected_carrier_id") else []
            ),
        },
    }


# ────────────────────────────────────────────────────────────────────────────
# Friendly stub for unknown IDs
# ────────────────────────────────────────────────────────────────────────────


def _unknown_stub(object_id: str, reason: str = "not yet indexed") -> dict:
    """Render-friendly placeholder so the UI never shows a broken chip.

    Returned shape matches the keys renderInvestigationCard expects:
    `label`, `properties`, `links`. Status property carries the reason so
    the UI can show a human-readable hint instead of an empty card.
    """
    return {
        "label": object_id or "(unknown)",
        "properties": {
            "status": f"unknown — {reason}",
        },
        "links": {},
    }


# ────────────────────────────────────────────────────────────────────────────
# Small parsers (the SDK returns everything as strings; the dbsql connector
# returns native types). Normalize so we don't crash on either path.
# ────────────────────────────────────────────────────────────────────────────


def _safe_int(v) -> int:
    try:
        if v is None or v == "":
            return 0
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _safe_float(v) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None
