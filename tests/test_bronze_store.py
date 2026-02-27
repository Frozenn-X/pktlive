"""Tests for BronzeStore — write, rotation, quarantine, compaction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from network_interface.storage.bronze_store import BronzeConfig, BronzeStore, FileState, ManifestEntry


class TestBronzeStore:
    def _make_store(self, tmp_path: Path, **overrides) -> BronzeStore:
        defaults = dict(
            root_dir=tmp_path / "bronze",
            max_file_bytes=1024 * 1024,     # 1 MiB (respects ge=1048576)
            max_file_age_sec=3600,
            flush_threshold=10,
            flush_interval_sec=0.1,
            fsync_interval_sec=1.0,
            retention_hours=72,
            compaction_min_bytes=1024 * 1024,
        )
        defaults.update(overrides)
        cfg = BronzeConfig(**defaults)
        return BronzeStore(cfg, agent_id="test_agent")

    def test_write_creates_ndjson(self, tmp_path: Path):
        store = self._make_store(tmp_path)
        for i in range(10):
            store.write_record(json.dumps({"i": i}))
        store.seal_active()

        ndjson_files = list((tmp_path / "bronze" / "data").rglob("*.ndjson"))
        assert len(ndjson_files) >= 1
        content = ndjson_files[0].read_text()
        assert '"i"' in content

    def test_rotation_on_size(self, tmp_path: Path):
        store = self._make_store(tmp_path, max_file_bytes=1024 * 1024, flush_threshold=10)
        big_payload = "x" * 120_000
        for i in range(100):
            store.write_record(json.dumps({"data": big_payload, "i": i}))
        store.seal_active()

        ndjson_files = list((tmp_path / "bronze" / "data").rglob("*.ndjson"))
        assert len(ndjson_files) >= 2, (
            f"Expected rotation but got {len(ndjson_files)} files, "
            f"total_bytes={store.stats['total_bytes']}"
        )

    def test_quarantine(self, tmp_path: Path):
        store = self._make_store(tmp_path)
        store.quarantine(raw_data='{"bad": true}', reason="validation failed")

        q_files = list((tmp_path / "bronze" / "_quarantine").rglob("*.jsonl"))
        assert len(q_files) >= 1
        content = q_files[0].read_text()
        assert "validation failed" in content

    def test_manifest_tracking(self, tmp_path: Path):
        store = self._make_store(tmp_path)
        for i in range(10):
            store.write_record(json.dumps({"i": i}))
        store.seal_active()

        manifest_path = tmp_path / "bronze" / "_manifest" / "manifest.jsonl"
        assert manifest_path.exists()
        lines = manifest_path.read_text().strip().split("\n")
        assert len(lines) >= 2  # ACTIVE + SEALED

    def test_stats(self, tmp_path: Path):
        store = self._make_store(tmp_path)
        for i in range(10):
            store.write_record(json.dumps({"i": i}))
        store.seal_active()

        stats = store.stats
        assert stats["total_records"] == 10
        assert stats["quarantined"] == 0

    def test_agent_meta_written(self, tmp_path: Path):
        store = self._make_store(tmp_path)
        meta_path = tmp_path / "bronze" / "_meta" / "agent.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["agent_id"] == "test_agent"

    def test_pending_transfer(self, tmp_path: Path):
        store = self._make_store(tmp_path)
        for i in range(10):
            store.write_record(json.dumps({"i": i}))
        store.seal_active()

        sealed = store.pending_transfer()
        assert len(sealed) >= 1
        assert all(e.state == FileState.SEALED for e in sealed)

    def test_compact_merges_small_files_and_preserves_records(self, tmp_path: Path):
        store = self._make_store(tmp_path)
        root = tmp_path / "bronze" / "data"
        part_dir = root / "dt=2026-01-01" / "hr=00"
        part_dir.mkdir(parents=True, exist_ok=True)

        total_records = 0
        for i in range(3):
            path = part_dir / f"small_{i}.ndjson"
            payload = "\n".join(json.dumps({"i": j}) for j in range(2))
            path.write_text(payload + "\n")
            size = path.stat().st_size
            total_records += 2
            entry = ManifestEntry(
                file_path=str(path),
                state=FileState.SEALED,
                partition_dt="2026-01-01",
                partition_hr=0,
                created_at="2026-01-01T00:00:00+00:00",
                sealed_at="2026-01-01T01:00:00+00:00",
                size_bytes=size,
                record_count=2,
                agent_id="test_agent",
            )
            store._manifest.register_active(entry)  # type: ignore[attr-defined]

        merged = store.compact()
        assert merged >= 2

        sealed = store.pending_transfer()
        sealed_records = sum(e.record_count for e in sealed)
        assert sealed_records == total_records

    def test_enforce_retention_purges_transferred_files(self, tmp_path: Path):
        store = self._make_store(tmp_path)
        root = tmp_path / "bronze" / "data"
        part_dir = root / "dt=2026-01-01" / "hr=00"
        part_dir.mkdir(parents=True, exist_ok=True)

        path = part_dir / "old.ndjson"
        path.write_text('{"x": 1}\n')

        entry = ManifestEntry(
            file_path=str(path),
            state=FileState.TRANSFERRED,
            partition_dt="2026-01-01",
            partition_hr=0,
            created_at="2026-01-01T00:00:00+00:00",
            sealed_at="2026-01-01T00:10:00+00:00",
            transferred_at="2026-01-01T00:20:00+00:00",
            size_bytes=path.stat().st_size,
            record_count=1,
            agent_id="test_agent",
        )
        store._manifest.register_active(entry)  # type: ignore[attr-defined]

        store._cfg.retention_hours = 1  # type: ignore[attr-defined]

        purged = store.enforce_retention()
        assert purged == 1
        assert not path.exists()
