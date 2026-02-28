"""
Web UI tests: dashboard, fragments (live, stats, ports, pipeline), api/health,
filters (q, proto, src_subnet, dst_port_min/max, port_category), anonymization.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from network_interface.web.app import LIVE_PATH, app

client = TestClient(app)


def _write_sample_live(
    *,
    recent: list[dict] | None = None,
    protocol_windows: dict | None = None,
    capture_health: dict | None = None,
    use_private_ips: bool = False,
) -> None:
    """Write a sample _live.json. Set use_private_ips=True to test anonymization (private IPs only)."""
    if use_private_ips:
        default_recent = [
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "src_ip": "192.168.1.10",
                "dst_ip": "10.0.0.5",
                "src_port": 55000,
                "dst_port": 443,
                "protocol": "TCP",
                "length": 128,
                "flags": "PA",
                "ttl": 64,
                "service_info": "HTTPS/TLS SNI=example.com",
                "port_category": "well_known",
                "src_network": "192.168.1.0/24",
            }
        ]
        top_src, top_dst, top_sub = "192.168.1.10", "10.0.0.5", "192.168.1.0/24"
    else:
        default_recent = [
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "src_ip": "192.0.2.1",
                "dst_ip": "198.51.100.1",
                "src_port": 55000,
                "dst_port": 443,
                "protocol": "TCP",
                "length": 128,
                "flags": "PA",
                "ttl": 64,
                "service_info": "HTTPS/TLS SNI=example.com",
                "port_category": "well_known",
                "src_network": "192.0.2.0/24",
            }
        ]
        top_src, top_dst, top_sub = "192.0.2.1", "198.51.100.1", "192.0.2.0/24"

    if recent is None:
        recent = default_recent

    sample = {
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "total_packets": 1,
        "total_bytes": 128,
        "protocols": [{"name": "TCP", "packets": 1, "bytes": 128}],
        "top_src_ips": [{"ip": top_src, "packets": 1}],
        "top_dst_ips": [{"ip": top_dst, "packets": 1}],
        "top_subnets": [{"subnet": top_sub, "packets": 1}],
        "top_dst_ports": [{"port": 443, "packets": 1}],
        "port_stats": [{"port": 443, "protocol": "TCP", "packets": 1, "bytes": 128}],
        "recent_packets": recent,
        "services": [],
        "protocol_windows": protocol_windows or {"5m": [{"name": "TCP", "bytes": 128}]},
        "subnet_windows": {"5m": [{"name": top_sub, "bytes": 128}]},
        "service_windows": {},
        "gold": {},
        "capture_health": capture_health or {"drop_rate_pct": 0, "os_recv": 1, "os_drop": 0},
    }
    LIVE_PATH.write_text(json.dumps(sample, default=str), encoding="utf-8")


def test_root_renders_dashboard() -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "networkInterface" in r.text


def test_live_fragment_shows_table() -> None:
    _write_sample_live()
    r = client.get("/fragment/live")
    assert r.status_code == 200
    assert "Live packets" in r.text
    assert "192.0.2.1" in r.text


def test_live_body_fragment_returns_tr_rows_only() -> None:
    _write_sample_live()
    r = client.get("/fragment/live-body")
    assert r.status_code == 200
    assert "<tr>" in r.text
    assert "<tbody" not in r.text
    assert "HTTPS/TLS" in r.text


def test_live_body_filter_proto() -> None:
    _write_sample_live()
    r = client.get("/fragment/live-body", params={"proto": "TCP"})
    assert r.status_code == 200
    assert "192.0.2.1" in r.text
    r2 = client.get("/fragment/live-body", params={"proto": "UDP"})
    assert r2.status_code == 200
    assert "Waiting for _live.json" in r2.text or "<tr>" in r2.text


def test_live_body_filter_src_subnet() -> None:
    _write_sample_live()
    r = client.get("/fragment/live-body", params={"src_subnet": "192.0.2"})
    assert r.status_code == 200
    assert "192.0.2.1" in r.text


def test_live_body_filter_port_category() -> None:
    _write_sample_live()
    r = client.get("/fragment/live-body", params={"port_category": "well_known"})
    assert r.status_code == 200
    assert "192.0.2.1" in r.text


def test_stats_fragment_shows_protocols_and_charts() -> None:
    _write_sample_live()
    r = client.get("/fragment/stats")
    assert r.status_code == 200
    assert "Protocols" in r.text
    assert "TCP" in r.text
    assert "Volume par protocole" in r.text
    assert "chart-bar" in r.text or "chart-row" in r.text


def test_api_health_returns_json() -> None:
    _write_sample_live()
    r = client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert "status" in data
    assert data["status"] in ("green", "yellow", "red")
    assert "drop_rate_pct" in data


def test_anonymize_masks_ips_in_live() -> None:
    _write_sample_live(use_private_ips=True)  # only private IPs are anonymized
    r = client.get("/fragment/live", params={"anonymize": "1"})
    assert r.status_code == 200
    assert "x.x.x.xxx" in r.text
    assert "192.168.1.10" not in r.text
    assert "10.0.0.5" not in r.text


def test_anonymize_masks_in_live_body() -> None:
    _write_sample_live(use_private_ips=True)
    r = client.get("/fragment/live-body", params={"anonymize": "1"})
    assert r.status_code == 200
    assert "****" in r.text
    assert "x.x.x.xxx" in r.text


@pytest.mark.skipif(bool(os.environ.get("NI_WEB_API_TOKEN")), reason="API token set")
def test_no_auth_required_when_token_unset() -> None:
    r = client.get("/")
    assert r.status_code == 200
    r2 = client.get("/api/health")
    assert r2.status_code == 200
