from __future__ import annotations

"""
Thin compatibility wrapper.

Preferred entrypoint:
    python -m network_interface.run_main [...]
or
    python scripts/run.py [...]
"""

from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
# So capture and dashboard always use the same _live.json path (project root)
os.environ.setdefault("NI_PROJECT_ROOT", str(ROOT))

from network_interface import run_main


def main() -> None:
    run_main.main()


if __name__ == "__main__":
    import multiprocessing as _mp

    _mp.freeze_support()
    main()
