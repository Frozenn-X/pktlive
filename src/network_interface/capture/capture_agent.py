"""
capture_agent.py — Production Edge Agent for Network Analytics (Streaming Medallion)

Architecture
────────────
  [NIC] ──▶ OS-native capture (dedicated thread, GIL released in C/kernel)
               │
               │  Linux  : AF_PACKET raw socket   (zero-copy kernel, stdlib)
               │  Windows: Npcap via ctypes        (direct C binding, no wrapper)
               │
               ▼
         ThreadPoolExecutor (N workers)
               │  dpkt parsing : struct-based, 130x faster than scapy
               ▼
         mp.Queue (bounded, 200k, back-pressure)
               │
               ▼
         DataSinkProcess (dedicated OS process, bypasses GIL)
               ├── BronzeStore  ──▶  ./data/bronze/data/dt=YYYY-MM-DD/hr=HH/*.ndjson
               ├── SilverStore  ──▶  ./data/silver/event_date=YYYY-MM-DD/*.parquet
               └── GoldAccum    ──▶  ./data/gold/event_date=YYYY-MM-DD/*.parquet
"""

from __future__ import annotations

import abc
import ctypes
import ctypes.util
import json
import queue
import logging
import multiprocessing as mp
import os
import platform
import signal
import socket
import struct
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import dpkt
import psutil
from pydantic import BaseModel, Field, field_validator

from ..storage.bronze_store import BronzeConfig, BronzeStore
from ..storage.streaming_pipeline import (
    GoldAccumulator, LiveSnapshot, SilverStore, silver_transform,
    compact_parquet_dir, enforce_retention,
    SILVER_SCHEMA, GOLD_SCHEMA, SILVER_DIR, GOLD_DIR,
)
from ..monitoring.structured_log import configure_logging, set_correlation_id
from ..monitoring.pipeline_metrics import PipelineMetrics

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
_QUEUE_MAXSIZE: int = 200_000
_PARSER_WORKERS: int = max(2, (os.cpu_count() or 4) - 2)
_SNAP_LEN: int = 65535
from .._paths import BRONZE_DIR

_AGENT_ID: str = uuid.uuid4().hex[:12]

logger = logging.getLogger("capture_agent")


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic schema — validated record written to Bronze
# ─────────────────────────────────────────────────────────────────────────────
class PacketRecord(BaseModel):
    """Schema-enforced packet summary destined for Bronze NDJSON files."""

    timestamp: str = Field(description="ISO-8601 UTC capture time")
    src_ip: str = Field(default="0.0.0.0")
    dst_ip: str = Field(default="0.0.0.0")
    src_port: Optional[int] = Field(default=None)
    dst_port: Optional[int] = Field(default=None)
    protocol: str = Field(default="OTHER")
    length: int = Field(ge=0)
    ttl: Optional[int] = Field(default=None)
    flags: Optional[str] = Field(default=None)
    agent_id: str = Field(default=_AGENT_ID)
    # Best-effort application/service hint extracted from payload for
    # well-known ports (HTTP, TLS, DNS, SSH, SMTP, ...).
    service_info: Optional[str] = Field(default=None)

    @field_validator("protocol", mode="before")
    @classmethod
    def _normalise_protocol(cls, v: Any) -> str:
        return str(v).upper() if v else "OTHER"


# ─────────────────────────────────────────────────────────────────────────────
# OS & NIC Detection
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PlatformInfo:
    os_name: str
    nic_name: str
    nic_ip: str
    npcap_device: Optional[str] = None


def detect_platform() -> PlatformInfo:
    """Auto-detect OS and the most-active NIC with a routable IPv4 address."""
    os_name = platform.system()
    if os_name not in ("Linux", "Windows"):
        raise EnvironmentError(f"Unsupported OS: {os_name}")

    best_nic: Optional[str] = None
    best_ip: str = "0.0.0.0"
    best_bytes: int = -1

    net_io = psutil.net_io_counters(pernic=True)
    addrs = psutil.net_if_addrs()

    for nic, counters in net_io.items():
        nic_addrs = addrs.get(nic, [])
        ipv4 = next(
            (a.address for a in nic_addrs if a.family.name == "AF_INET" and not a.address.startswith("127.")),
            None,
        )
        if ipv4 is None:
            continue
        total = counters.bytes_sent + counters.bytes_recv
        if total > best_bytes:
            best_bytes = total
            best_nic = nic
            best_ip = ipv4

    if best_nic is None:
        raise EnvironmentError("No active NIC with a routable IPv4 address found.")

    npcap_device: Optional[str] = None
    if os_name == "Windows":
        npcap_device = _resolve_npcap_device(best_nic)

    logger.info("Platform: %s | NIC: %s | IP: %s", os_name, best_nic, best_ip)
    return PlatformInfo(os_name=os_name, nic_name=best_nic, nic_ip=best_ip, npcap_device=npcap_device)


