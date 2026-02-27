from __future__ import annotations

import ctypes
import sys
import types

from network_interface.capture.capture_agent import LinuxCapture, WindowsCapture


class _PcapStatStruct(ctypes.Structure):
    _fields_ = [
        ("ps_recv", ctypes.c_uint),
        ("ps_drop", ctypes.c_uint),
        ("ps_ifdrop", ctypes.c_uint),
    ]


class _DummyStatsLib:
    def __init__(self) -> None:
        self.stats_called = False

    def pcap_stats(self, _pcap: object, ps: object) -> int:
        self.stats_called = True
        ptr = ctypes.cast(ps, ctypes.POINTER(_PcapStatStruct))
        ptr.contents.ps_recv = 10
        ptr.contents.ps_drop = 2
        ptr.contents.ps_ifdrop = 1
        return 0


class _DummyFilterLib:
    def __init__(self) -> None:
        self.compile_args: list[str] = []
        self.setfilter_called = False
        self.freecode_called = False

    def pcap_compile(self, _pcap: object, _prog: ctypes.c_void_p, expr: bytes, _opt: int, _mask: int) -> int:
        self.compile_args.append(expr.decode("ascii"))
        return 0

    def pcap_setfilter(self, _pcap: object, _prog: ctypes.c_void_p) -> int:
        self.setfilter_called = True
        return 0

    def pcap_freecode(self, _prog: ctypes.c_void_p) -> None:
        self.freecode_called = True


class TestWindowsCaptureInternals:
    def test_stats_uses_pcap_stats(self) -> None:
        cap = WindowsCapture("dummy")
        dummy = _DummyStatsLib()
        cap._wpcap = dummy  # type: ignore[attr-defined]
        cap._pcap = object()  # type: ignore[attr-defined]

        stats = cap.stats()

        assert dummy.stats_called
        assert stats["os_packets_recv"] == 10
        assert stats["os_packets_drop"] == 2
        assert stats["os_packets_ifdrop"] == 1

    def test_set_filter_calls_compile_and_setfilter(self) -> None:
        cap = WindowsCapture("dummy")
        dummy = _DummyFilterLib()
        cap._wpcap = dummy  # type: ignore[attr-defined]
        cap._pcap = object()  # type: ignore[attr-defined]

        cap.set_filter("tcp port 80")

        assert "tcp port 80" in dummy.compile_args
        assert dummy.setfilter_called
        assert dummy.freecode_called


class TestLinuxCaptureStats:
    def test_stats_uses_psutil_when_available(self) -> None:
        dummy_psutil = types.SimpleNamespace(
            net_io_counters=lambda pernic=True: {
                "eth0": types.SimpleNamespace(
                    packets_recv=5,
                    dropin=1,
                    dropout=2,
                )
            }
        )
        sys.modules["psutil"] = dummy_psutil  # used by LinuxCapture.stats import

        cap = LinuxCapture("eth0")
        stats = cap.stats()

        assert stats["os_packets_recv"] == 5
        assert stats["os_packets_drop"] == 1
        assert stats["os_packets_ifdrop"] == 2


