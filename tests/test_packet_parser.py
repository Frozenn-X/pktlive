"""Tests for parse_raw_packet() — dpkt-based packet parsing."""

from __future__ import annotations

import socket
from datetime import datetime

import dpkt

from network_interface.capture.capture_agent import PacketRecord, parse_raw_packet, _tcp_flags_str
from network_interface.storage.streaming_pipeline import silver_transform


def _build_eth_ip_tcp(
    src_ip: str = "192.168.1.1",
    dst_ip: str = "10.0.0.1",
    sport: int = 12345,
    dport: int = 443,
    flags: int = dpkt.tcp.TH_ACK,
    payload: bytes = b"hello",
) -> bytes:
    """Build a raw Ethernet frame with IP/TCP."""
    tcp = dpkt.tcp.TCP(sport=sport, dport=dport, flags=flags, data=payload, seq=0, off=5)
    ip = dpkt.ip.IP(
        src=socket.inet_aton(src_ip),
        dst=socket.inet_aton(dst_ip),
        p=6,
        data=tcp,
        len=20 + len(bytes(tcp)),
    )
    ip.data = tcp
    eth = dpkt.ethernet.Ethernet(
        dst=b"\xff" * 6,
        src=b"\x00" * 6,
        type=dpkt.ethernet.ETH_TYPE_IP,
        data=ip,
    )
    return bytes(eth)


def _build_eth_ip_udp(
    src_ip: str = "192.168.1.1",
    dst_ip: str = "10.0.0.1",
    sport: int = 54321,
    dport: int = 53,
    payload: bytes = b"dns",
) -> bytes:
    udp = dpkt.udp.UDP(sport=sport, dport=dport, data=payload)
    udp.ulen = 8 + len(payload)
    ip = dpkt.ip.IP(
        src=socket.inet_aton(src_ip),
        dst=socket.inet_aton(dst_ip),
        p=17,
        data=udp,
        len=20 + len(bytes(udp)),
    )
    ip.data = udp
    eth = dpkt.ethernet.Ethernet(
        dst=b"\xff" * 6,
        src=b"\x00" * 6,
        type=dpkt.ethernet.ETH_TYPE_IP,
        data=ip,
    )
    return bytes(eth)


