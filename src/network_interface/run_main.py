"""
run_main.py — Core launcher logic for the real-time network analytics stack.

Used by CLI entrypoints in scripts/ and by PyInstaller.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import importlib
import logging
import os
import platform
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .capture import capture_agent
from .monitoring.dashboard import _run as dashboard_run

# ── Constants ──
EXIT_OK = 0
EXIT_MISSING_SCRIPT = 1
EXIT_CAPTURE_FAILED = 2
EXIT_PREFLIGHT_FAILED = 3
EXIT_INTERNAL = 4
MIN_PYTHON = (3, 9)
REQUIRED_PACKAGES = ("dpkt", "pyarrow", "psutil", "pydantic")
DEFAULT_TIMEOUT = 30.0
STDOUT_TAIL_CHARS = 2000
CAPTURE_LOG = "_capture.log"

IS_WIN = sys.platform == "win32"

# __file__ = .../src/network_interface/run_main.py → parents[2] = project root (folder containing src/)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
VENV_PYTHON = PROJECT_ROOT / ".venv" / ("Scripts" if IS_WIN else "bin") / ("python.exe" if IS_WIN else "python")

log = logging.getLogger("run")


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _python() -> str:
    if VENV_PYTHON.exists():
        return str(VENV_PYTHON)
    return sys.executable


def _setup_logging(*, verbose: bool = False, log_file: str | None = None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(name)s] %(levelname)s  %(message)s"
    datefmt = "%H:%M:%S"

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, handlers=handlers)


def _check_unix_capture(errors: list[str]) -> None:
    if os.getuid() == 0:  # type: ignore[attr-defined]
        return
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("CapEff:"):
                    cap_hex = int(line.split(":")[1].strip(), 16)
                    cap_net_raw = 1 << 13
                    if cap_hex & cap_net_raw:
                        return
    except (FileNotFoundError, PermissionError, ValueError):
        pass
    errors.append("No raw capture capability (not root and no CAP_NET_RAW)")


def _check_venv_coherence() -> None:
    cfg = PROJECT_ROOT / ".venv" / "pyvenv.cfg"
    if not cfg.exists():
        return
    try:
        text = cfg.read_text(encoding="utf-8")
        match = re.search(r"^version\\s*=\\s*(\\S+)", text, re.MULTILINE)
        if match:
            venv_ver = match.group(1)
            running_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
            if venv_ver != running_ver:
                log.warning(
                    "Venv Python %s differs from running %s — consider recreating the venv",
                    venv_ver,
                    running_ver,
                )
    except OSError:
        pass


def _preflight() -> list[str]:
    errors: list[str] = []

    if sys.version_info < MIN_PYTHON:
        errors.append(
            f"Python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]} required, "
            f"got {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        )

    missing = []
    for pkg in REQUIRED_PACKAGES:
        try:
            importlib.import_module(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        errors.append(
            f"Missing packages: {', '.join(missing)}\\n"
            f"         Fix: pip install -r requirements.txt"
        )

    if IS_WIN:
        if not ctypes.util.find_library("wpcap"):
            errors.append(
                "Npcap/WinPcap not found (wpcap.dll).\\n"
                "         Install: https://npcap.com/#download"
            )
    else:
        _check_unix_capture(errors)

    _check_venv_coherence()

    return errors


def _is_admin() -> bool:
    if IS_WIN:
        try:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0  # type: ignore[union-attr]
        except (AttributeError, OSError):
            return False
    return os.getuid() == 0  # type: ignore[attr-defined]


def _request_admin() -> None:
    if IS_WIN:
        log.info("Raw packet capture requires Administrator privileges.")
        log.info("Re-launching elevated...")
        if _is_frozen():
            exe = sys.executable
            params = " ".join(f'"{a}"' for a in sys.argv[1:])
            cwd = os.path.dirname(exe)
        else:
            # Run run.py (not run_main.py) so sys.path and imports work in the elevated process.
            run_py = PROJECT_ROOT / "run.py"
            args = sys.argv[1:]
            params = f'"{run_py}"' + (" " + " ".join(f'"{a}"' for a in args)) if args else ""
            exe = _python()
            cwd = str(PROJECT_ROOT)
        ctypes.windll.shell32.ShellExecuteW(  # type: ignore[union-attr]
            None,
            "runas",
            exe,
            params,
            cwd,
            1,
        )
        sys.exit(EXIT_OK)
    else:
        script = str(Path(__file__).resolve())
        args = sys.argv[1:]
        cmd_parts = [shlex.quote(script)] + [shlex.quote(a) for a in args]
        cmd_str = " ".join(cmd_parts)
        log.error("Raw packet capture requires root. Re-run with sudo:")
        log.error("  sudo %s %s", _python(), cmd_str)
        sys.exit(EXIT_PREFLIGHT_FAILED)


def _banner(py: str) -> None:
    mode = "frozen (exe)" if _is_frozen() else "source"
    log.info("=" * 60)
    log.info("  Network Analytics — Real-Time Stack")
    log.info("=" * 60)
    log.info("  Python   : %s", py)
    log.info("  Platform : %s %s", platform.system(), platform.release())
    log.info("  Mode     : %s", mode)
    log.info("  Admin    : Yes")
    log.info("")


def _main_frozen(args: argparse.Namespace) -> None:
    import multiprocessing as mp

    mp.set_start_method("spawn", force=True)
    if getattr(args, "bpf", None):
        os.environ["NI_BPF_FILTER"] = args.bpf
    _banner(sys.executable)

    live_json = Path(os.getcwd()) / "_live.json"

    if not args.no_dashboard:
        def _dashboard_worker() -> None:
            w = 0.0
            while w < args.timeout:
                if live_json.exists():
                    break
                time.sleep(0.5)
                w += 0.5
            if not live_json.exists():
                return
            try:
                dashboard_run("live", args.interval, live_path=live_json)
            except Exception:
                log.exception("Dashboard crashed")

        threading.Thread(
            target=_dashboard_worker,
            daemon=True,
            name="dashboard",
        ).start()
        log.info("[2/2] Dashboard will launch after first data...")
    else:
        log.info("[2/2] Dashboard skipped (--no-dashboard).")

    log.info("[1/2] Starting capture agent (main thread)...")
    try:
        capture_agent.main()
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("Capture agent crashed")
    finally:
        log.info("All processes stopped. Goodbye.")


def _drain_stdout(proc: subprocess.Popen[bytes], dest: Path) -> None:
    pipe = proc.stdout
    if not pipe:
        return
    try:
        with open(dest, "wb") as f:
            while True:
                chunk = pipe.read(8192)
                if not chunk:
                    break
                f.write(chunk)
    except (OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _stop_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        if IS_WIN:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
        proc.wait(timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _main_source(args: argparse.Namespace) -> None:
    py = _python()
    _banner(py)

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYARROW_IGNORE_TIMEZONE"] = "1"
    env["PYTHONUTF8"] = "1"
    # Subprocesses: PYTHONPATH=src so "network_interface" is found (README: python -m network_interface.capture.capture_agent)
    src_dir = str(PROJECT_ROOT / "src")
    env["PYTHONPATH"] = src_dir + (os.path.pathsep + env["PYTHONPATH"]) if env.get("PYTHONPATH") else src_dir
    # So capture subprocess (and its DataSink) write _live.json in the same place as run_main/dashboard
    env["NI_PROJECT_ROOT"] = str(PROJECT_ROOT)
    os.environ["NI_PROJECT_ROOT"] = str(PROJECT_ROOT)
    if getattr(args, "bpf", None):
        env["NI_BPF_FILTER"] = args.bpf

    # Remove stale _live.json so we wait for the current capture to write a fresh one
    live_json = PROJECT_ROOT / "_live.json"
    if live_json.exists():
        try:
            live_json.unlink()
            log.debug("Removed stale _live.json")
        except OSError:
            pass

    log.info("[1/2] Starting capture agent...")

    capture_kw: dict = {
        "cwd": str(PROJECT_ROOT),
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
    }
    if IS_WIN:
        capture_kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        capture_kw["start_new_session"] = True

    capture_proc = subprocess.Popen([py, "-m", "network_interface.capture.capture_agent"], **capture_kw)  # type: ignore[arg-type]
    log.info("      PID  : %d", capture_proc.pid)

    capture_log = PROJECT_ROOT / CAPTURE_LOG
    drain_thread = threading.Thread(
        target=_drain_stdout, args=(capture_proc, capture_log), daemon=True,
    )
    drain_thread.start()

    log.info("Waiting for first data (_live.json)...")
    waited = 0.0
    while waited < args.timeout:
        if live_json.exists():
            break
        if capture_proc.poll() is not None:
            drain_thread.join(timeout=3)
            log.error("Capture agent exited unexpectedly (code=%s).", capture_proc.returncode)
            if capture_log.exists():
                tail = capture_log.read_text(errors="replace")[-STDOUT_TAIL_CHARS:]
                if tail:
                    log.error("Capture tail:\\n%s", tail)
            sys.exit(EXIT_CAPTURE_FAILED)
        time.sleep(0.5)
        waited += 0.5
        log.debug("  ... %.1fs", waited)

    if not live_json.exists():
        log.error("Timeout (%.0fs) waiting for _live.json", args.timeout)
        _stop_process(capture_proc)
        sys.exit(EXIT_CAPTURE_FAILED)

    log.info("Capture agent healthy.")

    if args.no_dashboard:
        log.info("[2/2] Dashboard skipped (--no-dashboard).")
        log.info("      Capture is running. Press Ctrl+C to stop.")
        try:
            capture_proc.wait()
        except KeyboardInterrupt:
            pass
        finally:
            _stop_process(capture_proc)
        return

    log.info("[2/2] Launching dashboard (refresh: %.1fs)...", args.interval)
    time.sleep(0.5)

    # Run dashboard in-process so it always has the same sys.path and no PYTHONPATH/env issues.
    try:
        dashboard_run("live", args.interval, live_path=PROJECT_ROOT / "_live.json")
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Stopping capture agent...")
        _stop_process(capture_proc)
        log.info("All processes stopped. Goodbye.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch the full real-time network analytics stack",
        epilog="Examples:\\n  python -m network_interface.run_main\\n  python -m network_interface.run_main --interval 1 --no-dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        metavar="SEC",
        help="Dashboard refresh interval in seconds (default: 2.0)",
    )
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Run capture agent only, skip TUI dashboard",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        metavar="SEC",
        help=f"Seconds to wait for first capture data (default: {DEFAULT_TIMEOUT})",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug-level logging",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        metavar="PATH",
        help="Also write launcher logs to this file",
    )
    parser.add_argument(
        "--bpf",
        type=str,
        default=None,
        metavar="EXPR",
        help="Optional pcap/BPF capture filter (example: tcp port 80 or udp port 53)",
    )
    args = parser.parse_args()
    if not 0.1 <= args.interval <= 60.0:
        parser.error(f"--interval must be between 0.1 and 60.0, got {args.interval}")
    if args.timeout < 5.0:
        parser.error(f"--timeout must be >= 5.0, got {args.timeout}")
    return args


def main() -> None:
    args = _parse_args()
    _setup_logging(verbose=args.verbose, log_file=args.log_file)

    errors = _preflight()
    if errors:
        for err in errors:
            log.error("Preflight: %s", err)
        sys.exit(EXIT_PREFLIGHT_FAILED)

    if not _is_admin():
        _request_admin()

    if _is_frozen():
        _main_frozen(args)
    else:
        _main_source(args)