def _resolve_npcap_device(friendly_name: str) -> str:
    """
    Map a Windows friendly NIC name (e.g. 'Ethernet') to the Npcap device
    path (e.g. '\\Device\\NPF_{GUID}') by matching the GUID found in the
    registry adapter list.
    """
    import winreg  # type: ignore[import-not-found]

    target_guid: Optional[str] = None
    reg_path = r"SYSTEM\CurrentControlSet\Control\Network\{4D36E972-E325-11CE-BFC1-08002BE10318}"

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path) as net_key:
            i = 0
            while True:
                try:
                    guid = winreg.EnumKey(net_key, i)
                    conn_path = f"{reg_path}\\{guid}\\Connection"
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, conn_path) as conn_key:
                        name, _ = winreg.QueryValueEx(conn_key, "Name")
                        if name == friendly_name:
                            target_guid = guid
                            break
                except OSError:
                    pass
                i += 1
    except OSError:
        pass

    if target_guid:
        device = f"\\Device\\NPF_{target_guid}"
        logger.info("Resolved NIC '%s' → %s", friendly_name, device)
        return device

    fallback = f"\\Device\\NPF_{friendly_name}"
    logger.warning("Could not resolve NIC '%s' via registry; using fallback: %s", friendly_name, fallback)
    return fallback


# ─────────────────────────────────────────────────────────────────────────────
# Capture Backends — Abstract base + Linux/Windows implementations
# ─────────────────────────────────────────────────────────────────────────────
class CaptureBackend(abc.ABC):
    """Interface commune pour les backends de capture natifs."""

    @abc.abstractmethod
    def open(self) -> None: ...
    @abc.abstractmethod
    def recv(self) -> Optional[bytes]: ...
    @abc.abstractmethod
    def close(self) -> None: ...
    @abc.abstractmethod
    def fileno(self) -> int: ...

    def stats(self) -> dict[str, int]:
        """
        Optional OS/driver-level stats.

        Implementations should return a dict with:
            - os_packets_recv: total packets seen by the driver/OS
            - os_packets_drop: packets dropped before userspace
            - os_packets_ifdrop: interface-level drops (if available)

        The default implementation returns an empty dict.
        """
        return {}

    def set_filter(self, bpf: Optional[str]) -> None:
        """
        Optional: apply a capture filter at backend level.

        Backends that support BPF/pcap filters can override this. The default
        implementation is a no-op.
        """
        return


class LinuxCapture(CaptureBackend):
    """
    AF_PACKET raw socket — Linux kernel native.
    Zero-copy pour la réception, le GIL est relâché pendant recvfrom().
    Nécessite CAP_NET_RAW ou root.
    """

    ETH_P_ALL: int = 0x0003

    def __init__(self, nic_name: str, snap_len: int = _SNAP_LEN) -> None:
        self._nic = nic_name
        self._snap_len = snap_len
        self._sock: Optional[socket.socket] = None

    def open(self) -> None:
        self._sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(self.ETH_P_ALL))
        self._sock.bind((self._nic, 0))
        self._sock.settimeout(1.0)
        logger.info("LinuxCapture: AF_PACKET bound to %s", self._nic)

    def recv(self) -> Optional[bytes]:
        if self._sock is None:
            return None
        try:
            data, _ = self._sock.recvfrom(self._snap_len)
            return data
        except socket.timeout:
            return None

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def fileno(self) -> int:
        if self._sock is None:
            raise RuntimeError("Socket not open")
        return self._sock.fileno()

    def set_filter(self, bpf: Optional[str]) -> None:
        """
        Placeholder for future AF_PACKET BPF support.

        Today we rely on OS / external tools (iptables, tc, tcpdump) for
        kernel-level filtering on Linux. The method exists so that callers
        can configure filters in a backend-agnostic way.
        """
        if not bpf:
            return
        logger.info("LinuxCapture: BPF filter requested (%s) — not yet implemented at kernel level.", bpf)

    def stats(self) -> dict[str, int]:
        """
        Best-effort Linux stats using psutil.

        Returns kernel counters for this NIC (packets + drops). If anything
        fails, an empty dict is returned and ignored by callers.
        """
        try:
            import psutil  # type: ignore[import-not-found]
        except Exception:
            return {}

        try:
            io = psutil.net_io_counters(pernic=True).get(self._nic)
            if io is None:
                return {}
            return {
                "os_packets_recv": int(getattr(io, "packets_recv", 0)),
                "os_packets_drop": int(getattr(io, "dropin", 0)),
                "os_packets_ifdrop": int(getattr(io, "dropout", 0)),
            }
        except Exception:
            return {}


