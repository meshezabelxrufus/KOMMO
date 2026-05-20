"""
utils/state_manager.py
======================
Sync state persistence for incremental extraction runs.

PURPOSE
───────
Tracks the "high-water mark" for each entity so that subsequent
extraction runs only pull records that changed since the last run —
instead of re-fetching the entire dataset every time.

STATE SCHEMA
────────────
  {
    "schema_version": 1,
    "updated_at": "2025-01-15T10:23:45+00:00",
    "entities": {
      "leads": {
        "last_run_at":        1736847825,
        "last_run_at_iso":    "2025-01-14T09:03:45+00:00",
        "records_extracted":  1523,
        "pages_fetched":      7,
        "status":             "success"
      },
      "tasks": { ... },
      "pipelines": { ... },
      "chats": {
        "last_message_timestamp":     1736847825,
        "last_message_timestamp_iso": "2025-01-14T09:03:45+00:00",
        ...
      }
    }
  }

DESIGN DECISIONS
────────────────
  - Atomic writes (tmp → rename) — crash-safe
  - Schema version field — forward-compatible migration path
  - Per-entity state — each entity tracks its own high-water mark
  - Separate last_message_timestamp for chat sync (different semantics)
  - All timestamps stored as BOTH Unix int AND ISO string for readability
  - StateManager is a class (not module-level functions) — testable,
    injectable, and mockable

USAGE
─────
    from utils.state_manager import StateManager

    sm = StateManager()                         # loads state/sync_state.json

    # Read last sync time for leads
    since_ts = sm.get_last_run_timestamp("leads")

    # After successful extraction:
    sm.mark_success("leads", records=1523, pages=7)

    # For chat sync (different timestamp semantics):
    sm.set_last_message_timestamp("chats", timestamp=1736847825)
    last_msg_ts = sm.get_last_message_timestamp("chats")

    # Check if an entity has ever been synced:
    if sm.has_been_synced("leads"):
        print("Incremental mode")
    else:
        print("First run — full extraction")
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_STATE_PATH  = Path("state/sync_state.json")
_SCHEMA_VERSION      = 1

# Valid entity keys (extend here when adding new extractors)
ENTITY_LEADS     = "leads"
ENTITY_TASKS     = "tasks"
ENTITY_PIPELINES = "pipelines"
ENTITY_CONTACTS  = "contacts"
ENTITY_CHATS     = "chats"
ENTITY_COMPANIES = "companies"

# Sync status values
STATUS_SUCCESS = "success"
STATUS_FAILED  = "failed"
STATUS_PARTIAL = "partial"   # Some records extracted, some failed


# ---------------------------------------------------------------------------
# EntityState — typed dict helper
# ---------------------------------------------------------------------------

def _empty_entity_state() -> dict[str, Any]:
    """
    Return an empty (never-synced) entity state dict.

    Provides a consistent default structure regardless of whether
    the state file exists or has a missing entity key.
    """
    return {
        "last_run_at":              None,   # Unix timestamp of last successful run
        "last_run_at_iso":          None,   # ISO 8601 string (human-readable)
        "last_message_timestamp":   None,   # For chat/conversation sync
        "last_message_timestamp_iso": None,
        "records_extracted":        0,
        "pages_fetched":            0,
        "status":                   None,   # "success" | "failed" | "partial"
        "error":                    None,   # Last error message (if status=failed)
    }


# ---------------------------------------------------------------------------
# StateManager
# ---------------------------------------------------------------------------

class StateManager:
    """
    Manages persistent sync state for incremental CRM extraction.

    Reads from and writes to a JSON file (default: state/sync_state.json).
    All writes are atomic (temp file → rename) to prevent corruption.

    Args:
        state_path: Override the default state file path.
                    Useful in tests (e.g. use a tmp_path fixture).
    """

    def __init__(self, state_path: str | Path | None = None) -> None:
        self._path: Path = Path(
            state_path
            or os.environ.get("SYNC_STATE_PATH", str(_DEFAULT_STATE_PATH))
        )
        self._state: dict[str, Any] = {}
        self._load()

    # =========================================================================
    # PUBLIC: Load / Save
    # =========================================================================

    def load_state(self) -> dict[str, Any]:
        """
        Reload state from disk and return a copy of the full state dict.

        Use this to get a fresh view after an external process may have
        updated the state file.

        Returns:
            Full state dict (safe copy — mutations do not affect internal state).
        """
        self._load()
        return dict(self._state)

    def save_state(self) -> Path:
        """
        Atomically persist the current in-memory state to disk.

        Returns:
            Path to the saved state file.
        """
        self._state["updated_at"] = datetime.now(tz=timezone.utc).isoformat()
        self._atomic_write(self._path, self._state)
        logger.debug("Sync state saved → %s", self._path)
        return self._path

    # =========================================================================
    # PUBLIC: Entity Queries
    # =========================================================================

    def has_been_synced(self, entity: str) -> bool:
        """
        Return True if the entity has at least one previous successful run.

        Use this to decide between full vs incremental extraction.

        Args:
            entity: Entity name (e.g. "leads", "tasks", "chats").

        Returns:
            True if last_run_at is set for this entity.

        Example:
            if sm.has_been_synced("leads"):
                since_ts = sm.get_last_run_timestamp("leads")
                extractor.extract_updated_since(since_ts)
            else:
                extractor.extract_all()
        """
        entity_state = self._get_entity(entity)
        return entity_state.get("last_run_at") is not None

    def get_last_run_timestamp(self, entity: str) -> int | None:
        """
        Get the Unix timestamp of the last successful extraction run.

        Use this as the `updated_at[from]` filter for incremental extraction.

        Args:
            entity: Entity name.

        Returns:
            Unix timestamp (int) or None if the entity has never been synced.

        Example:
            ts = sm.get_last_run_timestamp("leads")
            if ts:
                extractor.extract_updated_since(ts)
        """
        return self._get_entity(entity).get("last_run_at")

    def get_last_message_timestamp(self, entity: str) -> int | None:
        """
        Get the Unix timestamp of the last processed message/chat.

        This is semantically different from last_run_at — it tracks the
        most recent message timestamp (not the run time) to support
        resuming chat sync from exactly where it left off.

        Args:
            entity: Entity name (typically "chats").

        Returns:
            Unix timestamp (int) or None if no messages have been synced.

        Example:
            last_msg = sm.get_last_message_timestamp("chats")
            if last_msg:
                chats = fetch_chats_since(last_msg)
        """
        return self._get_entity(entity).get("last_message_timestamp")

    def get_entity_state(self, entity: str) -> dict[str, Any]:
        """
        Return a copy of the full state dict for a given entity.

        Args:
            entity: Entity name.

        Returns:
            Dict with all tracked fields for this entity.
        """
        return dict(self._get_entity(entity))

    def get_all_entities(self) -> dict[str, dict[str, Any]]:
        """
        Return a copy of the state for all tracked entities.

        Returns:
            Dict mapping entity name → entity state dict.
        """
        return {
            k: dict(v)
            for k, v in self._state.get("entities", {}).items()
        }

    # =========================================================================
    # PUBLIC: Entity Updates
    # =========================================================================

    def mark_success(
        self,
        entity: str,
        records: int = 0,
        pages: int = 0,
        run_timestamp: int | None = None,
    ) -> None:
        """
        Record a successful extraction run for an entity.

        Updates last_run_at to the run timestamp, clears any previous
        error, and saves state to disk.

        Args:
            entity:         Entity name (e.g. "leads").
            records:        Number of records successfully extracted.
            pages:          Number of API pages fetched.
            run_timestamp:  Unix timestamp for the run (defaults to now).

        Example:
            # After successful lead extraction:
            sm.mark_success("leads", records=result.total_records, pages=result.pages_fetched)
        """
        ts = run_timestamp or int(time.time())
        entity_state = self._get_entity(entity)
        entity_state.update({
            "last_run_at":       ts,
            "last_run_at_iso":   _ts_to_iso(ts),
            "records_extracted": records,
            "pages_fetched":     pages,
            "status":            STATUS_SUCCESS,
            "error":             None,
        })
        self._set_entity(entity, entity_state)
        self.save_state()
        logger.info(
            "State updated — entity=%s status=success records=%d last_run_at=%d",
            entity, records, ts,
        )

    def mark_failed(self, entity: str, error: str) -> None:
        """
        Record a failed extraction run for an entity.

        Preserves the previous last_run_at so the next successful run
        can still use it for incremental extraction. Saves state to disk.

        Args:
            entity: Entity name.
            error:  Error message or exception string.

        Example:
            try:
                extractor.extract_all()
                sm.mark_success("leads", ...)
            except KommoClientError as exc:
                sm.mark_failed("leads", error=str(exc))
        """
        entity_state = self._get_entity(entity)
        entity_state.update({
            "status": STATUS_FAILED,
            "error":  error[:500],   # Truncate very long error messages
        })
        self._set_entity(entity, entity_state)
        self.save_state()
        logger.warning(
            "State updated — entity=%s status=failed error=%s",
            entity, error[:120],
        )

    def mark_partial(
        self,
        entity: str,
        records: int,
        failed_records: int,
        error: str | None = None,
    ) -> None:
        """
        Record a partial extraction (some records succeeded, some failed).

        Used when validation errors sent records to dead-letter but the
        overall extraction run completed.

        Args:
            entity:         Entity name.
            records:        Records successfully extracted.
            failed_records: Records routed to dead-letter.
            error:          Optional description of the partial failure.
        """
        ts = int(time.time())
        entity_state = self._get_entity(entity)
        entity_state.update({
            "last_run_at":       ts,
            "last_run_at_iso":   _ts_to_iso(ts),
            "records_extracted": records,
            "status":            STATUS_PARTIAL,
            "error":             error or f"{failed_records} records failed validation",
        })
        self._set_entity(entity, entity_state)
        self.save_state()
        logger.warning(
            "State updated — entity=%s status=partial records=%d failed=%d",
            entity, records, failed_records,
        )

    def set_last_message_timestamp(self, entity: str, timestamp: int) -> None:
        """
        Update the last processed message/chat timestamp.

        Designed for chat/conversation sync where the cursor is the
        timestamp of the most recent message, not the run time.

        Args:
            entity:    Entity name (typically "chats").
            timestamp: Unix timestamp of the last processed message.

        Example:
            # After processing all chats up to this message:
            sm.set_last_message_timestamp("chats", timestamp=latest_msg_ts)
        """
        entity_state = self._get_entity(entity)
        entity_state.update({
            "last_message_timestamp":     timestamp,
            "last_message_timestamp_iso": _ts_to_iso(timestamp),
        })
        self._set_entity(entity, entity_state)
        self.save_state()
        logger.info(
            "Chat cursor updated — entity=%s last_message_timestamp=%d",
            entity, timestamp,
        )

    def reset_entity(self, entity: str) -> None:
        """
        Reset an entity's state as if it has never been synced.

        Use this to force a full re-extraction on the next run.

        Args:
            entity: Entity name to reset.
        """
        self._set_entity(entity, _empty_entity_state())
        self.save_state()
        logger.info("State reset for entity=%s", entity)

    def reset_all(self) -> None:
        """
        Reset state for ALL entities — forces full re-extraction everywhere.

        Use with caution. Creates a fresh state file.
        """
        self._state["entities"] = {}
        self.save_state()
        logger.warning("All sync state reset — next run will be a full extraction")

    # =========================================================================
    # PUBLIC: Reporting
    # =========================================================================

    def summary(self) -> str:
        """
        Return a human-readable summary of current sync state.

        Returns:
            Formatted multi-line string.
        """
        lines = ["Sync State Summary", "─" * 40]
        entities = self._state.get("entities", {})

        if not entities:
            lines.append("  No entities have been synced yet.")
            return "\n".join(lines)

        for name, state in entities.items():
            status    = state.get("status", "unknown")
            last_run  = state.get("last_run_at_iso", "never")
            records   = state.get("records_extracted", 0)
            last_msg  = state.get("last_message_timestamp_iso")

            icon = {"success": "✅", "failed": "❌", "partial": "⚠️"}.get(status, "○")
            lines.append(f"  {icon}  {name:<14} last_run={last_run}  records={records}")
            if last_msg:
                lines.append(f"              last_message={last_msg}")
            if state.get("error"):
                lines.append(f"              error={state['error'][:80]}")

        lines.append("─" * 40)
        lines.append(f"  Updated: {self._state.get('updated_at', 'never')}")
        return "\n".join(lines)

    # =========================================================================
    # PRIVATE: Internal state management
    # =========================================================================

    def _load(self) -> None:
        """
        Load state from disk into memory.

        If the file does not exist, initialises an empty state.
        If the file is corrupt, logs a warning and starts fresh.
        """
        if not self._path.exists():
            logger.debug("State file not found — initialising empty state: %s", self._path)
            self._state = self._empty_state()
            return

        try:
            raw  = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
            self._state = data
            logger.debug("Sync state loaded from %s", self._path)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Sync state file corrupt or unreadable (%s) — starting fresh: %s",
                self._path, exc,
            )
            self._state = self._empty_state()

    def _get_entity(self, entity: str) -> dict[str, Any]:
        """Return the state dict for a single entity, creating it if missing."""
        entities = self._state.setdefault("entities", {})
        if entity not in entities:
            entities[entity] = _empty_entity_state()
        return entities[entity]

    def _set_entity(self, entity: str, state: dict[str, Any]) -> None:
        """Write an entity state dict back into memory."""
        self._state.setdefault("entities", {})[entity] = state

    # =========================================================================
    # PRIVATE: File I/O
    # =========================================================================

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        """Return a brand-new, valid empty state dict."""
        return {
            "schema_version": _SCHEMA_VERSION,
            "updated_at":     datetime.now(tz=timezone.utc).isoformat(),
            "entities":       {},
        }

    @staticmethod
    def _atomic_write(path: Path, data: dict[str, Any]) -> None:
        """
        Write JSON to a temp file then atomically rename to the target.

        Prevents corrupt state files on crash or power loss.

        Args:
            path: Final target path.
            data: Data to serialise as pretty-printed JSON.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps(data, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    # =========================================================================
    # Dunder helpers
    # =========================================================================

    def __repr__(self) -> str:
        n = len(self._state.get("entities", {}))
        return f"StateManager(path={self._path!r}, entities={n})"


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------

def load_state(state_path: str | Path | None = None) -> dict[str, Any]:
    """
    Convenience function — load and return the full sync state dict.

    Args:
        state_path: Override path (defaults to state/sync_state.json).

    Returns:
        Full state dict.

    Example:
        from utils.state_manager import load_state
        state = load_state()
        print(state["entities"]["leads"]["last_run_at"])
    """
    return StateManager(state_path).load_state()


def save_state(
    updates: dict[str, Any],
    state_path: str | Path | None = None,
) -> Path:
    """
    Convenience function — merge updates into the state file and save.

    Performs a deep merge of `updates` into the existing state.
    Top-level keys in `updates` overwrite existing top-level keys.

    Args:
        updates:    Dict of updates to merge into state.
        state_path: Override path.

    Returns:
        Path to the saved state file.

    Example:
        from utils.state_manager import save_state
        save_state({"entities": {"leads": {"last_run_at": 1736847825}}})
    """
    sm = StateManager(state_path)
    _deep_merge(sm._state, updates)
    return sm.save_state()


def _deep_merge(base: dict[str, Any], updates: dict[str, Any]) -> None:
    """
    Recursively merge `updates` into `base` (modifies base in-place).

    Nested dicts are merged rather than replaced. All other types
    (lists, scalars) overwrite the base value.
    """
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def _ts_to_iso(ts: int) -> str:
    """Convert a Unix timestamp to an ISO 8601 UTC string."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
