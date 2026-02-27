"""Tests for silver_transform() ? the Bronze-to-Silver quality gate."""

from __future__ import annotations

from datetime import datetime, date, timezone

from network_interface.storage.streaming_pipeline import silver_transform, PortCategory


class TestSilverTransform:
    def test_valid_record(self, sample_bronze_record):
        result = silver_transform(sample_bronze_record)
        assert result is not None
        assert isinstance(result["event_ts"], datetime)
        assert isinstance(result["event_date"], date)
        assert result["protocol"] == "TCP"
        assert result["src_ip"] == "192.168.1.10"
        assert result["src_network"] == "192.168.1.0/24"

    def test_missing_timestamp_returns_none(self, sample_bronze_record):
        sample_bronze_record.pop("timestamp")
        assert silver_transform(sample_bronze_record) is None

    def test_empty_timestamp_returns_none(self, sample_bronze_record):
        sample_bronze_record["timestamp"] = ""
        assert silver_transform(sample_bronze_record) is None

    def test_unparseable_timestamp_returns_none(self, sample_bronze_record):
        sample_bronze_record["timestamp"] = "not-a-date"
        assert silver_transform(sample_bronze_record) is None

    def test_naive_timestamp_gets_utc(self, sample_bronze_record):
        sample_bronze_record["timestamp"] = "2026-01-15T12:30:00"
        result = silver_transform(sample_bronze_record)
        assert result is not None
        assert result["event_ts"].tzinfo == timezone.utc

    def test_aware_timestamp_preserved(self, sample_bronze_record):
        sample_bronze_record["timestamp"] = "2026-01-15T12:30:00+02:00"
        result = silver_transform(sample_bronze_record)
        assert result is not None
        assert result["event_ts"].tzinfo is not None

    def test_event_hour_truncated(self, sample_bronze_record):
        sample_bronze_record["timestamp"] = "2026-03-10T14:45:30.123456+00:00"
        result = silver_transform(sample_bronze_record)
        assert result["event_hour"].minute == 0
        assert result["event_hour"].second == 0
        assert result["event_hour"].microsecond == 0

    def test_protocol_uppercased(self, sample_bronze_record):
        sample_bronze_record["protocol"] = "tcp"
        result = silver_transform(sample_bronze_record)
        assert result["protocol"] == "TCP"

    def test_missing_protocol_defaults_to_other(self, sample_bronze_record):
        sample_bronze_record.pop("protocol")
        result = silver_transform(sample_bronze_record)
        assert result["protocol"] == "OTHER"

    def test_subnet_calculation_ipv4(self, sample_bronze_record):
        sample_bronze_record["src_ip"] = "10.20.30.40"
        result = silver_transform(sample_bronze_record)
        assert result["src_network"] == "10.20.30.0/24"

    def test_subnet_non_ipv4_passthrough(self, sample_bronze_record):
        sample_bronze_record["src_ip"] = "invalid"
        result = silver_transform(sample_bronze_record)
        assert result["src_network"] == "invalid"

    def test_optional_fields_none(self, sample_bronze_record):
        sample_bronze_record["src_port"] = None
        sample_bronze_record["dst_port"] = None
        sample_bronze_record["ttl"] = None
        sample_bronze_record["flags"] = None
        result = silver_transform(sample_bronze_record)
        assert result is not None
        assert result["src_port"] is None

    def test_ingested_at_is_utc(self, sample_bronze_record):
        result = silver_transform(sample_bronze_record)
        assert result["ingested_at"].tzinfo == timezone.utc

    def test_icmp_no_ports_returns_silver_with_none_ports(self, sample_bronze_record):
        """ICMP has no transport ports; silver_transform must accept None ports."""
        sample_bronze_record["protocol"] = "ICMP"
        sample_bronze_record["src_port"] = None
        sample_bronze_record["dst_port"] = None
        result = silver_transform(sample_bronze_record)
        assert result is not None
        assert result["protocol"] == "ICMP"
        assert result["src_port"] is None
        assert result["dst_port"] is None
        assert "src_network" in result

    def test_port_category_well_known(self, sample_bronze_record):
        sample_bronze_record["dst_port"] = 443
        result = silver_transform(sample_bronze_record)
        assert result is not None
        assert result["port_category"] == PortCategory.WELL_KNOWN

    def test_port_category_ephemeral(self, sample_bronze_record):
        sample_bronze_record["dst_port"] = 55000
        result = silver_transform(sample_bronze_record)
        assert result is not None
        assert result["port_category"] == PortCategory.EPHEMERAL

    def test_port_category_os_noise(self, sample_bronze_record):
        sample_bronze_record["dst_port"] = 1900
        result = silver_transform(sample_bronze_record)
        assert result is not None
        assert result["port_category"] == PortCategory.OS_NOISE

    def test_port_category_unknown_when_no_ports(self, sample_bronze_record):
        sample_bronze_record["src_port"] = None
        sample_bronze_record["dst_port"] = None
        result = silver_transform(sample_bronze_record)
        assert result is not None
        assert result["port_category"] == PortCategory.UNKNOWN