class WindowsCapture(CaptureBackend):
    """
    Npcap/WinPcap via ctypes — appels C directs, pas de wrapper Python.
    Charge wpcap.dll (Npcap), ouvre le device en mode promiscuous,
    et lit les paquets via pcap_next_ex().
    """

    PCAP_OPENFLAG_PROMISCUOUS: int = 1

    def __init__(self, device: str, snap_len: int = _SNAP_LEN) -> None:
        self._device = device
        self._snap_len = snap_len
        self._pcap: Any = None
        self._wpcap: Any = None

    def open(self) -> None:
        dll_path = self._find_wpcap_dll()
        self._wpcap = ctypes.cdll.LoadLibrary(dll_path)
        self._configure_ctypes()

        errbuf = ctypes.create_string_buffer(256)
        self._pcap = self._wpcap.pcap_open_live(
            self._device.encode("utf-8"),
            self._snap_len,
            self.PCAP_OPENFLAG_PROMISCUOUS,
            100,  # read timeout ms
            errbuf,
        )
        if not self._pcap:
            raise RuntimeError(f"pcap_open_live failed: {errbuf.value.decode()}")
        logger.info("WindowsCapture: Npcap opened on %s", self._device)

    def recv(self) -> Optional[bytes]:
        header = ctypes.POINTER(_PcapPkthdr)()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        result = self._wpcap.pcap_next_ex(self._pcap, ctypes.byref(header), ctypes.byref(data))
        if result == 1:
            return bytes(data[: header.contents.caplen])
        return None

    def close(self) -> None:
        if self._pcap and self._wpcap:
            self._wpcap.pcap_close(self._pcap)
            self._pcap = None

    def fileno(self) -> int:
        return -1

    def set_filter(self, bpf: Optional[str]) -> None:
        """
        Apply a classic pcap filter expression at the driver level.

        Example: "tcp port 80 or udp port 53"
        """
        if not bpf or not self._pcap or not self._wpcap:
            return
        try:
            class _BpfProgram(ctypes.Structure):
                _fields_ = [
                    ("bf_len", ctypes.c_uint),
                    ("bf_insns", ctypes.c_void_p),
                ]

            prog = _BpfProgram()
            rc = self._wpcap.pcap_compile(
                self._pcap,
                ctypes.byref(prog),
                bpf.encode("ascii"),
                1,  # optimize
                0xFFFFFFFF,  # netmask: "any"
            )
            if rc != 0:
                logger.warning("WindowsCapture: pcap_compile failed for filter %r", bpf)
                return
            try:
                rc = self._wpcap.pcap_setfilter(self._pcap, ctypes.byref(prog))
                if rc != 0:
                    logger.warning("WindowsCapture: pcap_setfilter failed for filter %r", bpf)
                else:
                    logger.info("WindowsCapture: applied BPF filter: %s", bpf)
            finally:
                try:
                    self._wpcap.pcap_freecode(ctypes.byref(prog))
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("WindowsCapture: failed to apply BPF filter %r: %s", bpf, exc)

    def stats(self) -> dict[str, int]:
        """
        Return pcap-level stats from Npcap/WinPcap via pcap_stats().

        The struct pcap_stat provides:
            ps_recv   – packets received by the filter
            ps_drop   – packets dropped by the driver
            ps_ifdrop – packets dropped by the interface
        """
        if not self._pcap or not self._wpcap:
            return {}
        try:
            class _PcapStat(ctypes.Structure):
                _fields_ = [
                    ("ps_recv", ctypes.c_uint),
                    ("ps_drop", ctypes.c_uint),
                    ("ps_ifdrop", ctypes.c_uint),
                ]

            stats = _PcapStat()
            rc = self._wpcap.pcap_stats(self._pcap, ctypes.byref(stats))
            if rc != 0:
                return {}
            return {
                "os_packets_recv": int(stats.ps_recv),
                "os_packets_drop": int(stats.ps_drop),
                "os_packets_ifdrop": int(stats.ps_ifdrop),
            }
        except Exception:
            return {}

    def _configure_ctypes(self) -> None:
        wpcap = self._wpcap
        wpcap.pcap_open_live.argtypes = [
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_char_p,
        ]
        wpcap.pcap_open_live.restype = ctypes.c_void_p
        wpcap.pcap_next_ex.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.POINTER(_PcapPkthdr)),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
        ]
        wpcap.pcap_next_ex.restype = ctypes.c_int
        wpcap.pcap_close.argtypes = [ctypes.c_void_p]
        wpcap.pcap_close.restype = None

        # int pcap_stats(pcap_t *p, struct pcap_stat *ps);
        wpcap.pcap_stats.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        wpcap.pcap_stats.restype = ctypes.c_int

        # int pcap_compile(pcap_t *p, struct bpf_program *fp,
        #                  const char *str, int optimize, bpf_u_int32 netmask);
        wpcap.pcap_compile.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        wpcap.pcap_compile.restype = ctypes.c_int

        # int pcap_setfilter(pcap_t *p, struct bpf_program *fp);
        wpcap.pcap_setfilter.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        wpcap.pcap_setfilter.restype = ctypes.c_int

        # void pcap_freecode(struct bpf_program *fp);
        wpcap.pcap_freecode.argtypes = [ctypes.c_void_p]
        wpcap.pcap_freecode.restype = None

    @staticmethod
    def _find_wpcap_dll() -> str:
        sys_root = os.environ.get("SystemRoot", r"C:\Windows")
        search_paths = [
            os.path.join(sys_root, "System32", "Npcap", "wpcap.dll"),
            os.path.join(sys_root, "System32", "wpcap.dll"),
            os.path.join(sys_root, "SysWOW64", "Npcap", "wpcap.dll"),
            os.path.join(sys_root, "SysWOW64", "wpcap.dll"),
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Npcap", "wpcap.dll"),
        ]
        for path in search_paths:
            if os.path.isfile(path):
                logger.info("Found wpcap.dll at: %s", path)
                return path

        via_find = ctypes.util.find_library("wpcap")
        if via_find:
            logger.info("Found wpcap.dll via find_library: %s", via_find)
            return via_find

        searched = "\n  ".join(search_paths)
        raise FileNotFoundError(
            f"wpcap.dll not found. Searched:\n  {searched}\n\n"
            "Install Npcap from https://npcap.com/#download\n"
            "  → Check 'Install Npcap in WinPcap API-compatible mode' during setup.\n"
            "  → Reboot after installation."
        )


