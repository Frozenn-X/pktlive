"""
Single source of truth for project root. No magic depth (parents[N]).

Resolves the directory that contains run.py and src/ so all components
(capture, pipeline, web, dashboard) use the same paths for _live.json,
bronze/, silver/, gold/, etc. Prefers NI_PROJECT_ROOT when set by the launcher.
"""

from __future__ import annotations

import os
from pathlib import Path

_MARKERS = ("run.py", "src")
_PROJECT_ROOT: Path | None = None


def get_project_root() -> Path:
    """Return the project root directory (contains run.py and src/)."""
    global _PROJECT_ROOT
    if _PROJECT_ROOT is not None:
        return _PROJECT_ROOT

    env_root = os.environ.get("NI_PROJECT_ROOT")
    if env_root:
        p = Path(env_root).resolve()
        if p.is_dir() and _is_project_root(p):
            _PROJECT_ROOT = p
            return _PROJECT_ROOT

    # Walk up from this file until we find the directory that contains run.py and src/
    path = Path(__file__).resolve()
    for parent in path.parents:
        if _is_project_root(parent):
            _PROJECT_ROOT = parent
            return _PROJECT_ROOT

    # Fallback: assume we're in src/network_interface/_paths.py → project root is parents[2]
    _PROJECT_ROOT = path.parents[2]
    return _PROJECT_ROOT


def _is_project_root(path: Path) -> bool:
    return path.is_dir() and all((path / m).exists() for m in _MARKERS)


# Convenience for imports: from network_interface._paths import PROJECT_ROOT
PROJECT_ROOT = get_project_root()
