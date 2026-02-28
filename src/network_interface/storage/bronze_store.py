"""
bronze_store.py — Local Bronze Layer (Medallion Architecture)

Arborescence sur disque
───────────────────────
  bronze/
  ├── data/
  │   └── dt=2026-02-25/
  │       └── hr=14/
  │           ├── capture_14h00_a1b2c3d4.ndjson   (SEALED — prêt pour transfert)
  │           └── capture_14h12_a1b2c3d4.ndjson   (ACTIVE — en cours d'écriture)
  ├── _manifest/
  │   └── manifest.jsonl          (registre append-only: état de chaque fichier)
  ├── _quarantine/
  │   └── ...                     (records qui échouent la validation Pydantic)
  └── _meta/
      └── agent.json              (identité + stats cumulatives de l'agent)

Principes de design
───────────────────
  1. Partitionnement date/heure   → prévient le scan linéaire à la lecture
  2. Rotation taille + temps      → fichiers 50-128 MiB, jamais de small-file
  3. Manifest append-only         → état de transfert (SEALED / TRANSFERRED / ARCHIVED)
  4. Quarantine                   → aucun record n'est perdu silencieusement
  5. Compaction locale            → fusionne les fichiers < seuil dans une partition
  6. Rétention configurable       → purge automatique des fichiers TRANSFERRED
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("bronze_store")


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────
class BronzeConfig(BaseModel):
    """Toute la config Bronze en un seul objet validé."""

    root_dir: Path = Field(default=Path("bronze"))
    max_file_bytes: int = Field(default=100 * 1024 * 1024, ge=1024 * 1024)
    max_file_age_sec: float = Field(default=300.0, ge=10.0)
    flush_threshold: int = Field(default=500, ge=10)
    flush_interval_sec: float = Field(default=1.0, ge=0.1)
    fsync_interval_sec: float = Field(default=30.0, ge=1.0)
    retention_hours: int = Field(default=72, ge=1)
    compaction_min_bytes: int = Field(default=10 * 1024 * 1024, ge=1024 * 1024)
    quarantine_enabled: bool = Field(default=True)


# ─────────────────────────────────────────────────────────────────────────────
# Manifest — registre d'état des fichiers Bronze
# ─────────────────────────────────────────────────────────────────────────────
class FileState(str, Enum):
    ACTIVE = "ACTIVE"
    SEALED = "SEALED"
    TRANSFERRED = "TRANSFERRED"
    ARCHIVED = "ARCHIVED"


class ManifestEntry(BaseModel):
    file_path: str
    state: FileState
    partition_dt: str
    partition_hr: int
    created_at: str
    sealed_at: Optional[str] = None
    transferred_at: Optional[str] = None
    size_bytes: int = 0
    record_count: int = 0
    agent_id: str = ""


class ManifestStore:
    """
    Append-only JSONL manifest.  Chaque ligne = un événement d'état.
    La lecture reconstruit l'état courant par file_path (last-write-wins).
    """

    def __init__(self, manifest_dir: Path) -> None:
        self._dir = manifest_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "manifest.jsonl"
        self._state: dict[str, ManifestEntry] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = ManifestEntry.model_validate_json(line)
                    self._state[entry.file_path] = entry
                except Exception:
                    logger.warning("Corrupt manifest line, skipping")

    def _append(self, entry: ManifestEntry) -> None:
        self._state[entry.file_path] = entry
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")
            f.flush()

    def register_active(self, entry: ManifestEntry) -> None:
        self._append(entry)

    def seal(self, file_path: str, size_bytes: int, record_count: int) -> None:
        existing = self._state.get(file_path)
        if existing is None:
            return
        updated = existing.model_copy(
            update={
                "state": FileState.SEALED,
                "sealed_at": _utc_iso(),
                "size_bytes": size_bytes,
                "record_count": record_count,
            }
        )
        self._append(updated)

    def mark_transferred(self, file_path: str) -> None:
        existing = self._state.get(file_path)
        if existing is None:
            return
        updated = existing.model_copy(
            update={
                "state": FileState.TRANSFERRED,
                "transferred_at": _utc_iso(),
            }
        )
        self._append(updated)

    def mark_archived(self, file_path: str) -> None:
        existing = self._state.get(file_path)
        if existing is None:
            return
        updated = existing.model_copy(update={"state": FileState.ARCHIVED})
        self._append(updated)

    def get_by_state(self, state: FileState) -> list[ManifestEntry]:
        return [e for e in self._state.values() if e.state == state]

    def get(self, file_path: str) -> Optional[ManifestEntry]:
        return self._state.get(file_path)

    @property
    def all_entries(self) -> dict[str, ManifestEntry]:
        return dict(self._state)


# ─────────────────────────────────────────────────────────────────────────────
# Active file handle — le fichier en cours d'écriture
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class _ActiveFile:
    path: Path
    fh: Any
    created_at: float
    bytes_written: int = 0
    records_written: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# BronzeStore — API publique du layer Bronze local
# ─────────────────────────────────────────────────────────────────────────────
class BronzeStore:
    """
    Gère l'écriture, la rotation, le manifest, la quarantine, la compaction
    et la rétention du Bronze layer sur stockage local.

    Thread-safety: cette classe est conçue pour être appelée depuis un seul
    process (le DataSinkProcess).  Pas de lock interne — l'isolation est
    assurée par l'architecture multi-process.
    """

    def __init__(self, config: BronzeConfig, agent_id: str) -> None:
        self._cfg = config
        self._agent_id = agent_id

        self._data_dir = config.root_dir / "data"
        self._quarantine_dir = config.root_dir / "_quarantine"
        self._meta_dir = config.root_dir / "_meta"

        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._quarantine_dir.mkdir(parents=True, exist_ok=True)
        self._meta_dir.mkdir(parents=True, exist_ok=True)

        self._manifest = ManifestStore(config.root_dir / "_manifest")
        self._active_file: Optional[_ActiveFile] = None
        self._buffer: list[str] = []
        self._last_flush: float = time.monotonic()
        self._last_fsync: float = time.monotonic()

        self._total_records: int = 0
        self._total_bytes: int = 0
        self._quarantined: int = 0

        self._write_agent_meta()
        logger.info(
            "BronzeStore initialized | root=%s | max_file=%d MiB | retention=%dh",
            config.root_dir,
            config.max_file_bytes // (1024 * 1024),
            config.retention_hours,
        )

    # ── public: écrire un record validé ───────────────────────────────────
    def write_record(self, record_json: str) -> None:
        """Accepte une ligne JSON déjà sérialisée (sortie de model_dump_json)."""
        self._buffer.append(record_json)

        if len(self._buffer) >= self._cfg.flush_threshold:
            self._flush()
            return

        elapsed = time.monotonic() - self._last_flush
        if elapsed >= self._cfg.flush_interval_sec:
            self._flush()

    # ── public: quarantine pour records malformés ─────────────────────────
    def quarantine(self, raw_data: str, reason: str) -> None:
        if not self._cfg.quarantine_enabled:
            return
        now = datetime.now(timezone.utc)
        q_dir = self._quarantine_dir / now.strftime("dt=%Y-%m-%d")
        q_dir.mkdir(parents=True, exist_ok=True)
        q_file = q_dir / f"quarantine_{now.strftime('%H%M%S')}_{self._agent_id}.jsonl"
        entry = json.dumps({"raw": raw_data, "reason": reason, "ts": _utc_iso()})
        with open(q_file, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
        self._quarantined += 1

    # ── public: sceller le fichier actif (appelé au shutdown) ─────────────
    def seal_active(self) -> None:
        """Force le flush + scellement du fichier en cours."""
        if self._buffer:
            self._flush()
        if self._active_file is not None:
            self._seal(self._active_file)
            self._active_file = None

    # ── public: liste des fichiers prêts au transfert vers Silver ─────────
    def pending_transfer(self) -> list[ManifestEntry]:
        return self._manifest.get_by_state(FileState.SEALED)

    def mark_transferred(self, file_path: str) -> None:
        self._manifest.mark_transferred(file_path)

    # ── public: compaction des petits fichiers dans une partition ──────────
    def compact(self) -> int:
        """
        Fusionne les fichiers SEALED < compaction_min_bytes dans la même
        partition heure.  Retourne le nombre de fichiers fusionnés.
        """
        sealed = self._manifest.get_by_state(FileState.SEALED)
        partitions: dict[str, list[ManifestEntry]] = {}
        for entry in sealed:
            key = f"{entry.partition_dt}/hr={entry.partition_hr:02d}"
            partitions.setdefault(key, []).append(entry)

        merged_count = 0
        for part_key, entries in partitions.items():
            small = [e for e in entries if e.size_bytes < self._cfg.compaction_min_bytes]
            if len(small) < 2:
                continue

            total_size = sum(e.size_bytes for e in small)
            total_records = sum(e.record_count for e in small)
            now = datetime.now(timezone.utc)
            merged_name = f"compacted_{now.strftime('%H%M%S')}_{self._agent_id}.ndjson"
            # Match write layout: dt=YYYY-MM-DD/hr=HH
            dt_part = f"dt={small[0].partition_dt}"
            hr_part = f"hr={small[0].partition_hr:02d}"
            merged_path = self._data_dir / dt_part / hr_part / merged_name
            merged_path.parent.mkdir(parents=True, exist_ok=True)

            with open(merged_path, "w", encoding="utf-8") as out:
                for entry in small:
                    src = Path(entry.file_path)
                    if not src.exists():
                        continue
                    with open(src, "r", encoding="utf-8") as inp:
                        shutil.copyfileobj(inp, out)

            self._manifest.register_active(
                ManifestEntry(
                    file_path=str(merged_path),
                    state=FileState.SEALED,
                    partition_dt=small[0].partition_dt,
                    partition_hr=small[0].partition_hr,
                    created_at=_utc_iso(),
                    sealed_at=_utc_iso(),
                    size_bytes=total_size,
                    record_count=total_records,
                    agent_id=self._agent_id,
                )
            )

            for entry in small:
                src = Path(entry.file_path)
                if src.exists():
                    src.unlink()
                self._manifest.mark_archived(entry.file_path)
                merged_count += 1

        if merged_count:
            logger.info("Compaction: merged %d small files", merged_count)
        return merged_count

    # ── public: rétention — purge des fichiers TRANSFERRED trop vieux ─────
    def enforce_retention(self) -> int:
        """Supprime les fichiers TRANSFERRED dont le seal_at > retention_hours."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self._cfg.retention_hours)
        transferred = self._manifest.get_by_state(FileState.TRANSFERRED)
        purged = 0
        for entry in transferred:
            if entry.transferred_at is None:
                continue
            try:
                ts = datetime.fromisoformat(entry.transferred_at)
            except ValueError:
                continue
            if ts < cutoff:
                p = Path(entry.file_path)
                if p.exists():
                    p.unlink()
                    purged += 1
                self._manifest.mark_archived(entry.file_path)
        if purged:
            logger.info("Retention: purged %d files older than %dh", purged, self._cfg.retention_hours)
        return purged

    # ── public: stats ─────────────────────────────────────────────────────
    @property
    def stats(self) -> dict[str, Any]:
        return {
            "total_records": self._total_records,
            "total_bytes": self._total_bytes,
            "quarantined": self._quarantined,
            "active_file": str(self._active_file.path) if self._active_file else None,
            "buffer_len": len(self._buffer),
            "sealed_files": len(self._manifest.get_by_state(FileState.SEALED)),
            "transferred_files": len(self._manifest.get_by_state(FileState.TRANSFERRED)),
        }

    # ── internal: flush buffer vers le fichier actif ──────────────────────
    def _flush(self) -> None:
        if not self._buffer:
            return

        if self._active_file is None:
            self._active_file = self._open_new_file()

        af = self._active_file
        blob = "\n".join(self._buffer) + "\n"
        blob_bytes = len(blob.encode("utf-8"))

        af.fh.write(blob)
        af.fh.flush()

        if time.monotonic() - self._last_fsync >= self._cfg.fsync_interval_sec:
            os.fsync(af.fh.fileno())
            self._last_fsync = time.monotonic()

        af.bytes_written += blob_bytes
        af.records_written += len(self._buffer)
        self._total_records += len(self._buffer)
        self._total_bytes += blob_bytes

        self._buffer.clear()
        self._last_flush = time.monotonic()

        needs_rotate_size = af.bytes_written >= self._cfg.max_file_bytes
        needs_rotate_age = (time.monotonic() - af.created_at) >= self._cfg.max_file_age_sec
        if needs_rotate_size or needs_rotate_age:
            self._seal(af)
            self._active_file = None

    # ── internal: ouvrir un nouveau fichier partitionné ───────────────────
    def _open_new_file(self) -> _ActiveFile:
        now = datetime.now(timezone.utc)
        dt_part = now.strftime("dt=%Y-%m-%d")
        hr_part = f"hr={now.hour:02d}"
        part_dir = self._data_dir / dt_part / hr_part
        part_dir.mkdir(parents=True, exist_ok=True)

        ts_label = now.strftime("%H%M%S")
        chunk_id = uuid.uuid4().hex[:8]
        name = f"capture_{ts_label}_{chunk_id}.ndjson"
        path = part_dir / name

        fh = open(path, "a", encoding="utf-8")
        logger.info("Opened Bronze file: %s", path)

        self._manifest.register_active(
            ManifestEntry(
                file_path=str(path),
                state=FileState.ACTIVE,
                partition_dt=now.strftime("%Y-%m-%d"),
                partition_hr=now.hour,
                created_at=_utc_iso(),
                agent_id=self._agent_id,
            )
        )

        return _ActiveFile(path=path, fh=fh, created_at=time.monotonic())

    # ── internal: sceller = fermer + mettre à jour le manifest ────────────
    def _seal(self, af: _ActiveFile) -> None:
        af.fh.flush()
        os.fsync(af.fh.fileno())
        af.fh.close()

        self._manifest.seal(
            file_path=str(af.path),
            size_bytes=af.bytes_written,
            record_count=af.records_written,
        )
        logger.info(
            "Sealed %s | %.2f MiB | %d records",
            af.path.name,
            af.bytes_written / (1024 * 1024),
            af.records_written,
        )

    # ── internal: écrire les métadonnées agent ────────────────────────────
    def _write_agent_meta(self) -> None:
        meta = {
            "agent_id": self._agent_id,
            "hostname": platform.node(),
            "os": platform.system(),
            "python": platform.python_version(),
            "started_at": _utc_iso(),
            "config": self._cfg.model_dump(mode="json"),
        }
        meta_path = self._meta_dir / "agent.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, default=str)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
