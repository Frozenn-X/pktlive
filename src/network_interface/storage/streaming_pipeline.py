"""
streaming_pipeline.py — Pure-Python Streaming Medallion Pipeline (Sub-Second)

Replaces the batch PySpark local pipeline with inline Silver/Gold transforms
that run inside the DataSinkProcess, on the same record, in the same loop tick.

    Bronze (BronzeStore, NDJSON)    — persistence brute, inchangee
         |
         v
    silver_transform(record)       — O(1) per record, pure Python
         |
         ├── LiveSnapshot._live.json  — atomic JSON sidecar for dashboard (<1ms read)
         v
    SilverStore (buffer -> Parquet) — flush every 500 records or 0.5s
         |
         v
    GoldAccumulator (in-memory)    — flush every 1s
         |
         v
    GoldStore (Parquet)

No JVM, no Spark, no Delta.  PyArrow writes columnar Parquet directly.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from collections import Counter, deque
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pyarrow as pa
import pyarrow.parquet as pq

from .medallion_core import GOLD_SCHEMA, SILVER_SCHEMA, PortCategory, silver_transform

logger = logging.getLogger("streaming_pipeline")

# ─────────────────────────────────────────────────────────────────────────────
# Configuration — project root so capture and web share _live.json
# ─────────────────────────────────────────────────────────────────────────────
from .._paths import get_project_root

_PROJECT_ROOT = get_project_root()
SILVER_DIR: str = str(_PROJECT_ROOT / "silver")
GOLD_DIR: str = str(_PROJECT_ROOT / "gold")
LIVE_SNAPSHOT_PATH: str = str(_PROJECT_ROOT / "_live.json")

_SILVER_FLUSH_RECORDS: int = 500
_SILVER_FLUSH_SEC: float = 0.5
_GOLD_FLUSH_SEC: float = 1.0
_SNAPSHOT_FLUSH_SEC: float = 0.3


def _flush_to_partitioned_parquet(
    records: list[dict[str, Any]],
    root: Path,
    prefix: str,
    schema: pa.Schema,
) -> None:
    """
    Write a batch of row dicts to partitioned Parquet files under root.

    Partitions on event_date, creating directories like event_date=YYYY-MM-DD
    and files named {prefix}_<epoch>_<chunk>.parquet.
    """
    if not records:
        return

    partitions: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        key = str(rec["event_date"])
        partitions.setdefault(key, []).append(rec)

    for date_key, part_records in partitions.items():
        part_dir = root / f"event_date={date_key}"
        part_dir.mkdir(parents=True, exist_ok=True)

        chunk_id = uuid.uuid4().hex[:8]
        path = part_dir / f"{prefix}_{int(time.time())}_{chunk_id}.parquet"

        table = pa.Table.from_pylist(part_records, schema=schema)
        pq.write_table(table, path, compression="snappy")


# ─────────────────────────────────────────────────────────────────────────────
# SilverStore — buffered Parquet writer with sub-second flush
# ─────────────────────────────────────────────────────────────────────────────
class SilverStore:
    """
    Buffers Silver records in memory and flushes to partitioned Parquet files.
    Flush triggers: >= 500 records OR >= 0.5 seconds since last flush.
    """

    def __init__(self, root_dir: str = SILVER_DIR) -> None:
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._buffer: list[dict[str, Any]] = []
        self._last_flush: float = time.monotonic()
        self._total_records: int = 0
        self._total_flushes: int = 0

    def write(self, silver_rec: dict[str, Any]) -> None:
        self._buffer.append(silver_rec)
        if len(self._buffer) >= _SILVER_FLUSH_RECORDS:
            self.flush()

    def maybe_flush(self) -> None:
        """Flush if the time threshold has been reached."""
        if self._buffer and (time.monotonic() - self._last_flush) >= _SILVER_FLUSH_SEC:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return

        _flush_to_partitioned_parquet(self._buffer, self._root, "silver", SILVER_SCHEMA)

        count = len(self._buffer)
        self._total_records += count
        self._total_flushes += 1
        self._buffer.clear()
        self._last_flush = time.monotonic()
        logger.debug("Silver flush: %d records (%d total)", count, self._total_records)

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "total_records": self._total_records,
            "total_flushes": self._total_flushes,
            "buffer_len": len(self._buffer),
        }


# ─────────────────────────────────────────────────────────────────────────────
# GoldAccumulator — in-memory aggregation with 1s Parquet flush
# ─────────────────────────────────────────────────────────────────────────────
class _GoldBucket:
    """Mutable accumulators for a single Gold groupBy key."""
    __slots__ = (
        "packet_count", "total_bytes", "min_ts", "max_ts",
        "src_ips", "dst_ips", "dst_ports",
    )

    def __init__(self, event_ts: datetime) -> None:
        self.packet_count: int = 0
        self.total_bytes: int = 0
        self.min_ts: datetime = event_ts
        self.max_ts: datetime = event_ts
        self.src_ips: set[str] = set()
        self.dst_ips: set[str] = set()
        self.dst_ports: set[int] = set()

    def update(self, rec: dict[str, Any]) -> None:
        self.packet_count += 1
        length = rec.get("length", 0)
        self.total_bytes += length

        ts = rec["event_ts"]
        if ts < self.min_ts:
            self.min_ts = ts
        if ts > self.max_ts:
            self.max_ts = ts

        self.src_ips.add(rec.get("src_ip", ""))
        self.dst_ips.add(rec.get("dst_ip", ""))
        dst_port = rec.get("dst_port")
        if dst_port is not None:
            self.dst_ports.add(dst_port)


_GoldKey = tuple[datetime, Any, str, str, str]  # (event_hour, event_date, protocol, src_network, agent_id)


class GoldAccumulator:
    """
    In-memory hash aggregation mirroring databricks_pipeline.aggregate_to_gold.
    Flushes accumulated Gold rows to partitioned Parquet every 1 second.
    """

    def __init__(self, root_dir: str = GOLD_DIR) -> None:
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._buckets: dict[_GoldKey, _GoldBucket] = {}
        self._last_flush: float = time.monotonic()
        self._total_records: int = 0
        self._total_flushes: int = 0

    def update(self, silver_rec: dict[str, Any]) -> None:
        key: _GoldKey = (
            silver_rec["event_hour"],
            silver_rec["event_date"],
            silver_rec["protocol"],
            silver_rec["src_network"],
            silver_rec["agent_id"],
        )
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _GoldBucket(silver_rec["event_ts"])
            self._buckets[key] = bucket
        bucket.update(silver_rec)

    def maybe_flush(self) -> None:
        if self._buckets and (time.monotonic() - self._last_flush) >= _GOLD_FLUSH_SEC:
            self.flush()

    def flush(self) -> None:
        if not self._buckets:
            return

        now = datetime.now(timezone.utc)
        rows: list[dict[str, Any]] = []
        for key, b in self._buckets.items():
            event_hour, event_date, protocol, src_network, agent_id = key
            avg_size = b.total_bytes / b.packet_count if b.packet_count else 0.0
            rows.append({
                "event_hour": event_hour,
                "event_date": event_date,
                "protocol": protocol,
                "src_network": src_network,
                "agent_id": agent_id,
                "packet_count": b.packet_count,
                "total_bytes": b.total_bytes,
                "avg_packet_size": avg_size,
                "first_seen": b.min_ts,
                "last_seen": b.max_ts,
                "unique_src_ips": len(b.src_ips),
                "unique_dst_ips": len(b.dst_ips),
                "unique_dst_ports": len(b.dst_ports),
                "computed_at": now,
            })
        _flush_to_partitioned_parquet(rows, self._root, "gold", GOLD_SCHEMA)

        self._total_records += len(rows)
        self._total_flushes += 1
        self._buckets.clear()
        self._last_flush = time.monotonic()
        logger.debug("Gold flush: %d rows (%d total)", len(rows), self._total_records)

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "total_records": self._total_records,
            "total_flushes": self._total_flushes,
            "active_buckets": len(self._buckets),
        }


# ─────────────────────────────────────────────────────────────────────────────
# LiveSnapshot — in-memory aggregation written to a single JSON file
# ─────────────────────────────────────────────────────────────────────────────
_RECENT_PACKETS_LIMIT = 60
_TOP_N = 10


class LiveSnapshot:
    """
    Maintains running aggregates in pure Python and atomically writes a
    compact JSON file (_live.json) that the dashboard can read in <1ms.

    Fed per-record by DataSinkProcess — no Parquet scan needed by the reader.
    """

    def __init__(self, path: str = LIVE_SNAPSHOT_PATH) -> None:
        self._path = Path(path)
        self._last_flush: float = time.monotonic()
        self._cleanup_stale_tmp()

        self._total_packets: int = 0
        self._total_bytes: int = 0
        self._started_at: str = datetime.now(timezone.utc).isoformat()

        self._proto_packets: Counter[str] = Counter()
        self._proto_bytes: Counter[str] = Counter()

        self._src_ips: Counter[str] = Counter()
        self._dst_ips: Counter[str] = Counter()
        self._src_nets: Counter[str] = Counter()
        self._dst_ports: Counter[int] = Counter()

        # service-level aggregates
        self._service_packets: Counter[str] = Counter()
        self._service_bytes: Counter[str] = Counter()

        # per-(port, protocol) stats for the ports view
        self._port_proto_packets: Counter[tuple[int, str]] = Counter()
        self._port_proto_bytes: Counter[tuple[int, str]] = Counter()

        self._recent: deque[dict[str, Any]] = deque(maxlen=_RECENT_PACKETS_LIMIT)

        self._gold_agg: dict[str, dict[str, Any]] = {}

        # Last known capture health snapshot from _metrics_capture.json
        self._capture_health: dict[str, Any] = {}

        # Sliding 60-minute time buckets for lightweight graphs (1 bucket ~= 1 minute).
        # Each bucket: {"start": epoch_seconds, "proto": Counter, "subnet": Counter, "service": Counter}
        self._time_buckets: deque[dict[str, Any]] = deque()

    def update(self, silver_rec: dict[str, Any]) -> None:
        """Feed one Silver record — O(1)."""
        self._total_packets += 1
        length = silver_rec.get("length", 0)
        self._total_bytes += length

        proto = silver_rec.get("protocol", "OTHER")
        self._proto_packets[proto] += 1
        self._proto_bytes[proto] += length

        svc = silver_rec.get("service_info")
        if svc:
            self._service_packets[svc] += 1
            self._service_bytes[svc] += length

        self._src_ips[silver_rec.get("src_ip", "")] += 1
        self._dst_ips[silver_rec.get("dst_ip", "")] += 1
        self._src_nets[silver_rec.get("src_network", "")] += 1

        dp = silver_rec.get("dst_port")
        if dp is not None:
            self._dst_ports[dp] += 1
            key = (dp, proto)
            self._port_proto_packets[key] += 1
            self._port_proto_bytes[key] += length

        ts = silver_rec.get("event_ts")
        ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        port_category = silver_rec.get("port_category", "")

        # Maintain recent packets list for the live view.
        self._recent.append({
            "ts": ts_str,
            "src_ip": silver_rec.get("src_ip", ""),
            "dst_ip": silver_rec.get("dst_ip", ""),
            "src_port": silver_rec.get("src_port"),
            "dst_port": silver_rec.get("dst_port"),
            "protocol": proto,
            "length": length,
            "flags": silver_rec.get("flags"),
            "ttl": silver_rec.get("ttl"),
            "service_info": silver_rec.get("service_info"),
            "port_category": port_category,
            "src_network": silver_rec.get("src_network", ""),
        })

        # Update sliding time buckets (up to 60 minutes of history in 1-minute buckets).
        now_epoch: float
        if hasattr(ts, "timestamp"):
            try:
                now_epoch = ts.timestamp()
            except Exception:
                now_epoch = time.time()
        else:
            now_epoch = time.time()

        bucket_start = now_epoch - (now_epoch % 60.0)
        # Drop buckets older than 60 minutes.
        cutoff = now_epoch - 60.0 * 60.0
        while self._time_buckets and self._time_buckets[0]["start"] < cutoff:
            self._time_buckets.popleft()

        if not self._time_buckets or self._time_buckets[-1]["start"] != bucket_start:
            self._time_buckets.append({
                "start": bucket_start,
                "proto": Counter(),
                "subnet": Counter(),
                "service": Counter(),
            })

        bucket = self._time_buckets[-1]
        bucket["proto"][proto] += length
        bucket["subnet"][silver_rec.get("src_network", "")] += length
        if svc:
            bucket["service"][svc] += length

    def update_gold(self, gold_accum: "GoldAccumulator") -> None:
        """Snapshot the current Gold buckets into the live state."""
        agg: dict[str, dict[str, Any]] = {}
        for key, b in gold_accum._buckets.items():
            _, _, protocol, _, _ = key
            e = agg.get(protocol)
            if e is None:
                e = {"packets": 0, "bytes": 0, "src": 0, "dst": 0, "ports": 0}
                agg[protocol] = e
            e["packets"] += b.packet_count
            e["bytes"] += b.total_bytes
            e["src"] = max(e["src"], len(b.src_ips))
            e["dst"] = max(e["dst"], len(b.dst_ips))
            e["ports"] = max(e["ports"], len(b.dst_ports))
        self._gold_agg = agg

    def _cleanup_stale_tmp(self) -> None:
        """Remove orphaned .tmp files left by previous crashed runs."""
        parent = self._path.parent if str(self._path.parent) != "." else Path(".")
        for stale in parent.glob("tmp*.tmp"):
            try:
                stale.unlink()
            except OSError:
                pass

    def maybe_flush(self) -> None:
        now = time.monotonic()
        if (now - self._last_flush) >= _SNAPSHOT_FLUSH_SEC:
            self.flush()

    def flush(self) -> None:
        # Best-effort: enrich with capture health from the capture metrics sidecar.
        self._capture_health = self._load_capture_health()

        # Build lightweight time-window aggregations for graphs.
        now_epoch = time.time()
        windows = {
            "5m": 5 * 60.0,
            "10m": 10 * 60.0,
            "30m": 30 * 60.0,
            "60m": 60 * 60.0,
        }
        proto_windows: dict[str, Counter[str]] = {k: Counter() for k in windows}
        subnet_windows: dict[str, Counter[str]] = {k: Counter() for k in windows}
        service_windows: dict[str, Counter[str]] = {k: Counter() for k in windows}

        for bucket in self._time_buckets:
            age = now_epoch - bucket["start"]
            for label, span in windows.items():
                if age <= span:
                    proto_windows[label].update(bucket["proto"])
                    subnet_windows[label].update(bucket["subnet"])
                    service_windows[label].update(bucket["service"])

        def _format_window_map(counter_map: dict[str, Counter[str]]) -> dict[str, list[dict[str, Any]]]:
            out: dict[str, list[dict[str, Any]]] = {}
            for label, ctr in counter_map.items():
                if not ctr:
                    continue
                out[label] = [
                    {"name": name, "bytes": value}
                    for name, value in ctr.most_common(_TOP_N)
                ]
            return out

        snapshot = {
            "snapshot_at": datetime.now(timezone.utc).isoformat(),
            "started_at": self._started_at,
            "total_packets": self._total_packets,
            "total_bytes": self._total_bytes,
            "protocols": [
                {"name": p, "packets": c, "bytes": self._proto_bytes[p]}
                for p, c in self._proto_packets.most_common(_TOP_N)
            ],
            "top_src_ips": [
                {"ip": ip, "packets": c}
                for ip, c in self._src_ips.most_common(_TOP_N)
            ],
            "top_dst_ips": [
                {"ip": ip, "packets": c}
                for ip, c in self._dst_ips.most_common(_TOP_N)
            ],
            "top_subnets": [
                {"subnet": s, "packets": c}
                for s, c in self._src_nets.most_common(_TOP_N)
            ],
            "top_dst_ports": [
                {"port": p, "packets": c}
                for p, c in self._dst_ports.most_common(_TOP_N)
            ],
            "port_stats": [
                {
                    "port": port,
                    "protocol": proto,
                    "packets": cnt,
                    "bytes": self._port_proto_bytes[(port, proto)],
                }
                for (port, proto), cnt in self._port_proto_packets.most_common(30)
            ],
            "recent_packets": list(self._recent),
            "gold": self._gold_agg,
            "services": [
                {
                    "name": s,
                    "packets": c,
                    "bytes": self._service_bytes.get(s, 0),
                }
                for s, c in self._service_packets.most_common(_TOP_N)
            ],
            "capture_health": self._capture_health,
            "protocol_windows": _format_window_map(proto_windows),
            "subnet_windows": _format_window_map(subnet_windows),
            "service_windows": _format_window_map(service_windows),
        }

        tmp_path: str | None = None
        tmp_fd: int = -1
        try:
            data = json.dumps(snapshot, default=str)
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(self._path.parent or "."), suffix=".tmp"
            )
            os.write(tmp_fd, data.encode("utf-8"))
            os.close(tmp_fd)
            tmp_fd = -1
            os.replace(tmp_path, str(self._path))
            tmp_path = None
        except Exception as exc:
            logger.debug("LiveSnapshot write failed: %s", exc)
        finally:
            if tmp_fd >= 0:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
            if tmp_path is not None and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        self._last_flush = time.monotonic()

    def _load_capture_health(self) -> dict[str, Any]:
        """
        Read the capture-side metrics file (_metrics_capture.json) and derive
        a compact health summary (OS + application-level drops).

        This is best-effort: any error results in an empty dict.
        """
        metrics_path = Path("_metrics_capture.json")
        if not metrics_path.exists():
            return {}
        try:
            raw = json.loads(metrics_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

        counters = raw.get("counters", {}) or {}
        os_counters = raw.get("os_counters", {}) or {}

        os_recv = int(os_counters.get("packets_recv", 0) or 0)
        os_drop = int(os_counters.get("packets_drop", 0) or 0)
        os_ifdrop = int(os_counters.get("packets_ifdrop", 0) or 0)
        app_queued = int(counters.get("queued", 0) or 0)
        app_dropped = int(counters.get("dropped", 0) or 0)

        total_seen = os_recv + os_drop
        drop_rate = (os_drop / total_seen * 100.0) if total_seen > 0 else 0.0

        return {
            "os_recv": os_recv,
            "os_drop": os_drop,
            "os_ifdrop": os_ifdrop,
            "app_queued": app_queued,
            "app_dropped": app_dropped,
            "drop_rate_pct": round(drop_rate, 4),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Parquet Compaction — merge small files into fewer large files
# ─────────────────────────────────────────────────────────────────────────────
_COMPACTION_SIZE_THRESHOLD: int = 1 * 1024 * 1024  # files < 1 MiB are "small"


def _read_single_parquet(path: Path, schema: pa.Schema) -> pa.Table:
    """Read one Parquet file and coerce to the target schema."""
    t = pq.ParquetFile(str(path)).read()
    return _coerce_schema(t, schema)


def compact_parquet_dir(root_dir: str, schema: pa.Schema) -> int:
    """
    Scan Hive-partitioned directory, merge small Parquet files within each
    partition into larger files.  Returns the number of files merged.

    Safe to call while the pipeline is running — reads finished files,
    writes a new merged file, then removes the originals.
    """
    root = Path(root_dir)
    if not root.exists():
        return 0

    merged_total = 0
    for part_dir in root.iterdir():
        if not part_dir.is_dir() or not part_dir.name.startswith("event_date="):
            continue

        parquets = sorted(part_dir.glob("*.parquet"))
        small = []
        for pf in parquets:
            try:
                size = pf.stat().st_size
            except OSError:
                continue
            if size < _COMPACTION_SIZE_THRESHOLD:
                small.append(pf)

        if len(small) < 2:
            continue

        tables = []
        valid_files = []
        for pf in small:
            try:
                t = _read_single_parquet(pf, schema)
                tables.append(t)
                valid_files.append(pf)
            except Exception as exc:
                logger.warning("Compaction skip %s: %s", pf.name, exc)

        if len(tables) < 2:
            continue

        merged = pa.concat_tables(tables, promote_options="default")
        chunk_id = uuid.uuid4().hex[:8]
        out_name = f"compacted_{int(time.time())}_{chunk_id}.parquet"
        out_path = part_dir / out_name

        pq.write_table(merged, str(out_path), compression="snappy")

        for pf in valid_files:
            try:
                pf.unlink()
                merged_total += 1
            except OSError:
                pass

    if merged_total > 0:
        logger.info("Compaction: merged %d small files in %s", merged_total, root_dir)
    return merged_total


# ─────────────────────────────────────────────────────────────────────────────
# Schema Evolution — read old Parquet files even if schema has changed
# ─────────────────────────────────────────────────────────────────────────────
def _coerce_schema(table: pa.Table, target_schema: pa.Schema) -> pa.Table:
    """
    Align a table's schema to the target schema:
    - Missing columns are filled with nulls of the correct type.
    - Extra columns are dropped.
    - Type mismatches are cast where safe.
    """
    columns = {}
    for field in target_schema:
        if field.name in table.column_names:
            col = table.column(field.name)
            if col.type != field.type:
                try:
                    col = col.cast(field.type, safe=True)
                except (pa.ArrowInvalid, pa.ArrowNotImplementedError):
                    col = pa.nulls(len(table), type=field.type)
            columns[field.name] = col
        else:
            columns[field.name] = pa.nulls(len(table), type=field.type)

    arrays = [columns[f.name] for f in target_schema]
    return pa.table(arrays, schema=target_schema)


def _empty_table(schema: pa.Schema) -> pa.Table:
    """Create an empty table that matches the given schema."""
    arrays = [pa.array([], type=f.type) for f in schema]
    return pa.table(arrays, names=[f.name for f in schema])


def read_parquet_with_evolution(
    root_dir: str,
    schema: pa.Schema,
    partition_filter: str | None = None,
) -> pa.Table:
    """
    Read all Parquet files from a Hive-partitioned directory, applying
    schema evolution to handle missing/extra/mismatched columns.
    """
    root = Path(root_dir)
    if not root.exists():
        return _empty_table(schema)

    tables = []
    for part_dir in root.iterdir():
        if not part_dir.is_dir() or not part_dir.name.startswith("event_date="):
            continue
        if partition_filter and part_dir.name != partition_filter:
            continue

        for pf in part_dir.glob("*.parquet"):
            try:
                t = _read_single_parquet(pf, schema)
                tables.append(t)
            except Exception as exc:
                logger.warning("Read skip %s: %s", pf, exc)

    if not tables:
        return _empty_table(schema)

    return pa.concat_tables(tables, promote_options="default")


# ─────────────────────────────────────────────────────────────────────────────
# Retention — purge old Silver/Gold partitions
# ─────────────────────────────────────────────────────────────────────────────
_SILVER_RETENTION_DAYS: int = 7
_GOLD_RETENTION_DAYS: int = 30


def enforce_retention(
    root_dir: str,
    retention_days: int | None = None,
    layer: str = "silver",
) -> int:
    """
    Delete Hive partitions older than retention_days.
    Returns the number of partitions purged.
    """
    if retention_days is None:
        retention_days = _SILVER_RETENTION_DAYS if layer == "silver" else _GOLD_RETENTION_DAYS

    root = Path(root_dir)
    if not root.exists():
        return 0

    utc_today = datetime.now(timezone.utc).date()
    cutoff = utc_today - timedelta(days=retention_days)
    purged = 0

    for part_dir in list(root.iterdir()):
        if not part_dir.is_dir() or not part_dir.name.startswith("event_date="):
            continue

        try:
            date_str = part_dir.name.split("=", 1)[1]
            part_date = date.fromisoformat(date_str)
        except (ValueError, IndexError):
            continue

        if part_date < cutoff:
            shutil.rmtree(part_dir, ignore_errors=True)
            purged += 1
            logger.info("Retention: purged partition %s from %s", part_dir.name, root_dir)

    return purged