class _PcapPkthdr(ctypes.Structure):
    """Mirror of struct pcap_pkthdr from pcap.h."""
    _fields_ = [
        ("tv_sec", ctypes.c_long),
        ("tv_usec", ctypes.c_long),
        ("caplen", ctypes.c_uint),
        ("len", ctypes.c_uint),
    ]


def create_backend(pinfo: PlatformInfo) -> CaptureBackend:
    """Factory: retourne le backend natif optimal pour l'OS détecté."""
    if pinfo.os_name == "Linux":
        return LinuxCapture(pinfo.nic_name)
    if pinfo.os_name == "Windows":
        device = pinfo.npcap_device or pinfo.nic_name
        return WindowsCapture(device)
    raise EnvironmentError(f"No capture backend for OS: {pinfo.os_name}")


# ─────────────────────────────────────────────────────────────────────────────
# Packet Parser — dpkt (struct-based, 130x faster than scapy)
# ─────────────────────────────────────────────────────────────────────────────
_PROTO_MAP: dict[int, str] = {1: "ICMP", 6: "TCP", 17: "UDP"}
_ETH_HEADER_LEN: int = 14


def parse_raw_packet(raw: bytes) -> Optional[dict[str, Any]]:
    """
    O(1) struct-based extraction via dpkt.
    Receives raw Ethernet frame bytes, returns a dict or None for non-IP.
    """
    if len(raw) < _ETH_HEADER_LEN + 20:
        return None

    try:
        eth = dpkt.ethernet.Ethernet(raw)
    except (dpkt.UnpackError, dpkt.NeedData):
        return None

    if not isinstance(eth.data, dpkt.ip.IP):
        return None

    ip: dpkt.ip.IP = eth.data

    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "src_ip": socket.inet_ntoa(ip.src),
        "dst_ip": socket.inet_ntoa(ip.dst),
        "protocol": _PROTO_MAP.get(ip.p, f"PROTO_{ip.p}"),
        "length": len(raw),
        "ttl": ip.ttl,
    }

    if isinstance(ip.data, dpkt.tcp.TCP):
        tcp: dpkt.tcp.TCP = ip.data
        record["src_port"] = tcp.sport
        record["dst_port"] = tcp.dport
        record["flags"] = _tcp_flags_str(tcp.flags)

        # best-effort service detection on TCP payload for well-known ports
        payload = bytes(tcp.data or b"")
        port_for_service = tcp.dport or tcp.sport
        svc = _detect_service("TCP", int(port_for_service), payload)
        if svc:
            record["service_info"] = svc
    elif isinstance(ip.data, dpkt.udp.UDP):
        udp: dpkt.udp.UDP = ip.data
        record["src_port"] = udp.sport
        record["dst_port"] = udp.dport
        payload = bytes(udp.data or b"")
        port_for_service = udp.dport or udp.sport
        svc = _detect_service("UDP", int(port_for_service), payload)
        if svc:
            record["service_info"] = svc

    return record


