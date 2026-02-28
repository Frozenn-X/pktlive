"""Tests for PacketRecord validation and integration with silver_transform."""

from __future__ import annotations

import pytest

from network_interface.capture.capture_agent import PacketRecord
from network_interface.storage.streaming_pipeline import silver_transform


class TestPacketRecordValidation:
    def test_valid_packet_record(self):
        rec = PacketRecord(
            timestamp="2026-02-25T12:00:00+00:00",
            src_ip="192.168.1.1",
            dst_ip="10.0.0.1",
            src_port=12345,
            dst_port=443,
            protocol="tcp",
            length=100,
            ttl=64,
            flags="SA",
        )
        assert rec.protocol == "TCP"
        assert rec.length == 100

    def test_negative_length_rejected(self):
        with pytest.raises(ValueError):
            PacketRecord(
                timestamp="2026-02-25T12:00:00+00:00",
                src_ip="192.168.1.1",
                dst_ip="10.0.0.1",
                length=-1,
            )

    def test_wrong_type_for_port_rejected(self):
        with pytest.raises((TypeError, ValueError)):
            PacketRecord(
                timestamp="2026-02-25T12:00:00+00:00",
                src_ip="192.168.1.1",
                dst_ip="10.0.0.1",
                src_port="not-an-int",  # type: ignore[arg-type]
                length=42,
            )


class TestPacketRecordToSilverIntegration:
    def test_packet_record_chain_to_silver(self):
        """PacketRecord(**raw) -> model_dump() -> silver_transform() end-to-end."""
        rec = PacketRecord(
            timestamp="2026-02-25T12:34:56+00:00",
            src_ip="192.168.1.10",
            dst_ip="10.0.0.1",
            src_port=54321,
            dst_port=443,
            protocol="tcp",
            length=128,
            ttl=64,
            flags="PA",
        )

        payload = rec.model_dump()
        silver = silver_transform(payload)
        assert silver is not None
        assert silver["src_ip"] == "192.168.1.10"
        assert silver["dst_ip"] == "10.0.0.1"
        assert silver["protocol"] == "TCP"
        assert silver["src_port"] == 54321
        assert silver["dst_port"] == 443

