"""Integration test: Bronze record -> Silver transform -> Gold aggregation -> Snapshot."""

from __future__ import annotations

import json
from pathlib import Path

from network_interface.storage.streaming_pipeline import (
    GoldAccumulator,
    LiveSnapshot,
    SilverStore,
    silver_transform,
)
from tests.conftest import make_bronze_records


class TestEndToEndPipeline:
    def test_full_pipeline_flow(self, tmp_data_dir: Path):
        """Simulate the DataSinkProcess loop: validate -> silver -> gold -> snapshot."""
        silver_store = SilverStore(root_dir=str(tmp_data_dir / "silver"))
        gold_accum = GoldAccumulator(root_dir=str(tmp_data_dir / "gold"))
        snapshot = LiveSnapshot(path=str(tmp_data_dir / "_live.json"))

        records = make_bronze_records(100)
        for rec in records:
            silver_rec = silver_transform(rec)
            assert silver_rec is not None
            silver_store.write(silver_rec)
            gold_accum.update(silver_rec)
            snapshot.update(silver_rec)

        silver_store.flush()
        gold_accum.flush()
        snapshot.update_gold(gold_accum)
        snapshot.flush()

        silver_files = list((tmp_data_dir / "silver").rglob("*.parquet"))
        gold_files = list((tmp_data_dir / "gold").rglob("*.parquet"))
        live_path = tmp_data_dir / "_live.json"

        assert len(silver_files) >= 1
        assert len(gold_files) >= 1
        assert live_path.exists()

        snap_data = json.loads(live_path.read_text())
        assert snap_data["total_packets"] == 100
        total_proto_pkts = sum(p["packets"] for p in snap_data["protocols"])
        assert total_proto_pkts == 100

    def test_bad_records_filtered(self, tmp_data_dir: Path):
        """Records with bad timestamps should be filtered out by silver_transform."""
        good = make_bronze_records(5)
        bad = [{"protocol": "TCP", "length": 10}]  # no timestamp

        silver_store = SilverStore(root_dir=str(tmp_data_dir / "silver"))
        count = 0
        for rec in good + bad:
            sr = silver_transform(rec)
            if sr:
                silver_store.write(sr)
                count += 1
        silver_store.flush()

        assert count == 5  # bad record filtered
        assert silver_store.stats["total_records"] == 5
