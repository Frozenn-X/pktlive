from __future__ import annotations

from pathlib import Path
import multiprocessing as mp
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from network_interface import run_main


def main() -> None:
    run_main.main()


if __name__ == "__main__":
    mp.freeze_support()
    main()

