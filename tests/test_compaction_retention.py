"""Tests for Parquet compaction, schema evolution, and Silver/Gold retention."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from network_interface.storage.streaming_pipeline import (
    SILVER_SCHEMA,
    GOLD_SCHEMA,
    SilverStore,
    GoldAccumulator,
    compact_parquet_dir,
    enforce_retention,
    read_parquet_with_evolution,
    silver_transform,
    _coerce_schema,
)
from tests.conftest import make_bronze_records


class TestCompaction:
    def test_merges_small_files(self, tmp_data_dir: Path):
        silver_dir = str(tmp_data_dir / "silver")
        store = SilverStore(root_dir=silver_dir)

        for batch in range(5):
            records = make_bronze_records(10)
            for rec in records:
                sr = silver_transform(rec)
                if sr:
                    store.write(sr)
            store.flush()

        part_dirs = list((tmp_data_dir / "silver").iterdir())
        assert len(part_dirs) >= 1
        files_before = list(part_dirs[0].glob("*.parquet"))
        assert len(files_before) == 5

        # Row count before compaction.
        table_before = read_parquet_with_evolution(silver_dir, SILVER_SCHEMA)
        rows_before = table_before.num_rows

        merged = compact_parquet_dir(silver_dir, SILVER_SCHEMA)
        assert merged >= 2

        files_after = list(part_dirs[0].glob("*.parquet"))
        assert len(files_after) < len(files_before)

        # Row count preserved after compaction.
        table_after = read_parquet_with_evolution(silver_dir, SILVER_SCHEMA)
        assert table_after.num_rows == rows_before

    def test_noop_on_missing_dir(self, tmp_data_dir: Path):
        assert compact_parquet_dir(str(tmp_data_dir / "nope"), SILVER_SCHEMA) == 0

    def test_noop_on_single_file(self, tmp_data_dir: Path):
        silver_dir = str(tmp_data_dir / "silver")
        store = SilverStore(root_dir=silver_dir)
        records = make_bronze_records(10)
        for rec in records:
            sr = silver_transform(rec)
            if sr:
                store.write(sr)
        store.flush()

        assert compact_parquet_dir(silver_dir, SILVER_SCHEMA) == 0


class TestSchemaEvolution:
    def test_missing_column_filled_with_nulls(self):
        old_schema = pa.schema([
            ("src_ip", pa.string()),
            ("dst_ip", pa.string()),
        ])
        old_table = pa.table(
            {"src_ip": ["1.2.3.4"], "dst_ip": ["5.6.7.8"]},
            schema=old_schema,
        )

        new_schema = pa.schema([
            ("src_ip", pa.string()),
            ("dst_ip", pa.string()),
            ("new_field", pa.int64()),
        ])

        result = _coerce_schema(old_table, new_schema)
        assert "new_field" in result.column_names
        assert result.column("new_field").null_count == 1

    def test_extra_column_dropped(self):
        wide_schema = pa.schema([
            ("src_ip", pa.string()),
            ("dst_ip", pa.string()),
            ("obsolete", pa.string()),
        ])
        wide_table = pa.table(
            {"src_ip": ["1.2.3.4"], "dst_ip": ["5.6.7.8"], "obsolete": ["x"]},
            schema=wide_schema,
        )

        target_schema = pa.schema([
            ("src_ip", pa.string()),
            ("dst_ip", pa.string()),
        ])

        result = _coerce_schema(wide_table, target_schema)
        assert "obsolete" not in result.column_names
        assert result.num_columns == 2

    def test_read_with_evolution(self, tmp_data_dir: Path):
        silver_dir = str(tmp_data_dir / "silver")
        store = SilverStore(root_dir=silver_dir)
        records = make_bronze_records(5)
        for rec in records:
            sr = silver_transform(rec)
            if sr:
                store.write(sr)
        store.flush()

        table = read_parquet_with_evolution(silver_dir, SILVER_SCHEMA)
        assert table.num_rows == 5
        assert table.schema == SILVER_SCHEMA

    def test_read_empty_dir(self, tmp_data_dir: Path):
        table = read_parquet_with_evolution(str(tmp_data_dir / "nope"), SILVER_SCHEMA)
        assert table.num_rows == 0

    def test_type_mismatch_falls_back_to_nulls(self):
        # Source table has string data that cannot be safely cast to int.
        src_schema = pa.schema([
            ("length", pa.string()),
        ])
        src_table = pa.table({"length": ["not-a-number"]}, schema=src_schema)

        target_schema = pa.schema([
            ("length", pa.int64()),
        ])

        result = _coerce_schema(src_table, target_schema)
        assert "length" in result.column_names
        col = result.column("length")
        assert col.null_count == result.num_rows


class TestRetention:
    def _create_old_partition(self, root: Path, days_ago: int) -> Path:
        old_date = datetime.now(timezone.utc).date() - timedelta(days=days_ago)
        part_dir = root / f"event_date={old_date.isoformat()}"
        part_dir.mkdir(parents=True, exist_ok=True)
        dummy = pa.table({"x": [1]})
        pq.write_table(dummy, str(part_dir / "dummy.parquet"))
        return part_dir

    def test_purges_old_partitions(self, tmp_data_dir: Path):
        silver_dir = tmp_data_dir / "silver"
        silver_dir.mkdir()
        self._create_old_partition(silver_dir, 10)
        self._create_old_partition(silver_dir, 2)

        purged = enforce_retention(str(silver_dir), retention_days=7, layer="silver")
        assert purged == 1

        remaining = list(silver_dir.iterdir())
        assert len(remaining) == 1

    def test_keeps_recent_partitions(self, tmp_data_dir: Path):
        silver_dir = tmp_data_dir / "silver"
        silver_dir.mkdir()
        self._create_old_partition(silver_dir, 1)
        self._create_old_partition(silver_dir, 3)

        purged = enforce_retention(str(silver_dir), retention_days=7, layer="silver")
        assert purged == 0

    def test_noop_on_missing_dir(self, tmp_data_dir: Path):
        assert enforce_retention(str(tmp_data_dir / "nope"), layer="silver") == 0

    def test_gold_retention_default(self, tmp_data_dir: Path):
        gold_dir = tmp_data_dir / "gold"
        gold_dir.mkdir()
        self._create_old_partition(gold_dir, 35)
        self._create_old_partition(gold_dir, 5)

        purged = enforce_retention(str(gold_dir), layer="gold")
        assert purged == 1