_TCP_FLAG_NAMES: list[tuple[int, str]] = [
    (dpkt.tcp.TH_FIN, "F"),
    (dpkt.tcp.TH_SYN, "S"),
    (dpkt.tcp.TH_RST, "R"),
    (dpkt.tcp.TH_PUSH, "P"),
    (dpkt.tcp.TH_ACK, "A"),
    (dpkt.tcp.TH_URG, "U"),
    (dpkt.tcp.TH_ECE, "E"),
    (dpkt.tcp.TH_CWR, "C"),
]


def _tcp_flags_str(flags: int) -> str:
    return "".join(ch for mask, ch in _TCP_FLAG_NAMES if flags & mask)


def _detect_service(proto: str, port: int, payload: bytes) -> Optional[str]:
    """
    Lightweight, best-effort service detection for well-known ports.

    This runs only on TCP/UDP and a small set of ports so the overhead stays
    O(1) and negligible compared to parsing the packet itself.
    """
    if not payload:
        return None

    proto = proto.upper()

    # HTTPS / TLS (ClientHello with possible SNI)
    if proto == "TCP" and port == 443 and len(payload) > 5:
        # TLS record type 22 (handshake) + version, then Handshake type 1 (ClientHello)
        try:
            content_type = payload[0]
            handshake_type = payload[5]
        except IndexError:
            content_type = 0
            handshake_type = 0
        if content_type == 0x16 and handshake_type == 0x01:
            hostname = _extract_tls_sni(payload)
            if hostname:
                return f"HTTPS/TLS SNI={hostname}"
            return "HTTPS/TLS"

    # HTTP (80, 8080, 8000 ...)
    if proto == "TCP" and port in (80, 8080, 8000, 8001):
        info = _extract_http_info(payload)
        if info:
            return info

    # DNS
    if proto == "UDP" and port == 53:
        info = _extract_dns_info(payload)
        if info:
            return info

    # SSH banner
    if proto == "TCP" and port == 22 and payload.startswith(b"SSH-"):
        try:
            banner = payload.split(b"\n", 1)[0][:80].decode("ascii", errors="ignore").strip()
        except Exception:
            banner = "SSH"
        return banner or "SSH"

    # SMTP banner
    if proto == "TCP" and port in (25, 587, 465) and payload.startswith(b"220"):
        try:
            line = payload.split(b"\n", 1)[0][:80].decode("ascii", errors="ignore").strip()
        except Exception:
            line = "SMTP"
        return line or "SMTP"

    return None


def _extract_http_info(payload: bytes) -> Optional[str]:
    """
    Try to parse the beginning of an HTTP request or response and summarise it.
    """
    try:
        # Heuristic: if it looks like a request line, use Request, else Response.
        if payload.startswith((b"GET ", b"POST ", b"PUT ", b"DELETE ", b"HEAD ", b"OPTIONS ", b"PATCH ")):
            req = dpkt.http.Request(payload)
            host = req.headers.get("host", "")
            path = req.uri or "/"
            if host:
                return f"HTTP {req.method} {host}{path}"
            return f"HTTP {req.method} {path}"
        else:
            resp = dpkt.http.Response(payload)
            ct = resp.headers.get("content-type", "")
            return f"HTTP {resp.status} {ct}".strip()
    except Exception:
        return None


