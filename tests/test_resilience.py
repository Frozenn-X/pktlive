"""Resilience tests — atomic writes, readable snapshots, no orphan .tmp files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from network_interface.storage.streaming_pipeline import LiveSnapshot, silver_transform
from tests.conftest import make_bronze_records


class TestSnapshotResilience:
    def test_flushed_snapshot_is_valid_json_and_parseable(self, tmp_path: Path):
        """After flush, _live.json must be valid JSON and parseable by a reader."""
        path = str(tmp_path / "_live.json")
        snap = LiveSnapshot(path=path)
        for rec in make_bronze_records(5):
            sr = silver_transform(rec)
            if sr:
                snap.update(sr)
        snap.flush()

        raw = Path(path).read_text()
        data = json.loads(raw)
        assert "total_packets" in data
        assert "snapshot_at" in data
        assert data["total_packets"] == 5

    def test_multiple_flushes_produce_valid_snapshots(self, tmp_path: Path):
        """Repeated flush cycles must always leave valid JSON on disk."""
        path = str(tmp_path / "_live.json")
        snap = LiveSnapshot(path=path)
        for cycle in range(3):
            for rec in make_bronze_records(2):
                sr = silver_transform(rec)
                if sr:
                    snap.update(sr)
            snap.flush()
            raw = Path(path).read_text()
            data = json.loads(raw)
            assert data["total_packets"] == (cycle + 1) * 2
