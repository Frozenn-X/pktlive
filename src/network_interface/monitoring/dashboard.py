"""
dashboard.py — htop-style Network Analytics TUI

Reads _live.json (atomic snapshot from capture agent, <1ms).
Three modal views, all fitted to terminal height.

Views:
    live   — real-time packet flow (default)
    ports  — port/service breakdown
    stats  — protocol, IPs, subnets, Gold aggregates

Usage:
    python dashboard.py              # live view, 0.5s refresh
    python dashboard.py ports        # start in ports view
    python dashboard.py stats        # start in stats view
    python dashboard.py --interval 1 # slower refresh

Hotkeys:  [L]ive  [P]orts  [S]tats  [F]ilter  [Q]uit
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    import msvcrt
else:
    import select
    import tty
    import termios

def _project_root() -> Path:
    root = os.environ.get("NI_PROJECT_ROOT")
    if root:
        p = Path(root).resolve()
        if p.is_dir():
            return p
    return Path(__file__).resolve().parents[4]


_PROJECT_ROOT = _project_root()
LIVE_PATH = _PROJECT_ROOT / "_live.json"

WELL_KNOWN_PORTS: dict[int, str] = {
    20: "FTP-DATA", 21: "FTP", 22: "SSH", 23: "TELNET", 25: "SMTP",
    53: "DNS", 67: "DHCP", 68: "DHCP", 80: "HTTP", 110: "POP3",
    123: "NTP", 143: "IMAP", 443: "HTTPS", 445: "SMB", 993: "IMAPS",
    995: "POP3S", 1433: "MSSQL", 1723: "PPTP", 3306: "MySQL",
    3389: "RDP", 5060: "SIP", 5353: "mDNS", 5432: "Postgres",
    5900: "VNC", 6379: "Redis", 8080: "HTTP-ALT", 8443: "HTTPS-ALT",
    27017: "MongoDB",
}

_OS_NOISE_PORTS: set[int] = {
    137, 138, 139,  # NetBIOS
    1900,  # SSDP / UPnP
    5353,  # mDNS
    5355,  # LLMNR
    3702,  # WS-Discovery
}

_EPHEMERAL_LOW: int = 49152


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def _bar(pct: float, width: int = 20) -> str:
    filled = int(pct / 100 * width)
    return "\u2588" * filled + "\u2591" * (width - filled)


def _load(live_path: Path | None = None) -> dict[str, Any]:
    path = live_path if live_path is not None else LIVE_PATH
    try:
        return json.loads(path.read_bytes())
    except Exception:
        return {}


def _data_age(snap: dict[str, Any]) -> str:
    sa = snap.get("snapshot_at", "")
    if not sa:
        return "?"
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(sa)).total_seconds()
        return f"{max(0, age):.1f}s"
    except Exception:
        return "?"


def _read_key() -> str | None:
    """Non-blocking single-key read. Returns lowercase char or None."""
    if sys.platform == "win32":
        if msvcrt.kbhit():  # type: ignore[attr-defined]
            ch = msvcrt.getch()  # type: ignore[attr-defined]
            return ch.decode("utf-8", errors="ignore").lower()
    else:
        if select.select([sys.stdin], [], [], 0)[0]:
            return sys.stdin.read(1).lower()
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Header + Footer (shared by all views)
# ─────────────────────────────────────────────────────────────────────────────

def _header(snap: dict[str, Any], view_name: str, width: int, load_ms: float, filter_mode: str) -> list[str]:
    total = snap.get("total_packets", 0)
    total_b = snap.get("total_bytes", 0)
    now = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    age = _data_age(snap)
    health = snap.get("capture_health") or {}
    drop_pct = health.get("drop_rate_pct")
    os_drop = health.get("os_drop")
    app_drop = health.get("app_dropped")
    if drop_pct is not None:
        drops_str = f"  drops: {drop_pct:.3f}% (os={os_drop} / queue={app_drop})"
    else:
        drops_str = ""
    sep = "\u2500" * width
    tag = view_name.upper()
    filter_str = {
        "all": "all",
        "hide_os_noise": "no-noise",
        "only_well_known": "well-known",
    }.get(filter_mode, filter_mode)
    return [
        sep,
        f"  NETWORK CAPTURE \u2014 {tag:<8}  {now}  |  "
        f"{total:,} pkts / {_human_bytes(total_b)}  |  "
        f"age: {age}  |  load: {load_ms:.0f}ms  filter: {filter_str}{drops_str}",
        sep,
    ]


_FOOTER_VIEWS = {
    "live":  "  [P]orts  [S]tats  [F]ilter  [Q]uit",
    "ports": "  [L]ive   [S]tats  [F]ilter  [Q]uit",
    "stats": "  [L]ive   [P]orts  [F]ilter  [Q]uit",
}


def _footer(view_name: str, width: int) -> list[str]:
    return ["\u2500" * width, _FOOTER_VIEWS[view_name]]


# ─────────────────────────────────────────────────────────────────────────────
# View: LIVE — htop-style packet flow
# ─────────────────────────────────────────────────────────────────────────────

def _view_live(snap: dict[str, Any], body_rows: int, width: int) -> list[str]:
    lines: list[str] = []
    hdr = f"  {'Time':<14} {'Source':<22} {'Destination':<22} {'Proto':<6} {'Len':>6} {'Flags':<5} {'TTL':>4} {'Service':<18}"
    lines.append(hdr)
    lines.append("  " + "\u2500" * (width - 4))

    recent = snap.get("recent_packets", [])
    avail = body_rows - 2  # header row + separator

    # show the most recent packets that fit, newest at bottom
    visible = recent[-avail:] if len(recent) > avail else recent
    for pkt in visible:
        ts_raw = pkt.get("ts", "")
        try:
            ts_str = datetime.fromisoformat(ts_raw).strftime("%H:%M:%S.%f")[:-3]
        except Exception:
            ts_str = ts_raw[:12]
        src = pkt.get("src_ip", "?")
        sp = pkt.get("src_port")
        src_f = f"{src}:{sp}" if sp is not None else src
        dst = pkt.get("dst_ip", "?")
        dp = pkt.get("dst_port")
        dst_f = f"{dst}:{dp}" if dp is not None else dst
        proto = pkt.get("protocol", "?")
        ln = pkt.get("length", 0)
        fl = pkt.get("flags") or ""
        tt = pkt.get("ttl")
        tt_s = str(tt) if tt is not None else ""
        svc = pkt.get("service_info") or ""
        lines.append(f"  {ts_str:<14} {src_f:<22} {dst_f:<22} {proto:<6} {ln:>6} {fl:<5} {tt_s:>4} {svc:<18}")

    # pad remaining rows so footer stays at bottom
    while len(lines) < body_rows:
        lines.append("")

    return lines


# ─────────────────────────────────────────────────────────────────────────────
# View: PORTS — port/service breakdown
# ─────────────────────────────────────────────────────────────────────────────

def _view_ports(snap: dict[str, Any], body_rows: int, width: int, filter_mode: str) -> list[str]:
    lines: list[str] = []
    hdr = f"  {'Port':<7} {'Service':<12} {'Proto':<6} {'Packets':>10} {'%':>6} {'Bytes':>12}  {'Distribution'}"
    lines.append(hdr)
    lines.append("  " + "\u2500" * (width - 4))

    port_stats = snap.get("port_stats", [])
    total = snap.get("total_packets", 0)
    avail = body_rows - 2

    filtered: list[dict[str, Any]] = []
    for entry in port_stats:
        port = int(entry.get("port", 0) or 0)
        if filter_mode == "hide_os_noise" and port in _OS_NOISE_PORTS:
            continue
        if filter_mode == "only_well_known" and port not in WELL_KNOWN_PORTS:
            continue
        filtered.append(entry)

    for entry in filtered[:avail]:
        port = entry["port"]
        proto = entry["protocol"]
        pkts = entry["packets"]
        byt = entry["bytes"]
        pct = pkts / total * 100 if total else 0
        svc = WELL_KNOWN_PORTS.get(port, "")
        lines.append(
            f"  {port:<7} {svc:<12} {proto:<6} {pkts:>10,} {pct:>5.1f}% {_human_bytes(byt):>12}  {_bar(pct)}"
        )

    while len(lines) < body_rows:
        lines.append("")

    return lines


# ─────────────────────────────────────────────────────────────────────────────
# View: STATS — compact overview
# ─────────────────────────────────────────────────────────────────────────────

def _view_stats(snap: dict[str, Any], body_rows: int, width: int) -> list[str]:
    lines: list[str] = []
    total = snap.get("total_packets", 0)
    sub_sep = "  " + "\u2500" * (width - 4)

    # ── Protocol Breakdown ──
    lines.append("  PROTOCOLS")
    lines.append(sub_sep)
    protocols = snap.get("protocols", [])
    if protocols:
        lines.append(f"  {'Proto':<10} {'Packets':>10} {'%':>6} {'Bytes':>12} {'Avg':>7}  {'Bar'}")
        for p in protocols[:5]:
            pct = p["packets"] / total * 100 if total else 0
            avg = p["bytes"] / p["packets"] if p["packets"] else 0
            lines.append(f"  {p['name']:<10} {p['packets']:>10,} {pct:>5.1f}% {_human_bytes(p['bytes']):>12} {avg:>6.0f}B  {_bar(pct, 15)}")
    lines.append("")

    # how many rows left for 3 remaining sections + gold
    used = len(lines)
    remaining = body_rows - used
    per_section = max(2, (remaining - 6) // 4)  # 4 sections, ~6 lines overhead

    # ── Top Source IPs ──
    lines.append("  TOP SRC IPs")
    lines.append(sub_sep)
    for e in snap.get("top_src_ips", [])[:per_section]:
        pct = e["packets"] / total * 100 if total else 0
        lines.append(f"  {e['ip']:<20} {e['packets']:>10,} {pct:>5.1f}%")
    lines.append("")

    # ── Top Destination IPs ──
    lines.append("  TOP DST IPs")
    lines.append(sub_sep)
    for e in snap.get("top_dst_ips", [])[:per_section]:
        pct = e["packets"] / total * 100 if total else 0
        lines.append(f"  {e['ip']:<20} {e['packets']:>10,} {pct:>5.1f}%")
    lines.append("")

    # ── Subnets ──
    lines.append("  SUBNETS (/24)")
    lines.append(sub_sep)
    for e in snap.get("top_subnets", [])[:per_section]:
        pct = e["packets"] / total * 100 if total else 0
        lines.append(f"  {e['subnet']:<22} {e['packets']:>10,} {pct:>5.1f}%")
    lines.append("")

    # ── Gold ──
    gold = snap.get("gold", {})
    lines.append("  GOLD AGGREGATES")
    lines.append(sub_sep)
    if gold:
        lines.append(f"  {'Proto':<8} {'Pkts':>10} {'Bytes':>12} {'uSrc':>6} {'uDst':>6} {'uPorts':>7}")
        for proto, a in sorted(gold.items(), key=lambda x: x[1].get("packets", 0), reverse=True)[:per_section]:
            lines.append(
                f"  {proto:<8} {a.get('packets',0):>10,} {_human_bytes(a.get('bytes',0)):>12} "
                f"{a.get('src',0):>6} {a.get('dst',0):>6} {a.get('ports',0):>7}"
            )
    else:
        lines.append("  (accumulating...)")

    while len(lines) < body_rows:
        lines.append("")

    return lines[:body_rows]


# ─────────────────────────────────────────────────────────────────────────────
# Main render loop
# ─────────────────────────────────────────────────────────────────────────────

_VIEW_FN = {
    "live": _view_live,
    "ports": _view_ports,
    "stats": _view_stats,
}

_KEY_MAP = {
    "l": "live",
    "p": "ports",
    "s": "stats",
}


def _run(view: str, interval: float, live_path: Path | None = None) -> None:
    # setup raw terminal on unix for non-blocking reads
    old_settings = None
    if sys.platform != "win32":
        old_settings = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())

    sys.stdout.write("\033[?25l")  # hide cursor
    sys.stdout.flush()

    # default filter mode from environment, used by launchers/run_main
    filter_mode = os.environ.get("NI_FILTER_PORTS", "all")
    if filter_mode not in ("all", "hide_os_noise", "only_well_known"):
        filter_mode = "all"

    try:
        while True:
            t0 = time.monotonic()

            try:
                term_h = os.get_terminal_size().lines
                term_w = os.get_terminal_size().columns
            except OSError:
                term_h, term_w = 30, 110
            width = max(90, term_w)

            snap = _load(live_path)
            load_ms = (time.monotonic() - t0) * 1000

            if not snap:
                buf = "\033[H\033[2J"
                buf += "  Waiting for _live.json ... Start capture_agent.py\n"
                sys.stdout.write(buf)
                sys.stdout.flush()
                time.sleep(1)
                continue

            hdr = _header(snap, view, width, load_ms, filter_mode)
            ftr = _footer(view, width)

            # body gets whatever rows remain
            body_rows = max(3, term_h - len(hdr) - len(ftr))
            if view == "ports":
                body = _view_ports(snap, body_rows, width, filter_mode)
            else:
                body = _VIEW_FN[view](snap, body_rows, width)

            buf = "\033[H\033[2J"
            for line in hdr + body + ftr:
                buf += line[:width] + "\n"
            sys.stdout.write(buf)
            sys.stdout.flush()

            # non-blocking input check
            elapsed = time.monotonic() - t0
            wait = max(0, interval - elapsed)
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                key = _read_key()
                if key == "q":
                    return
                if key == "f":
                    if filter_mode == "all":
                        filter_mode = "hide_os_noise"
                    elif filter_mode == "hide_os_noise":
                        filter_mode = "only_well_known"
                    else:
                        filter_mode = "all"
                    break
                if key in _KEY_MAP:
                    view = _KEY_MAP[key]
                    break
                time.sleep(0.05)

    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h\033[H\033[2J")
        sys.stdout.flush()
        if old_settings is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        print("Dashboard stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Real-time network analytics dashboard")
    parser.add_argument(
        "view", nargs="?", default="live",
        choices=["live", "ports", "stats"],
        help="Initial view (default: live)"
    )
    parser.add_argument(
        "--interval", type=float, default=0.5,
        help="Refresh interval in seconds (default: 0.5)"
    )
    args = parser.parse_args()
    _run(args.view, args.interval)


if __name__ == "__main__":
    main()
