from __future__ import annotations

"""
network_interface package

Src-layout package containing the real-time network analytics stack:
- capture_agent: edge capture + bronze/silver/gold writer
- streaming_pipeline: inline medallion transforms
- bronze_store: local bronze layer
- pipeline_metrics: runtime metrics and health
- structured_log: JSON structured logging
- dashboard: TUI dashboard reading _live.json
- databricks_pipeline: optional cloud integration
"""

from . import capture, storage, monitoring, cloud  # noqa: F401

__all__ = [
    "capture",
    "storage",
    "monitoring",
    "cloud",
]

