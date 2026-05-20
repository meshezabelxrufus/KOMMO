"""
outputs/json_writer.py
======================
Atomic JSON file writer for extracted CRM data.

Design goals:
  - Atomic writes: temp file → os.rename() — no partial files on crash
  - Pretty-printed JSON (human-readable, easy to inspect)
  - Timestamped filenames to prevent accidental overwrites
  - Dead-letter output: validation failures written to separate directory
  - Creates output directories on first write

Output structure:
    outputs/
      data/
        leads_2025-01-15T10-23-45.json
        pipelines_2025-01-15T10-23-45.json
        tasks_2025-01-15T10-23-45.json
      errors/
        leads_failed_2025-01-15T10-23-45.json  ← dead-letter records

Usage:
    from outputs.json_writer import JsonWriter
    from pathlib import Path

    writer = JsonWriter(output_dir=Path("outputs/data"))
    writer.write(entity="leads", records=[lead.model_dump() for lead in leads])
    writer.write_dead_letter(entity="leads", records=[bad_dict1, bad_dict2])
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.logger import get_logger

log = get_logger(__name__)


class JsonWriter:
    """
    Atomic JSON writer for CRM extract data.

    Args:
        output_dir: Base directory for successful output files.
                    Dead-letter files go to {output_dir}/../errors/
    """

    def __init__(self, output_dir: Path) -> None:
        self._output_dir = output_dir
        self._error_dir = output_dir.parent / "errors"

    # ------------------------------------------------------------------
    # Public Interface
    # ------------------------------------------------------------------

    def write(self, entity: str, records: list[dict[str, Any]]) -> Path:
        """
        Atomically write a list of records to a timestamped JSON file.

        Args:
            entity:  Entity name used in the filename (e.g. "leads").
            records: List of serialisable dicts.

        Returns:
            Path to the written file.
        """
        # TODO: Implement
        # 1. Ensure output_dir exists
        # 2. Generate timestamped filename
        # 3. Write JSON to .tmp file
        # 4. os.rename() to final path
        # 5. Log success with file path and record count
        raise NotImplementedError("JsonWriter.write — to be implemented in Phase 4")

    def write_dead_letter(
        self,
        entity: str,
        records: list[dict[str, Any]],
        page: int | None = None,
    ) -> Path:
        """
        Write validation-failed records to the dead-letter directory.

        These records are preserved in full so they can be investigated
        and replayed after the root cause is fixed.

        Args:
            entity:  Entity name (e.g. "leads").
            records: Raw dicts that failed Pydantic validation.
            page:    Optional page number for traceability.

        Returns:
            Path to the dead-letter file.
        """
        # TODO: Implement (mirrors write() but to self._error_dir)
        raise NotImplementedError("JsonWriter.write_dead_letter — to be implemented in Phase 4")

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------

    def _timestamped_filename(self, entity: str, suffix: str = "") -> str:
        """
        Generate a filename like: leads_2025-01-15T10-23-45.json

        Colons replaced with hyphens for Windows filesystem compatibility.
        """
        ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        name = f"{entity}{suffix}_{ts}.json"
        return name

    def _atomic_write(self, target: Path, data: list[dict[str, Any]]) -> None:
        """
        Write JSON to a temp file then atomically rename to target.

        Args:
            target: Final file path.
            data:   Data to serialise as JSON.
        """
        # TODO: Implement atomic write pattern
        # tmp_path = target.with_suffix(".tmp")
        # tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        # os.rename(tmp_path, target)
        raise NotImplementedError
