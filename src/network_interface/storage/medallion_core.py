"""
medallion_core.py — Shared Silver/Gold contract and transform logic.

Centralizes schemas and the single-record Silver transform so that:
- streaming_pipeline.py (local PyArrow) uses this module for SILVER_SCHEMA,
  GOLD_SCHEMA, silver_transform(), and port classification.
- databricks_pipeline.py (cloud Spark) can align on the same field semantics
  and optionally reference SILVER_FIELD_NAMES for DDL consistency.

No Spark or PyArrow dependency at import time for the transform; PyArrow
schemas are defined here for the local pipeline.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import pyarrow as pa

# ─────────────────────────────────────────────────────────────────────────────
# Silver Schema (PyArrow) — single source of truth for local Parquet
# ─────────────────────────────────────────────────────────────────────────────
SILVER_SCHEMA = pa.schema([
    ("event_ts", pa.timestamp("us", tz="UTC")),
    ("event_hour", pa.timestamp("us", tz="UTC")),
    ("event_date", pa.date32()),
    ("src_ip", pa.string()),
    ("dst_ip", pa.string()),
    pa.field("src_port", pa.int32(), nullable=True),
    pa.field("dst_port", pa.int32(), nullable=True),
    ("protocol", pa.string()),
    ("length", pa.int64()),
    pa.field("ttl", pa.int32(), nullable=True),
    pa.field("flags", pa.string(), nullable=True),
    pa.field("service_info", pa.string(), nullable=True),
    pa.field("port_category", pa.string(), nullable=True),
    ("agent_id", pa.string()),
    ("ingested_at", pa.timestamp("us", tz="UTC")),
    ("src_network", pa.string()),
])

GOLD_SCHEMA = pa.schema([
    ("event_hour", pa.timestamp("us", tz="UTC")),
    ("event_date", pa.date32()),
    ("protocol", pa.string()),
    ("src_network", pa.string()),
    ("agent_id", pa.string()),
    ("packet_count", pa.int64()),
    ("total_bytes", pa.int64()),
    ("avg_packet_size", pa.float64()),
    ("first_seen", pa.timestamp("us", tz="UTC")),
    ("last_seen", pa.timestamp("us", tz="UTC")),
    ("unique_src_ips", pa.int64()),
    ("unique_dst_ips", pa.int64()),
    ("unique_dst_ports", pa.int64()),
    ("computed_at", pa.timestamp("us", tz="UTC")),
])

SILVER_FIELD_NAMES = [f.name for f in SILVER_SCHEMA]
GOLD_FIELD_NAMES = [f.name for f in GOLD_SCHEMA]


# ─────────────────────────────────────────────────────────────────────────────
# Port classification — shared for analytics/UI and filtering
# ─────────────────────────────────────────────────────────────────────────────
class PortCategory(str):
    USER_APP = "user_app"
    WELL_KNOWN = "well_known"
    OS_NOISE = "os_noise"
    EPHEMERAL = "ephemeral"
    UNKNOWN = "unknown"


_OS_NOISE_PORTS: set[int] = {
    137, 138, 139,
    1900, 5353, 5355, 3702,
}
_WELL_KNOWN_PORTS: set[int] = {
    22, 53, 80, 443,
    110, 143, 993, 995,
    1433, 1723, 3306, 3389,
    5060, 5432, 5900, 6379,
    8080, 8443, 27017,
}
_EPHEMERAL_LOW: int = 49152


def classify_ports(src_port: Optional[int], dst_port: Optional[int]) -> str:
    """Best-effort port category for a flow. Used by Silver transform and UI filters."""
    if src_port is None and dst_port is None:
        return PortCategory.UNKNOWN
    ports = {p for p in (src_port, dst_port) if p is not None}
    if any(p in _OS_NOISE_PORTS for p in ports):
        return PortCategory.OS_NOISE
    if any(p in _WELL_KNOWN_PORTS for p in ports):
        return PortCategory.WELL_KNOWN
    if any(p >= _EPHEMERAL_LOW for p in ports):
        return PortCategory.EPHEMERAL
    return PortCategory.USER_APP


# ─────────────────────────────────────────────────────────────────────────────
# Silver Transform — O(1) per record; mirrors Spark _transform_to_silver semantics
# ─────────────────────────────────────────────────────────────────────────────
def silver_transform(
    record_dict: dict[str, Any],
    now: datetime | None = None,
) -> Optional[dict[str, Any]]:
    """
    Transform a single Bronze record dict into a Silver record dict.
    Returns None if the timestamp is unparseable (quality gate).
    """
    ts_raw = record_dict.get("timestamp")
    if not ts_raw:
        return None
    try:
        event_ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None

    if event_ts.tzinfo is None:
        event_ts = event_ts.replace(tzinfo=timezone.utc)

    event_hour = event_ts.replace(minute=0, second=0, microsecond=0)
    event_date = event_ts.date()

    src_ip: str = str(record_dict.get("src_ip", "0.0.0.0"))
    parts = src_ip.split(".")
    src_network = ".".join(parts[:3]) + ".0/24" if len(parts) == 4 else src_ip

    if now is None:
        now = datetime.now(timezone.utc)

    src_port = record_dict.get("src_port")
    dst_port = record_dict.get("dst_port")
    port_category = classify_ports(src_port, dst_port)

    return {
        "event_ts": event_ts,
        "event_hour": event_hour,
        "event_date": event_date,
        "src_ip": src_ip,
        "dst_ip": str(record_dict.get("dst_ip", "0.0.0.0")),
        "src_port": src_port,
        "dst_port": dst_port,
        "protocol": str(record_dict.get("protocol", "OTHER")).upper(),
        "length": int(record_dict.get("length", 0)),
        "ttl": record_dict.get("ttl"),
        "flags": record_dict.get("flags"),
        "service_info": record_dict.get("service_info"),
        "port_category": port_category,
        "agent_id": str(record_dict.get("agent_id", "")),
        "ingested_at": now,
        "src_network": src_network,
    }
