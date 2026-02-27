"""
pipeline_metrics.py — Real-time pipeline health metrics.

Tracks packets/sec, queue depth, errors, latency per layer, and writes
a compact _metrics.json sidecar every N seconds for monitoring tools.

Designed to be fed from DataSinkProcess and CaptureEngine with zero
performance impact (Counter increments + periodic JSON dump).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("pipeline_metrics")

_METRICS_FLUSH_SEC: float = 2.0
_RATE_WINDOW_SEC: float = 10.0


class PipelineMetrics:
    """
    Thread/process-safe within a single process (not shared across mp).
    Each process (capture engine, data sink) has its own instance.
    The DataSink instance writes _metrics.json.
    """

    def __init__(self, path: str = "_metrics.json") -> None:
        self._path = Path(path)
        self._last_flush: float = time.monotonic()
        self._start_time: float = time.monotonic()

        self._packets_captured: int = 0
        self._packets_parsed: int = 0
        self._packets_queued: int = 0
        self._packets_dropped: int = 0
        self._packets_processed: int = 0
        self._validation_errors: int = 0
        self._silver_records: int = 0
        self._gold_flushes: int = 0
        self._silver_flushes: int = 0
        self._snapshot_flushes: int = 0

        self._queue_depth: int = 0

        self._capture_to_queue_us: deque[float] = deque(maxlen=1000)
        self._queue_to_bronze_us: deque[float] = deque(maxlen=1000)
        self._bronze_to_silver_us: deque[float] = deque(maxlen=1000)
        self._silver_to_gold_us: deque[float] = deque(maxlen=1000)

        self._rate_timestamps: deque[float] = deque(maxlen=50_000)

        self._errors: list[dict[str, Any]] = []
        self._max_errors: int = 100

    def inc_captured(self) -> None:
        self._packets_captured += 1

    def inc_parsed(self) -> None:
        self._packets_parsed += 1

    def inc_queued(self) -> None:
        self._packets_queued += 1

    def inc_dropped(self) -> None:
        self._packets_dropped += 1

    def inc_processed(self) -> None:
        self._packets_processed += 1
        self._rate_timestamps.append(time.monotonic())

    def inc_validation_error(self) -> None:
        self._validation_errors += 1

    def inc_silver(self) -> None:
        self._silver_records += 1

    def inc_silver_flush(self) -> None:
        self._silver_flushes += 1

    def inc_gold_flush(self) -> None:
        self._gold_flushes += 1

    def inc_snapshot_flush(self) -> None:
        self._snapshot_flushes += 1

    def set_queue_depth(self, depth: int) -> None:
        self._queue_depth = depth

    def record_latency(self, stage: str, us: float) -> None:
        target = {
            "capture_to_queue": self._capture_to_queue_us,
            "queue_to_bronze": self._queue_to_bronze_us,
            "bronze_to_silver": self._bronze_to_silver_us,
            "silver_to_gold": self._silver_to_gold_us,
        }.get(stage)
        if target is not None:
            target.append(us)

    def record_error(self, component: str, error: str) -> None:
        if len(self._errors) < self._max_errors:
            self._errors.append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "component": component,
                "error": error,
            })

    def _compute_rate(self) -> float:
        """Packets per second over the last _RATE_WINDOW_SEC."""
        now = time.monotonic()
        cutoff = now - _RATE_WINDOW_SEC
        while self._rate_timestamps and self._rate_timestamps[0] < cutoff:
            self._rate_timestamps.popleft()
        count = len(self._rate_timestamps)
        return count / _RATE_WINDOW_SEC if count > 0 else 0.0

    def snapshot(self) -> dict[str, Any]:
        uptime = time.monotonic() - self._start_time

        def _latency_percentiles(buf: deque[float]) -> dict[str, float]:
            if not buf:
                return {}
            ordered = sorted(buf)
            n = len(ordered)

            def _pick(p: float) -> float:
                idx = int(n * p / 100.0)
                idx = min(idx, n - 1)
                return round(ordered[idx], 1)

            return {
                "p50": _pick(50),
                "p95": _pick(95),
                "p99": _pick(99),
            }

        latency: dict[str, Any] = {}
        for stage, buf in [
            ("capture_to_queue", self._capture_to_queue_us),
            ("queue_to_bronze", self._queue_to_bronze_us),
            ("bronze_to_silver", self._bronze_to_silver_us),
            ("silver_to_gold", self._silver_to_gold_us),
        ]:
            if buf:
                latency[stage] = _latency_percentiles(buf)

        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "uptime_sec": round(uptime, 1),
            "counters": {
                "captured": self._packets_captured,
                "parsed": self._packets_parsed,
                "queued": self._packets_queued,
                "dropped": self._packets_dropped,
                "processed": self._packets_processed,
                "validation_errors": self._validation_errors,
                "silver_records": self._silver_records,
                "silver_flushes": self._silver_flushes,
                "gold_flushes": self._gold_flushes,
                "snapshot_flushes": self._snapshot_flushes,
            },
            "rates": {
                "packets_per_sec": round(self._compute_rate(), 1),
            },
            "queue_depth": self._queue_depth,
            "latency_us": latency,
            "recent_errors": self._errors[-10:],
        }

    def maybe_flush(self) -> None:
        now = time.monotonic()
        if (now - self._last_flush) >= _METRICS_FLUSH_SEC:
            self.flush()

    def flush(self) -> None:
        tmp_path: str | None = None
        tmp_fd: int = -1
        try:
            data = json.dumps(self.snapshot(), default=str)
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(self._path.parent or "."), suffix=".tmp"
            )
            os.write(tmp_fd, data.encode("utf-8"))
            os.close(tmp_fd)
            tmp_fd = -1
            os.replace(tmp_path, str(self._path))
            tmp_path = None
        except Exception as exc:
            logger.debug("Metrics write failed: %s", exc)
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