def _extract_dns_info(payload: bytes) -> Optional[str]:
    """Parse a DNS message and return a short description of the first query."""
    try:
        dns = dpkt.dns.DNS(payload)
    except Exception:
        return None
    if not (dns.qr == dpkt.dns.DNS_Q and dns.qd):
        return None
    q = dns.qd[0]
    name = q.name or ""
    qtype = q.type
    try:
        type_name = dpkt.dns.DNS_QTYPE.get(qtype, str(qtype))
    except Exception:
        type_name = str(qtype)
    if name:
        return f"DNS {type_name} {name}"
    return f"DNS {type_name}"


def _extract_tls_sni(payload: bytes) -> Optional[str]:
    """
    Very small, defensive TLS ClientHello SNI extractor.

    We avoid a full TLS implementation: we scan extensions for server_name.
    If anything looks off, we just return None.
    """
    try:
        # Skip TLS record header (5 bytes) + handshake header (4 bytes)
        # struct: ContentType(1), Version(2), Length(2), HandshakeType(1), Length(3)
        p = memoryview(payload)
        if len(p) < 11:
            return None
        # position after handshake header
        idx = 5 + 4

        # ClientHello fixed part: version(2), random(32), session_id_len(1), session_id, cipher_suites_len(2),
        # cipher_suites, compression_methods_len(1), compression_methods, extensions_len(2), extensions...
        # We walk it carefully, bailing out on malformed values.
        if idx + 2 + 32 + 1 > len(p):
            return None
        idx += 2 + 32  # version + random
        sid_len = p[idx]
        idx += 1 + sid_len
        if idx + 2 > len(p):
            return None
        cs_len = int.from_bytes(p[idx : idx + 2], "big")
        idx += 2 + cs_len
        if idx + 1 > len(p):
            return None
        comp_len = p[idx]
        idx += 1 + comp_len
        if idx + 2 > len(p):
            return None
        ext_total_len = int.from_bytes(p[idx : idx + 2], "big")
        idx += 2
        end_ext = idx + ext_total_len
        if end_ext > len(p):
            return None

        # Walk extensions
        while idx + 4 <= end_ext:
            ext_type = int.from_bytes(p[idx : idx + 2], "big")
            ext_len = int.from_bytes(p[idx + 2 : idx + 4], "big")
            idx += 4
            if idx + ext_len > end_ext:
                break
            if ext_type == 0x00:  # server_name
                # struct: list_len(2), [ name_type(1), name_len(2), name(bytes) ]*
                sn_view = p[idx : idx + ext_len]
                if len(sn_view) < 5:
                    break
                list_len = int.from_bytes(sn_view[0:2], "big")
                pos = 2
                if pos + list_len > len(sn_view):
                    break
                # take first entry
                if pos + 3 > len(sn_view):
                    break
                name_type = sn_view[pos]
                name_len = int.from_bytes(sn_view[pos + 1 : pos + 3], "big")
                pos += 3
                if name_type != 0 or pos + name_len > len(sn_view):
                    break
                hostname_bytes = sn_view[pos : pos + name_len].tobytes()
                return hostname_bytes.decode("idna", errors="ignore")
            idx += ext_len
    except Exception:
        return None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Data Sink Process — dedicated OS process for I/O (bypasses GIL)
# ─────────────────────────────────────────────────────────────────────────────
_COMPACTION_INTERVAL_SEC: float = 600.0
_RETENTION_INTERVAL_SEC: float = 3600.0


