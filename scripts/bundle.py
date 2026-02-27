"""
bundle.py — Build standalone executables per OS via PyInstaller.

Produces a single-file executable (--onefile) that embeds Python, all
dependencies, and project modules. No Python install needed on the target.

Output:
    dist/networkInterface.exe      (Windows)
    dist/networkInterface           (Linux / macOS)

Usage:
    python bundle.py                # build for current OS
    python bundle.py --onedir       # directory mode (faster startup)
    python bundle.py --clean        # wipe dist/ + build/ first
    python bundle.py --name myapp   # custom output name
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"

ENTRY_POINT = "scripts/run.py"

HIDDEN_IMPORTS = [
    "network_interface.capture.capture_agent",
    "network_interface.monitoring.dashboard",
    "network_interface.storage.streaming_pipeline",
    "network_interface.storage.bronze_store",
    "network_interface.monitoring.structured_log",
    "network_interface.monitoring.pipeline_metrics",
    "network_interface.cloud.databricks_pipeline",
    "dpkt",
    "dpkt.ethernet",
    "dpkt.ip",
    "dpkt.ip6",
    "dpkt.tcp",
    "dpkt.udp",
    "dpkt.dns",
    "dpkt.icmp",
    "dpkt.arp",
    "psutil",
    "pydantic",
    "pyarrow",
]

DATA_FILES = [
    "requirements.txt",
]


def _resolve_name() -> str:
    return "networkInterface"


def build(*, onedir: bool = False, name: str | None = None) -> Path:
    name = name or _resolve_name()

    cmd: list[str] = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name", name,
        "--distpath", str(DIST_DIR),
        "--workpath", str(BUILD_DIR),
    ]

    if onedir:
        cmd.append("--onedir")
    else:
        cmd.append("--onefile")

    cmd.append("--console")

    for mod in HIDDEN_IMPORTS:
        cmd.extend(["--hidden-import", mod])

    sep = ";" if sys.platform == "win32" else ":"
    for data in DATA_FILES:
        src = PROJECT_ROOT / data
        if src.exists():
            cmd.extend(["--add-data", f"{src}{sep}."])

    cmd.append(str(PROJECT_ROOT / ENTRY_POINT))

    print(f"[*] Building {'directory' if onedir else 'single-file'} executable...")
    print(f"    Target : {platform.system()} {platform.machine()}")
    print(f"    Entry  : {ENTRY_POINT}")
    print(f"    Name   : {name}")
    print()

    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    if result.returncode != 0:
        print(f"\n[ERROR] PyInstaller exited with code {result.returncode}", file=sys.stderr)
        sys.exit(result.returncode)

    if onedir:
        out = DIST_DIR / name
    elif sys.platform == "win32":
        out = DIST_DIR / f"{name}.exe"
    else:
        out = DIST_DIR / name

    if out.exists():
        if out.is_file():
            size_mb = out.stat().st_size / (1024 * 1024)
            print(f"\n[OK] {out.relative_to(PROJECT_ROOT)}  ({size_mb:.1f} MB)")
        else:
            print(f"\n[OK] {out.relative_to(PROJECT_ROOT)}/  (directory mode)")
    else:
        print(f"\n[WARN] Expected output not found at {out}", file=sys.stderr)

    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build standalone executable via PyInstaller",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python bundle.py              # single-file exe for current OS\n"
            "  python bundle.py --onedir     # directory mode (faster startup)\n"
            "  python bundle.py --clean      # clean build\n"
        ),
    )
    parser.add_argument(
        "--onedir",
        action="store_true",
        help="Directory mode instead of single-file (faster startup, larger output)",
    )
    parser.add_argument(
        "--name",
        type=str,
        default=None,
        metavar="NAME",
        help=f"Output executable name (default: {_resolve_name()})",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove dist/ and build/ before building",
    )
    args = parser.parse_args()

    if args.clean:
        for d in (DIST_DIR, BUILD_DIR):
            if d.exists():
                shutil.rmtree(d)
                print(f"[*] Cleaned {d.relative_to(PROJECT_ROOT)}/")

    build(onedir=args.onedir, name=args.name)


if __name__ == "__main__":
    main()
