"""Performance smoke tests — assert upper bounds for hot paths."""

from __future__ import annotations

import time

import pytest

from network_interface.storage.streaming_pipeline import silver_transform
from tests.conftest import make_bronze_records


@pytest.mark.smoke
def test_silver_transform_throughput_under_100ms_per_1000():
    """Silver transform of 1000 records should complete in under 100ms."""
    records = make_bronze_records(1000)
    t0 = time.perf_counter()
    for rec in records:
        silver_transform(rec)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms < 100.0, f"silver_transform 1000 records took {elapsed_ms:.1f}ms (max 100ms)"


@pytest.mark.smoke
def test_silver_transform_single_record_under_1ms():
    """Single silver_transform call should be under 1ms."""
    rec = make_bronze_records(1)[0]
    t0 = time.perf_counter()
    for _ in range(100):
        silver_transform(rec)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    per_call_ms = elapsed_ms / 100
    assert per_call_ms < 1.0, f"per-call {per_call_ms:.3f}ms (max 1ms)"
