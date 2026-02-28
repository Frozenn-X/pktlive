"""
Web UI launcher with capture orchestration.

When the web UI starts, if _live.json is missing or stale (snapshot_at older than
threshold), spawns the capture stack (run.py --no-dashboard) so that Live/Stats
show fresh data. Uses the project venv Python when available so capture has all
dependencies. Waits briefly for the first fresh snapshot before starting uvicorn.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import uvicorn

from .._paths import PROJECT_ROOT

LIVE_PATH = PROJECT_ROOT / "_live.json"

IS_WIN = sys.platform == "win32"
VENV_PYTHON = PROJECT_ROOT / ".venv" / ("Scripts" if IS_WIN else "bin") / ("python.exe" if IS_WIN else "python")


def _python_for_capture() -> str:
    """Prefer project venv so capture has dpkt, pyarrow, etc."""
    if VENV_PYTHON.exists():
        return str(VENV_PYTHON)
    return sys.executable


def _load_live_snapshot() -> dict[str, Any]:
    if not LIVE_PATH.exists():
        return {}
    try:
        return json.loads(LIVE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _is_capture_healthy(threshold_sec: float = 10.0) -> bool:
    """
    True if _live.json exists and snapshot_at is within threshold_sec.
    """
    snap = _load_live_snapshot()
    ts_raw = snap.get("snapshot_at") or ""
    if not ts_raw:
        return False
    try:
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    except Exception:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    return age <= threshold_sec


def _stop_process(proc: Optional[subprocess.Popen[bytes]]) -> None:
    if proc is None:
        return
    if proc.poll() is not None:
        return
    try:
        if IS_WIN:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
        proc.wait(timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        try:
            proc.kill()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def main() -> None:
    """
    Launch the local web UI and optionally the capture stack.

    Usage (from project root, venv recommended):
        python -m src.network_interface.web.web_main
    """
    capture_proc: Optional[subprocess.Popen[bytes]] = None

    if not _is_capture_healthy():
        py = _python_for_capture()
        env = os.environ.copy()
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONUNBUFFERED", "1")
        # So capture (and run_main’s subprocess) write _live.json at project root
        env["NI_PROJECT_ROOT"] = str(PROJECT_ROOT)
        env["PYTHONPATH"] = str(PROJECT_ROOT) + (os.path.pathsep + env.get("PYTHONPATH", "")) if env.get("PYTHONPATH") else str(PROJECT_ROOT)
        cmd: list[str] = [py, "run.py", "--no-dashboard"]
        try:
            capture_proc = subprocess.Popen(
                cmd,
                cwd=str(PROJECT_ROOT),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            capture_proc = None
        else:
            # Wait for first fresh snapshot so "last capture" date is current (max 25s).
            for _ in range(25):
                time.sleep(1)
                if _is_capture_healthy(threshold_sec=15.0):
                    break
                if capture_proc.poll() is not None:
                    break

    try:
        uvicorn.run(
            "src.network_interface.web.app:app",
            host="127.0.0.1",
            port=8000,
            reload=False,
            log_level="info",
        )
    finally:
        _stop_process(capture_proc)


if __name__ == "__main__":
    main()