class DataSinkProcess:
    """
    Runs in its own OS process.  Drains the mp.Queue, validates records via
    Pydantic, and delegates I/O to BronzeStore which handles partitioned
    writes, manifest tracking, rotation, compaction, and retention.
    """

    def __init__(
        self,
        queue: mp.Queue,  # type: ignore[type-arg]
        bronze_config: BronzeConfig,
        agent_id: str,
        shutdown_event: mp.Event,  # type: ignore[type-arg]
    ) -> None:
        self._queue = queue
        self._bronze_config = bronze_config
        self._agent_id = agent_id
        self._shutdown = shutdown_event

    def run(self) -> None:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        configure_logging(agent_id=self._agent_id, log_file="pipeline.log")
        logger.info("DataSink process started (pid=%d)", os.getpid())

        store = BronzeStore(self._bronze_config, self._agent_id)
        silver_store = SilverStore()
        gold_accum = GoldAccumulator()
        snapshot = LiveSnapshot()
        metrics = PipelineMetrics()
        last_compaction = time.monotonic()
        last_retention = time.monotonic()

        while not self._shutdown.is_set() or not self._queue.empty():
            try:
                metrics.set_queue_depth(self._queue.qsize())
            except NotImplementedError:
                pass

            try:
                raw_dict: dict[str, Any] = self._queue.get(timeout=0.15)
            except queue.Empty:
                silver_store.maybe_flush()
                gold_accum.maybe_flush()
                snapshot.update_gold(gold_accum)
                snapshot.maybe_flush()
                metrics.maybe_flush()
                continue
            except Exception as exc:
                logger.error("DataSink queue get failed: %s", exc, exc_info=True)
                break

            t0 = time.monotonic()
            try:
                record = PacketRecord(**raw_dict)
            except Exception as exc:
                metrics.inc_validation_error()
                metrics.record_error("pydantic", str(exc))
                store.quarantine(
                    raw_data=json.dumps(raw_dict, default=str) if isinstance(raw_dict, dict) else str(raw_dict),
                    reason=str(exc),
                )
                continue

            record_dict = record.model_dump()
            store.write_record(json.dumps(record_dict, default=str))
            t_bronze = time.monotonic()
            metrics.record_latency("queue_to_bronze", (t_bronze - t0) * 1e6)

            silver_now = datetime.now(timezone.utc)
            silver_rec = silver_transform(record_dict, now=silver_now)
            if silver_rec is not None:
                t_silver = time.monotonic()
                metrics.record_latency("bronze_to_silver", (t_silver - t_bronze) * 1e6)
                silver_store.write(silver_rec)
                gold_accum.update(silver_rec)
                snapshot.update(silver_rec)
                metrics.inc_silver()
                metrics.record_latency("silver_to_gold", (time.monotonic() - t_silver) * 1e6)

            metrics.inc_processed()

            silver_store.maybe_flush()
            gold_accum.maybe_flush()
            snapshot.update_gold(gold_accum)
            snapshot.maybe_flush()
            metrics.maybe_flush()

            now = time.monotonic()
            if now - last_compaction >= _COMPACTION_INTERVAL_SEC:
                store.compact()
                compact_parquet_dir(SILVER_DIR, SILVER_SCHEMA)
                compact_parquet_dir(GOLD_DIR, GOLD_SCHEMA)
                last_compaction = now
            if now - last_retention >= _RETENTION_INTERVAL_SEC:
                store.enforce_retention()
                enforce_retention(SILVER_DIR, layer="silver")
                enforce_retention(GOLD_DIR, layer="gold")
                last_retention = now

        silver_store.flush()
        gold_accum.flush()
        snapshot.flush()
        metrics.flush()
        store.seal_active()

        b_stats = store.stats
        s_stats = silver_store.stats
        g_stats = gold_accum.stats
        logger.info(
            "DataSink shutdown | bronze=%d records | silver=%d records (%d flushes) | gold=%d rows (%d flushes) | quarantined=%d",
            b_stats["total_records"],
            s_stats["total_records"],
            s_stats["total_flushes"],
            g_stats["total_records"],
            g_stats["total_flushes"],
            b_stats["quarantined"],
        )

