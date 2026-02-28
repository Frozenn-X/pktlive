"""Shared fixtures for the test suite."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

# Ensure src/ is on sys.path for src-layout imports in tests
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in os.sys.path:
    os.sys.path.insert(0, str(SRC))

os.environ["PYARROW_IGNORE_TIMEZONE"] = "1"


@pytest.fixture()
def tmp_data_dir(tmp_path: Path) -> Path:
    """Return a temporary directory for data output."""
    return tmp_path


@pytest.fixture()
def sample_bronze_record() -> dict[str, Any]:
    """A minimal valid Bronze record dict (pre-Pydantic)."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "src_ip": "192.168.1.10",
        "dst_ip": "10.0.0.1",
        "src_port": 54321,
        "dst_port": 443,
        "protocol": "TCP",
        "length": 128,
        "ttl": 64,
        "flags": "SA",
        "agent_id": "test_agent_01",
    }


@pytest.fixture()
def sample_silver_record() -> dict[str, Any]:
    """A Silver record dict (output of silver_transform)."""
    now = datetime.now(timezone.utc)
    return {
        "event_ts": now,
        "event_hour": now.replace(minute=0, second=0, microsecond=0),
        "event_date": now.date(),
        "src_ip": "192.168.1.10",
        "dst_ip": "10.0.0.1",
        "src_port": 54321,
        "dst_port": 443,
        "protocol": "TCP",
        "length": 128,
        "ttl": 64,
        "flags": "SA",
        "agent_id": "test_agent_01",
        "ingested_at": now,
        "src_network": "192.168.1.0/24",
    }


def make_bronze_records(n: int, **overrides: Any) -> list[dict[str, Any]]:
    """Generate n Bronze record dicts with optional field overrides."""
    records = []
    protocols = ["TCP", "UDP", "ICMP"]
    for i in range(n):
        rec = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "src_ip": f"192.168.1.{i % 256}",
            "dst_ip": f"10.0.0.{i % 256}",
            "src_port": 50000 + i,
            "dst_port": [443, 80, 53, 8080][i % 4],
            "protocol": protocols[i % len(protocols)],
            "length": 64 + i * 2,
            "ttl": 64,
            "flags": "A" if i % 2 == 0 else "S",
            "agent_id": "test_agent",
        }
        rec.update(overrides)
        records.append(rec)
    return records