class TestParseRawPacket:
    def test_tcp_packet(self):
        raw = _build_eth_ip_tcp()
        result = parse_raw_packet(raw)
        assert result is not None
        assert result["protocol"] == "TCP"
        assert result["src_ip"] == "192.168.1.1"
        assert result["dst_ip"] == "10.0.0.1"
        assert result["src_port"] == 12345
        assert result["dst_port"] == 443

    def test_udp_packet(self):
        raw = _build_eth_ip_udp()
        result = parse_raw_packet(raw)
        assert result is not None
        assert result["protocol"] == "UDP"
        assert result["src_port"] == 54321
        assert result["dst_port"] == 53

    def test_too_short_returns_none(self):
        assert parse_raw_packet(b"\x00" * 10) is None

    def test_non_ip_returns_none(self):
        eth = dpkt.ethernet.Ethernet(
            dst=b"\xff" * 6,
            src=b"\x00" * 6,
            type=dpkt.ethernet.ETH_TYPE_ARP,
            data=b"\x00" * 28,
        )
        assert parse_raw_packet(bytes(eth)) is None

    def test_tcp_flags_string(self):
        assert _tcp_flags_str(dpkt.tcp.TH_SYN | dpkt.tcp.TH_ACK) == "SA"
        assert _tcp_flags_str(dpkt.tcp.TH_FIN) == "F"
        assert _tcp_flags_str(0) == ""

    def test_result_has_timestamp(self):
        raw = _build_eth_ip_tcp()
        result = parse_raw_packet(raw)
        assert "timestamp" in result
        # Ensure the timestamp is a valid ISO-8601 string.
        ts = result["timestamp"]
        parsed = datetime.fromisoformat(ts)
        assert isinstance(parsed, datetime)

    def test_result_has_ttl_and_length(self):
        raw = _build_eth_ip_tcp()
        result = parse_raw_packet(raw)
        assert "ttl" in result
        assert "length" in result
        assert result["length"] > 0

    def test_icmp_without_ports_flows_to_silver(self):
        # Build an ICMP packet with no TCP/UDP ports.
        icmp = dpkt.icmp.ICMP(type=8, data=b"ping")
        ip = dpkt.ip.IP(
            src=socket.inet_aton("192.168.1.1"),
            dst=socket.inet_aton("10.0.0.1"),
            p=1,
            data=icmp,
            len=20 + len(bytes(icmp)),
        )
        ip.data = icmp
        eth = dpkt.ethernet.Ethernet(
            dst=b"\xff" * 6,
            src=b"\x00" * 6,
            type=dpkt.ethernet.ETH_TYPE_IP,
            data=ip,
        )
        raw = bytes(eth)

        parsed = parse_raw_packet(raw)
        assert parsed is not None
        assert parsed["protocol"] == "ICMP"
        assert "src_port" not in parsed
        assert "dst_port" not in parsed

        pkt = PacketRecord(**parsed)
        silver = silver_transform(pkt.model_dump())
        assert silver is not None
        assert silver["protocol"] == "ICMP"
        assert silver["src_port"] is None
        assert silver["dst_port"] is None

    def test_malformed_tls_does_not_crash(self):
        # Too short / invalid TLS record: should not raise and no service_info.
        payload = b"\x16\x03"
        raw = _build_eth_ip_tcp(dport=443, payload=payload)
        result = parse_raw_packet(raw)
        assert result is not None
        assert "service_info" not in result or result["service_info"] is None

    def test_empty_payload_no_service_info(self):
        raw = _build_eth_ip_tcp(dport=443, payload=b"")
        result = parse_raw_packet(raw)
        assert result is not None
        assert "service_info" not in result or result["service_info"] is None

    def test_unknown_protocol_label(self):
        # Craft an IP packet with an unknown protocol number (e.g. 47 = GRE)
        ip = dpkt.ip.IP(
            src=socket.inet_aton("192.168.1.1"),
            dst=socket.inet_aton("10.0.0.1"),
            p=47,
            len=20 + 0,
            data=b"",
        )
        eth = dpkt.ethernet.Ethernet(
            dst=b"\xff" * 6,
            src=b"\x00" * 6,
            type=dpkt.ethernet.ETH_TYPE_IP,
            data=ip,
        )
        raw = bytes(eth)
        result = parse_raw_packet(raw)
        assert result is not None
        assert result["protocol"] == "PROTO_47"

    def test_https_service_info_detected(self):
        # Minimal TLS ClientHello-like payload: type=22, handshake type=1.
        payload = b"\x16\x03\x01\x00\x20\x01" + b"\x00" * 40
        raw = _build_eth_ip_tcp(dport=443, payload=payload)
        result = parse_raw_packet(raw)
        assert result is not None
        svc = result.get("service_info")
        assert svc is not None
        assert "HTTPS/TLS" in svc

    def test_http_service_info_detected(self):
        payload = b"GET /foo HTTP/1.1\r\nHost: example.com\r\n\r\n"
        raw = _build_eth_ip_tcp(dport=80, payload=payload)
        result = parse_raw_packet(raw)
        assert result is not None
        svc = result.get("service_info")
        assert svc is not None
        assert "HTTP" in svc
        assert "example.com" in svc

    def test_dns_service_info_detected(self):
        dns = dpkt.dns.DNS()
        dns.qr = dpkt.dns.DNS_Q
        dns.opcode = dpkt.dns.DNS_QUERY
        dns.qd = [dpkt.dns.DNS.Q(name="example.com", type=dpkt.dns.DNS_A)]
        payload = bytes(dns)
        raw = _build_eth_ip_udp(dport=53, payload=payload)
        result = parse_raw_packet(raw)
        assert result is not None
        svc = result.get("service_info")
        assert svc is not None
        assert "DNS" in svc
        assert "example.com" in svc

    def test_ssh_service_info_banner(self):
        payload = b"SSH-2.0-OpenSSH_8.9\r\n"
        raw = _build_eth_ip_tcp(dport=22, payload=payload)
        result = parse_raw_packet(raw)
        assert result is not None
        svc = result.get("service_info")
        assert svc is not None
        assert "SSH-2.0-OpenSSH" in svc