# ─────────────────────────────────────────────────────────────────────────────
# Capture Engine — orchestrator
# ─────────────────────────────────────────────────────────────────────────────
class CaptureEngine:
    """
    Main orchestrator.

    - Creates the OS-native capture backend (AF_PACKET or Npcap).
    - Runs capture in a dedicated thread (blocking recv, GIL released in C).
    - Dispatches raw bytes to ThreadPoolExecutor for dpkt parsing.
    - Parsed dicts are placed on a bounded mp.Queue consumed by DataSinkProcess.
    - Handles SIGTERM / SIGINT for graceful shutdown.
    """

    def __init__(self, platform_info: PlatformInfo, bronze_config: BronzeConfig) -> None:
        self._platform = platform_info
        self._bronze_config = bronze_config
        self._shutdown_event: mp.Event = mp.Event()  # type: ignore[type-arg]
        self._queue: mp.Queue = mp.Queue(maxsize=_QUEUE_MAXSIZE)  # type: ignore[type-arg]
        self._executor = ThreadPoolExecutor(
            max_workers=_PARSER_WORKERS,
            thread_name_prefix="pkt_parser",
        )
        self._backend: Optional[CaptureBackend] = None
        self._capture_thread: Optional[threading.Thread] = None
        self._sink_proc: Optional[mp.Process] = None
        self._packets_seen: int = 0
        self._packets_queued: int = 0
        self._metrics = PipelineMetrics(path="_metrics_capture.json")

    def start(self) -> None:
        self._install_signal_handlers()

        # 1. Launch the I/O sink in a separate OS process (BronzeStore inside)
        sink = DataSinkProcess(self._queue, self._bronze_config, _AGENT_ID, self._shutdown_event)
        self._sink_proc = mp.Process(target=sink.run, name="data_sink", daemon=False)
        self._sink_proc.start()
        logger.info("DataSink process launched (pid=%d)", self._sink_proc.pid)

        # 2. Open the native capture backend
        self._backend = create_backend(self._platform)
        # Optional BPF/pcap filter from the launcher (run_main.py)
        bpf = os.environ.get("NI_BPF_FILTER")
        if bpf:
            try:
                self._backend.set_filter(bpf)
            except Exception as exc:
                logger.warning("Failed to apply capture filter %r: %s", bpf, exc)
        self._backend.open()

        # 3. Run capture loop in dedicated thread
        self._capture_thread = threading.Thread(
            target=self._capture_loop, name="capture_loop", daemon=True
        )
        self._capture_thread.start()
        logger.info(
            "Capture started on %s (%s backend) with %d parser threads",
            self._platform.nic_name,
            self._platform.os_name,
            _PARSER_WORKERS,
        )

        self._stats_loop()

    def _capture_loop(self) -> None:
        """Tight recv loop in dedicated thread. GIL released during C calls."""
        backend = self._backend
        if backend is None:
            return
        while not self._shutdown_event.is_set():
            raw = backend.recv()
            if raw is None:
                continue
            self._packets_seen += 1
            self._metrics.inc_captured()
            self._executor.submit(self._parse_and_enqueue, raw)

    def _stats_loop(self) -> None:
        """Block main thread, emitting periodic stats, until shutdown."""
        _STATS_INTERVAL: float = 15.0
        _POLL_SEC: float = 0.5
        try:
            elapsed = 0.0
            while not self._shutdown_event.is_set():
                time.sleep(_POLL_SEC)
                elapsed += _POLL_SEC
                self._metrics.maybe_flush()
                if elapsed >= _STATS_INTERVAL:
                    elapsed = 0.0
                    q_size = 0
                    os_stats: dict[str, int] = {}
                    try:
                        q_size = self._queue.qsize()
                    except NotImplementedError:
                        pass
                    # queue depth + OS/driver stats (best-effort)
                    self._metrics.set_queue_depth(q_size)
                    if self._backend is not None:
                        try:
                            os_stats = self._backend.stats() or {}
                        except Exception:
                            os_stats = {}
                    if os_stats:
                        self._metrics.set_os_counters(
                            recv=os_stats.get("os_packets_recv"),
                            drop=os_stats.get("os_packets_drop"),
                            ifdrop=os_stats.get("os_packets_ifdrop"),
                        )
                    logger.info(
                        "Stats | seen=%d queued=%d q_size=%d os_recv=%s os_drop=%s",
                        self._packets_seen,
                        self._packets_queued,
                        q_size,
                        os_stats.get("os_packets_recv", 0),
                        os_stats.get("os_packets_drop", 0),
                    )
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        logger.info("Initiating graceful shutdown...")
        self._shutdown_event.set()

        if self._capture_thread is not None:
            self._capture_thread.join(timeout=5)

        if self._backend is not None:
            self._backend.close()
            logger.info("Capture backend closed.")

        self._executor.shutdown(wait=False, cancel_futures=True)
        logger.info("Parser pool drained.")

        if self._sink_proc is not None and self._sink_proc.is_alive():
            self._sink_proc.join(timeout=10)
            if self._sink_proc.is_alive():
                logger.warning("DataSink did not exit in time; terminating.")
                self._sink_proc.terminate()

        logger.info("Shutdown complete.")

    def _parse_and_enqueue(self, raw: bytes) -> None:
        parsed = parse_raw_packet(raw)
        if parsed is None:
            return
        self._metrics.inc_parsed()
        try:
            self._queue.put_nowait(parsed)
            self._packets_queued += 1
            self._metrics.inc_queued()
        except Exception:
            self._metrics.inc_dropped()

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        sig_name = signal.Signals(signum).name
        logger.info("Received %s — shutting down", sig_name)
        self._shutdown_event.set()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    configure_logging(agent_id=_AGENT_ID, log_file="capture.log")
    set_correlation_id(_AGENT_ID)

    logger.info("Network Capture Agent v2.0  (agent_id=%s)", _AGENT_ID)

    bronze_cfg = BronzeConfig(root_dir=BRONZE_DIR)

    pinfo = detect_platform()
    engine = CaptureEngine(pinfo, bronze_cfg)
    engine.start()


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    main()
