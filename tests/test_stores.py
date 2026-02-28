"""Tests for SilverStore, GoldAccumulator, and LiveSnapshot."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from network_interface.storage.streaming_pipeline import (
    GoldAccumulator,
    LiveSnapshot,
    SilverStore,
    silver_transform,
    _SILVER_FLUSH_SEC,
    _GOLD_FLUSH_SEC,
)
from tests.conftest import make_bronze_records


class TestSilverStore:
    def test_flush_creates_parquet(self, tmp_data_dir: Path):
        store = SilverStore(root_dir=str(tmp_data_dir / "silver"))
        records = make_bronze_records(10)
        for rec in records:
            sr = silver_transform(rec)
            if sr:
                store.write(sr)
        store.flush()

        parquets = list((tmp_data_dir / "silver").rglob("*.parquet"))
        assert len(parquets) >= 1
        table = pq.ParquetFile(str(parquets[0])).read()
        assert table.num_rows > 0
        assert "event_ts" in table.column_names

    def test_auto_flush_at_threshold(self, tmp_data_dir: Path):
        store = SilverStore(root_dir=str(tmp_data_dir / "silver"))
        records = make_bronze_records(600)
        for rec in records:
            sr = silver_transform(rec)
            if sr:
                store.write(sr)
        assert store.stats["total_flushes"] >= 1

    def test_empty_flush_is_noop(self, tmp_data_dir: Path):
        store = SilverStore(root_dir=str(tmp_data_dir / "silver"))
        store.flush()
        assert store.stats["total_flushes"] == 0

    def test_stats_accumulate(self, tmp_data_dir: Path):
        store = SilverStore(root_dir=str(tmp_data_dir / "silver"))
        records = make_bronze_records(20)
        for rec in records:
            sr = silver_transform(rec)
            if sr:
                store.write(sr)
        store.flush()
        assert store.stats["total_records"] == 20
        assert store.stats["buffer_len"] == 0

    def test_hive_partitioning(self, tmp_data_dir: Path):
        store = SilverStore(root_dir=str(tmp_data_dir / "silver"))
        records = make_bronze_records(5)
        for rec in records:
            sr = silver_transform(rec)
            if sr:
                store.write(sr)
        store.flush()
        dirs = [d for d in (tmp_data_dir / "silver").iterdir() if d.is_dir()]
        assert all(d.name.startswith("event_date=") for d in dirs)

    def test_maybe_flush_time_based(self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch):
        fake_now = [0.0]

        def _fake_monotonic() -> float:
            return fake_now[0]

        monkeypatch.setattr("network_interface.storage.streaming_pipeline.time.monotonic", _fake_monotonic)
        store = SilverStore(root_dir=str(tmp_data_dir / "silver"))

        records = make_bronze_records(1)
        for rec in records:
            sr = silver_transform(rec)
            if sr:
                store.write(sr)

        # Buffer has data but threshold not reached; no flush yet.
        assert store.stats["total_flushes"] == 0

        # Advance time past flush interval and trigger maybe_flush.
        fake_now[0] = _SILVER_FLUSH_SEC + 0.1
        store.maybe_flush()
        assert store.stats["total_flushes"] == 1
        assert store.stats["buffer_len"] == 0


class TestGoldAccumulator:
    def test_flush_creates_parquet(self, tmp_data_dir: Path):
        accum = GoldAccumulator(root_dir=str(tmp_data_dir / "gold"))
        records = make_bronze_records(50)
        for rec in records:
            sr = silver_transform(rec)
            if sr:
                accum.update(sr)
        accum.flush()

        parquets = list((tmp_data_dir / "gold").rglob("*.parquet"))
        assert len(parquets) >= 1
        table = pq.ParquetFile(str(parquets[0])).read()
        assert "packet_count" in table.column_names
        assert "total_bytes" in table.column_names

    def test_aggregation_correctness(self, tmp_data_dir: Path):
        accum = GoldAccumulator(root_dir=str(tmp_data_dir / "gold"))
        records = make_bronze_records(10, protocol="TCP", src_ip="1.2.3.4")
        for rec in records:
            sr = silver_transform(rec)
            if sr:
                accum.update(sr)

        assert len(accum._buckets) >= 1
        total_pkts = sum(b.packet_count for b in accum._buckets.values())
        assert total_pkts == 10

    def test_empty_flush_is_noop(self, tmp_data_dir: Path):
        accum = GoldAccumulator(root_dir=str(tmp_data_dir / "gold"))
        accum.flush()
        assert accum.stats["total_flushes"] == 0

    def test_flush_clears_buckets(self, tmp_data_dir: Path):
        accum = GoldAccumulator(root_dir=str(tmp_data_dir / "gold"))
        records = make_bronze_records(5)
        for rec in records:
            sr = silver_transform(rec)
            if sr:
                accum.update(sr)
        accum.flush()
        assert accum.stats["active_buckets"] == 0

    def test_maybe_flush_time_based(self, tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch):
        fake_now = [0.0]

        def _fake_monotonic() -> float:
            return fake_now[0]

        monkeypatch.setattr("network_interface.storage.streaming_pipeline.time.monotonic", _fake_monotonic)
        accum = GoldAccumulator(root_dir=str(tmp_data_dir / "gold"))

        records = make_bronze_records(3)
        for rec in records:
            sr = silver_transform(rec)
            if sr:
                accum.update(sr)

        assert accum.stats["total_flushes"] == 0

        fake_now[0] = _GOLD_FLUSH_SEC + 0.1
        accum.maybe_flush()
        assert accum.stats["total_flushes"] == 1


class TestLiveSnapshot:
    def test_flush_creates_json(self, tmp_data_dir: Path):
        path = str(tmp_data_dir / "_live.json")
        snap = LiveSnapshot(path=path)
        records = make_bronze_records(10)
        for rec in records:
            sr = silver_transform(rec)
            if sr:
                snap.update(sr)
        snap.flush()

        data = json.loads(Path(path).read_text())
        # Ensure multiple protocols (TCP, UDP, ICMP) are present.
        names = {p["name"] for p in data["protocols"]}
        assert {"TCP", "UDP", "ICMP"}.issubset(names)
        assert data["total_packets"] == 10
        assert len(data["recent_packets"]) == 10

    def test_recent_packets_capped(self, tmp_data_dir: Path):
        path = str(tmp_data_dir / "_live.json")
        snap = LiveSnapshot(path=path)
        records = make_bronze_records(100)
        for rec in records:
            sr = silver_transform(rec)
            if sr:
                snap.update(sr)
        snap.flush()

        data = json.loads(Path(path).read_text())
        assert len(data["recent_packets"]) == 60  # _RECENT_PACKETS_LIMIT

    def test_port_stats_tracked(self, tmp_data_dir: Path):
        path = str(tmp_data_dir / "_live.json")
        snap = LiveSnapshot(path=path)
        records = make_bronze_records(20)
        for rec in records:
            sr = silver_transform(rec)
            if sr:
                snap.update(sr)
        snap.flush()

        data = json.loads(Path(path).read_text())
        assert len(data["port_stats"]) > 0
        first = data["port_stats"][0]
        assert "port" in first
        assert "protocol" in first
        assert "bytes" in first
        assert first["bytes"] > 0

    def test_gold_aggregation_snapshot(self, tmp_data_dir: Path):
        path = str(tmp_data_dir / "_live.json")
        snap = LiveSnapshot(path=path)
        accum = GoldAccumulator(root_dir=str(tmp_data_dir / "gold"))

        records = make_bronze_records(10)
        for rec in records:
            sr = silver_transform(rec)
            if sr:
                snap.update(sr)
                accum.update(sr)

        snap.update_gold(accum)
        snap.flush()

        data = json.loads(Path(path).read_text())
        assert len(data["gold"]) > 0

    def test_empty_snapshot_writes_valid_json(self, tmp_data_dir: Path):
        path = str(tmp_data_dir / "_live.json")
        snap = LiveSnapshot(path=path)
        snap.flush()
        raw = Path(path).read_text()
        data = json.loads(raw)
        assert data["total_packets"] == 0

    def test_cleanup_stale_tmp(self, tmp_data_dir: Path):
        parent = tmp_data_dir
        stale = parent / "tmp123.tmp"
        keep = parent / "keep.tmp"
        stale.write_text("x")
        keep.write_text("y")

        path = str(parent / "_live.json")
        LiveSnapshot(path=path)

        assert not stale.exists()
        assert keep.exists()
