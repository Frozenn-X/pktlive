"""
structured_log.py — JSON structured logging for SOC/SIEM integration.

Produces one JSON object per line, compatible with Splunk HEC, ELK/Filebeat,
Loki/Promtail, and any JSON-aware log collector.

Fields per log line:
    ts          ISO-8601 UTC timestamp
    level       DEBUG/INFO/WARNING/ERROR/CRITICAL
    logger      logger name (e.g. "capture_agent", "bronze_store")
    msg         human-readable message
    correlation correlation ID shared across the pipeline session
    agent_id    agent identifier
    pid         OS process ID
    thread      thread name
    extra       dict of structured key-value data (latency, counts, etc.)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from typing import Any


_CORRELATION_ID: str = uuid.uuid4().hex[:12]


def set_correlation_id(cid: str) -> None:
    global _CORRELATION_ID
    _CORRELATION_ID = cid


def get_correlation_id() -> str:
    return _CORRELATION_ID


class JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def __init__(self, agent_id: str = "") -> None:
        super().__init__()
        self._agent_id = agent_id

    def format(self, record: logging.LogRecord) -> str:
        extra = {}
        for key in ("extra", "metrics", "event"):
            val = getattr(record, key, None)
            if val is not None:
                extra[key] = val

        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "correlation": _CORRELATION_ID,
            "agent_id": self._agent_id,
            "pid": os.getpid(),
            "thread": threading.current_thread().name,
        }
        if extra:
            entry["extra"] = extra
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(entry, default=str)


def configure_logging(
    agent_id: str = "",
    level: int = logging.INFO,
    log_file: str | None = None,
) -> None:
    """
    Replace the root logger's handlers with a JSON structured handler.
    Optionally also log to a file for persistent collection.
    """
    root = logging.getLogger()
    root.setLevel(level)

    for h in root.handlers[:]:
        root.removeHandler(h)

    formatter = JSONFormatter(agent_id=agent_id)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
