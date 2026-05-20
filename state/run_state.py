"""
state/run_state.py
==================
Tracks metadata for each extraction run.

A RunState is created at the start of each extraction run and holds:
  - A unique run_id (UUID) used to correlate all log entries
  - Start/end timestamps
  - Record counts per entity
  - Error counts

In Milestone 1 this is purely in-memory — no persistence needed.
In Milestone 2, RunState will be serialised to state/last_run.json
to support incremental extraction (extract only updated records).

Usage:
    from state.run_state import RunState

    run = RunState.start()
    structlog.contextvars.bind_contextvars(run_id=run.run_id)

    # After extraction:
    run.record_counts["leads"] = 1523
    run.finish()
    print(run.summary())
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class RunState:
    """
    Immutable run identifier with mutable result accumulators.

    Attributes:
        run_id:        UUID string — unique per extraction run.
        started_at:    UTC datetime when the run began.
        finished_at:   UTC datetime when the run completed (set by finish()).
        record_counts: Dict mapping entity name → total records extracted.
        error_counts:  Dict mapping entity name → failed (dead-letter) records.
    """

    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    finished_at: datetime | None = field(default=None)
    record_counts: dict[str, int] = field(default_factory=dict)
    error_counts: dict[str, int] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def start(cls) -> "RunState":
        """Create and return a new RunState with a fresh run_id."""
        return cls()

    # ------------------------------------------------------------------
    # State Updates
    # ------------------------------------------------------------------

    def finish(self) -> None:
        """Record the run completion timestamp."""
        self.finished_at = datetime.now(tz=timezone.utc)

    def add_records(self, entity: str, count: int) -> None:
        """Increment the record count for a given entity."""
        self.record_counts[entity] = self.record_counts.get(entity, 0) + count

    def add_errors(self, entity: str, count: int) -> None:
        """Increment the error count for a given entity."""
        self.error_counts[entity] = self.error_counts.get(entity, 0) + count

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    @property
    def duration_seconds(self) -> float | None:
        """Total run duration in seconds, or None if not finished."""
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def summary(self) -> dict[str, Any]:
        """
        Return a structured summary dict suitable for logging.

        Example:
            {
                "run_id": "abc-123",
                "started_at": "2025-01-15T10:23:00+00:00",
                "finished_at": "2025-01-15T10:23:42+00:00",
                "duration_seconds": 42.1,
                "record_counts": {"leads": 1523, "tasks": 312},
                "error_counts": {"leads": 2}
            }
        """
        return {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_seconds": self.duration_seconds,
            "record_counts": self.record_counts,
            "error_counts": self.error_counts,
        }
