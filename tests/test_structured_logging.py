"""Tests for structured JSON logging and pipeline metrics."""

from __future__ import annotations

import json
import logging
import uuid
from io import StringIO
from pathlib import Path

import pytest

from network_interface.monitoring.structured_log import JSONFormatter, set_correlation_id, get_correlation_id
from network_interface.monitoring.pipeline_metrics import PipelineMetrics


@pytest.fixture(autouse=True)
def _reset_correlation_id() -> None:
    """Ensure each test starts with a fresh correlation ID and does not leak state."""
    set_correlation_id(uuid.uuid4().hex[:12])
    yield
    set_correlation_id(uuid.uuid4().hex[:12])


class TestJSONFormatter:
    def test_produces_valid_json(self):
        formatter = JSONFormatter(agent_id="test")
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="hello world",
            args=None,
            exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["msg"] == "hello world"
        assert parsed["level"] == "INFO"
        assert parsed["agent_id"] == "test"
        assert "ts" in parsed
        assert "correlation" in parsed
        assert "pid" in parsed

    def test_includes_exception(self):
        formatter = JSONFormatter()
        try:
            raise ValueError("boom")
        except ValueError:
            record = logging.LogRecord(
                name="test",
                level=logging.ERROR,
                pathname="",
                lineno=0,
                msg="error occurred",
                args=None,
                exc_info=True,
            )
            import sys
            record.exc_info = sys.exc_info()

        output = formatter.format(record)
        parsed = json.loads(output)
        assert "exception" in parsed
        assert "ValueError" in parsed["exception"]

    def test_correlation_id(self):
        set_correlation_id("abc123")
        assert get_correlation_id() == "abc123"

        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="",
            lineno=0, msg="x", args=None, exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["correlation"] == "abc123"


class TestPipelineMetrics:
    def test_counters(self, tmp_path: Path):
        m = PipelineMetrics(path=str(tmp_path / "__test_metrics.json"))
        m.inc_captured()
        m.inc_captured()
        m.inc_parsed()
        m.inc_queued()
        m.inc_dropped()
        m.inc_processed()
        m.inc_validation_error()

        snap = m.snapshot()
        assert snap["counters"]["captured"] == 2
        assert snap["counters"]["parsed"] == 1
        assert snap["counters"]["queued"] == 1
        assert snap["counters"]["dropped"] == 1
        assert snap["counters"]["validation_errors"] == 1

    def test_latency_percentiles(self, tmp_path: Path):
        m = PipelineMetrics(path=str(tmp_path / "__test_metrics.json"))
        for i in range(100):
            m.record_latency("queue_to_bronze", float(i))

        snap = m.snapshot()
        lat = snap["latency_us"]["queue_to_bronze"]
        assert lat["p50"] > 0
        assert lat["p95"] > lat["p50"]
        assert lat["p99"] >= lat["p95"]

    def test_error_recording(self, tmp_path: Path):
        m = PipelineMetrics(path=str(tmp_path / "__test_metrics.json"))
        m.record_error("pydantic", "validation failed")
        snap = m.snapshot()
        assert len(snap["recent_errors"]) == 1
        assert snap["recent_errors"][0]["component"] == "pydantic"

    def test_flush_creates_file(self, tmp_data_dir: Path):
        path = str(tmp_data_dir / "_metrics.json")
        m = PipelineMetrics(path=path)
        m.inc_processed()
        m.flush()

        data = json.loads(Path(path).read_text())
        assert data["counters"]["processed"] == 1
        assert "uptime_sec" in data

    def test_queue_depth(self, tmp_path: Path):
        m = PipelineMetrics(path=str(tmp_path / "__test_metrics.json"))
        m.set_queue_depth(42)
        snap = m.snapshot()
        assert snap["queue_depth"] == 42
