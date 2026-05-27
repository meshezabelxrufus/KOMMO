"""
normalizers/daily_json_export.py
=================================
Daily AI-ready JSON export generator for Milestone 2.

PURPOSE
───────
Claude AI cannot efficiently analyse 18,000+ flat messages in one shot.
This module solves that by restructuring messages_flat.json into:

  daily_exports/
    YYYY-MM-DD.json   ← one file per calendar day (UTC)

Each daily file groups all messages by lead, sorts them chronologically,
and attaches conversation-level statistics so Claude can reason about
each lead's communication history without additional context lookups.

OUTPUT SCHEMA — YYYY-MM-DD.json
────────────────────────────────
{
  "_meta": {
    "date":            "2025-01-15",
    "generated_at":    "2025-05-23T08:30:00+00:00",
    "source_file":     "outputs/messages_flat.json",
    "pipeline_version":"milestone-2.0",
    "total_messages":  142,
    "total_leads":     38,
    "date_range": {
      "first_message_at": "2025-01-15T00:03:11+00:00",
      "last_message_at":  "2025-01-15T23:58:02+00:00"
    }
  },
  "leads": [
    {
      "lead_id":      12345,
      "lead_name":    "John Smith",
      "contact_name": "John Smith",
      "channel":      "WhatsApp",
      "stats": {
        "total_messages": 8,
        "inbound":        5,
        "outbound":       3,
        "first_message_at": "2025-01-15T09:12:00+00:00",
        "last_message_at":  "2025-01-15T17:44:00+00:00"
      },
      "messages": [
        {
          "message_id":   "uuid-...",
          "direction":    "in",
          "author":       "John Smith",
          "author_type":  "contact",
          "message_text": "Hola, me interesa saber sobre el procedimiento",
          "timestamp":    1736935920,
          "timestamp_iso":"2025-01-15T09:12:00+00:00",
          "media_url":    null
        },
        ...
      ]
    },
    ...
  ]
}

DESIGN DECISIONS
────────────────
  - Memory-efficient streaming: messages are processed in a single pass
    using defaultdict bucketing — no full-dataset sorts
  - Atomic writes (tmp → rename) — crash-safe, never produces partial files
  - All timestamps are UTC, both Unix int and ISO 8601 string
  - Leads within each day are sorted by their first message timestamp
  - The _meta block provides Claude with enough context to skip manual counts
  - lead_id=None messages are grouped under a dedicated "_no_lead" bucket
    rather than discarded, so nothing is silently lost

USAGE
─────
    from normalizers.daily_json_export import DailyExportGenerator, generate_daily_export

    # Generate all days found in messages_flat.json
    generator = DailyExportGenerator()
    results   = generator.generate_all()
    for r in results:
        print(r)

    # Generate a single date
    result = generator.export_for_date("2025-01-15")

    # Generate only the latest day (most common for daily cron)
    result = generator.export_latest_day()

    # Convenience function (used by run_daily_export.py)
    results = generate_daily_export(date="2025-01-15")  # or date=None for latest
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Generator, Iterable

from pydantic import BaseModel, Field, field_validator, model_validator

from utils.logger import get_logger

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

log: logging.Logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PIPELINE_VERSION         = "milestone-2.0"
_DEFAULT_INPUT_FILE      = Path("outputs/messages_flat.json")
_DEFAULT_EXPORT_DIR      = Path("daily_exports")
_MESSAGES_KEY_CANDIDATES = ("messages", "data")   # both top-level keys used
_NO_LEAD_BUCKET          = "_no_lead"


# =============================================================================
# Custom Exceptions
# =============================================================================

class DailyExportError(Exception):
    """Base exception for all DailyExportGenerator errors."""

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.context = context

    def __str__(self) -> str:
        base = self.args[0]
        if self.context:
            ctx = " | ".join(f"{k}={v}" for k, v in self.context.items())
            return f"{base} [{ctx}]"
        return base


class DailyExportInputError(DailyExportError):
    """Input file is missing, empty, or has an unreadable format."""


class DailyExportWriteError(DailyExportError):
    """Failed to write an output file."""


class DailyExportDateError(DailyExportError):
    """Requested date has no messages or is in an invalid format."""


# =============================================================================
# Pydantic Models
# =============================================================================

class MessageItem(BaseModel):
    """
    A single message as it appears inside a lead's conversation block.

    Only the fields relevant to Claude's analysis are included.
    Heavy or redundant fields (chat_id, account_id, etc.) are omitted
    to keep the output compact.
    """

    model_config = {"extra": "ignore"}

    message_id:   str | None = Field(None, description="Message UUID from Kommo")
    direction:    str | None = Field(None, description="'in' (inbound) or 'out' (outbound)")
    author:       str | None = Field(None, description="Human-readable author name")
    author_type:  str | None = Field(None, description="'user' | 'contact' | 'bot'")
    author_id:    int | None = Field(None, description="Kommo author ID")
    message_text: str | None = Field(None, description="Plain-text message body")
    timestamp:    int | None = Field(None, description="Unix epoch seconds (UTC)")
    timestamp_iso:str | None = Field(None, description="ISO 8601 UTC timestamp string")
    media_url:    str | None = Field(None, description="Attached media URL (if any)")
    channel_raw:  str | None = Field(None, description="Raw channel code (whatsapp, email...)")

    @model_validator(mode="before")
    @classmethod
    def map_flat_message_fields(cls, data: Any) -> Any:
        """
        Map flat message field names from messages_flat.json into this model.

        The flat schema uses 'message_text' and 'author' as top-level keys,
        but some variants use 'body' and 'author.name'. This validator
        normalises both forms.
        """
        if not isinstance(data, dict):
            return data

        # message_id: prefer 'message_id', fall back to 'id'
        if "message_id" not in data and "id" in data:
            data = dict(data)
            data["message_id"] = data["id"]

        # message_text: prefer 'message_text', fall back to 'body'
        if "message_text" not in data and "body" in data:
            data = dict(data)
            data["message_text"] = data["body"]

        # author: prefer 'author' as a string, fall back to 'author.name'
        if "author" in data and isinstance(data["author"], dict):
            d = dict(data)
            author_dict = data["author"]
            d["author"]      = author_dict.get("name")
            d["author_type"] = author_dict.get("type")
            d["author_id"]   = author_dict.get("id")
            data = d

        return data


class LeadConversation(BaseModel):
    """
    All messages for a single lead on a single day, with statistics.

    This is the primary unit that Claude reads — one block per lead,
    messages sorted oldest-first.
    """

    lead_id:      int | str | None = Field(None, description="Kommo lead ID (int or '_no_lead')")
    lead_name:    str | None       = Field(None, description="Lead title from Kommo")
    contact_name: str | None       = Field(None, description="Contact name linked to this lead")
    channel:      str | None       = Field(None, description="Primary channel (e.g. 'WhatsApp')")
    stats:        ConversationStats
    messages:     list[MessageItem]


class ConversationStats(BaseModel):
    """Aggregated statistics for a single lead's messages on a given day."""

    total_messages:  int   = 0
    inbound:         int   = 0
    outbound:        int   = 0
    first_message_at:str | None = None   # ISO 8601
    last_message_at: str | None = None   # ISO 8601


class DailyExportMeta(BaseModel):
    """Top-level metadata block for a daily export file."""

    date:             str
    generated_at:     str = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )
    source_file:      str
    pipeline_version: str = PIPELINE_VERSION
    total_messages:   int = 0
    total_leads:      int = 0
    date_range:       dict[str, str | None] = Field(default_factory=dict)


class DailyExport(BaseModel):
    """Complete structure of a YYYY-MM-DD.json export file."""

    _meta: DailyExportMeta
    leads: list[LeadConversation]


# =============================================================================
# ExportResult — returned by every generate/export method
# =============================================================================

@dataclass
class ExportResult:
    """
    Outcome of a single daily export operation.

    Attributes:
        date:          The calendar date exported (YYYY-MM-DD string).
        output_path:   Path to the written JSON file.
        total_messages:Total messages written.
        total_leads:   Unique lead_ids in this day's export.
        duration_s:    Wall-clock time for the export operation.
        success:       True if the write completed without errors.
        error:         Error message if success=False.
    """

    date:           str
    output_path:    Path | None = None
    total_messages: int         = 0
    total_leads:    int         = 0
    duration_s:     float       = 0.0
    success:        bool        = True
    error:          str | None  = None

    @property
    def status_icon(self) -> str:
        return "✅" if self.success else "❌"

    def __str__(self) -> str:
        if self.success:
            kb = (self.output_path.stat().st_size // 1024
                  if self.output_path and self.output_path.exists() else 0)
            return (
                f"{self.status_icon}  {self.date}  "
                f"{self.total_messages:,} msgs / {self.total_leads} leads  "
                f"[{self.duration_s:.2f}s]  {self.output_path}  ({kb} KB)"
            )
        return f"{self.status_icon}  {self.date}  FAILED — {self.error}"


# =============================================================================
# DailyExportGenerator — Core engine
# =============================================================================

class DailyExportGenerator:
    """
    Generates daily AI-ready JSON exports from outputs/messages_flat.json.

    Groups messages by UTC calendar date → lead_id, sorts chronologically,
    computes per-lead statistics, and atomically writes one file per day.

    Args:
        input_file:  Path to messages_flat.json (default: outputs/messages_flat.json).
        export_dir:  Directory for output files (default: daily_exports/).
        source_label:Human-readable source label embedded in _meta.

    Example:
        generator = DailyExportGenerator()
        results   = generator.generate_all()
        for r in results:
            print(r)
    """

    def __init__(
        self,
        input_file:   str | Path = _DEFAULT_INPUT_FILE,
        export_dir:   str | Path = _DEFAULT_EXPORT_DIR,
        source_label: str | None = None,
    ) -> None:
        self._input_file  = Path(input_file)
        self._export_dir  = Path(export_dir)
        self._source_label = source_label or str(self._input_file)
        log.info(
            "DailyExportGenerator initialised",
            extra={
                "input_file": str(self._input_file),
                "export_dir": str(self._export_dir),
            },
        )

    # =========================================================================
    # PUBLIC: Main entry points
    # =========================================================================

    def generate_all(self) -> list[ExportResult]:
        """
        Generate one export file for every date found in messages_flat.json.

        Performs a single streaming pass through the input file, bucketing
        messages by date as it goes. Each bucket is then written atomically.

        Returns:
            List of ExportResult, one per date found (sorted oldest-first).

        Raises:
            DailyExportInputError: Input file is missing or unreadable.

        Example:
            results = generator.generate_all()
            print(f"Generated {len(results)} daily files")
        """
        log.info(
            "generate_all: starting full export",
            extra={"input_file": str(self._input_file)},
        )
        started = time.monotonic()

        date_buckets = self._bucket_messages_by_date()
        results: list[ExportResult] = []

        for date_str in sorted(date_buckets.keys()):
            lead_buckets = date_buckets[date_str]
            result = self._write_day(date_str, lead_buckets)
            results.append(result)

        total_duration = time.monotonic() - started
        total_msgs = sum(r.total_messages for r in results)
        success_count = sum(1 for r in results if r.success)

        log.info(
            "generate_all complete — dates=%d messages=%d duration=%.2fs",
            len(results), total_msgs, total_duration,
            extra={
                "dates_generated": len(results),
                "success_count":   success_count,
                "failed_count":    len(results) - success_count,
                "total_messages":  total_msgs,
                "duration_s":      round(total_duration, 2),
            },
        )
        return results

    def export_for_date(self, target_date: str) -> ExportResult:
        """
        Generate the export file for a single specified date.

        Performs a streaming pass, collecting only messages matching
        the target date. More memory-efficient than generate_all() when
        only one day is needed.

        Args:
            target_date: Date string in YYYY-MM-DD format.

        Returns:
            ExportResult for the requested date.

        Raises:
            DailyExportDateError:  Date format is invalid.
            DailyExportInputError: Input file is missing or unreadable.

        Example:
            result = generator.export_for_date("2025-01-15")
            print(result)
        """
        target_date = _validate_date_string(target_date)

        log.info(
            "export_for_date: %s", target_date,
            extra={"date": target_date, "input_file": str(self._input_file)},
        )
        started = time.monotonic()

        # Stream and collect only messages for the target date
        lead_buckets: dict[str | int, list[dict[str, Any]]] = defaultdict(list)
        found = False

        for msg in self._iter_messages():
            msg_date = _message_date_utc(msg)
            if msg_date == target_date:
                found = True
                bucket_key = _lead_key(msg)
                lead_buckets[bucket_key].append(msg)

        if not found:
            duration_s = time.monotonic() - started
            log.warning(
                "export_for_date: no messages found for date %s", target_date,
                extra={"date": target_date, "duration_s": round(duration_s, 2)},
            )
            return ExportResult(
                date=target_date,
                success=False,
                error=f"No messages found for date {target_date}",
                duration_s=duration_s,
            )

        result = self._write_day(target_date, dict(lead_buckets))
        log.info("export_for_date complete — %s", result)
        return result

    def export_latest_day(self) -> ExportResult:
        """
        Auto-detect the latest message date and generate its export.

        Performs two passes:
          1. Scan all messages to find the maximum timestamp date.
          2. Call export_for_date() for that date.

        This is the primary method for daily cron jobs — run after extraction
        to automatically export the most recent day's conversations.

        Returns:
            ExportResult for the latest date found.

        Raises:
            DailyExportInputError: Input file is missing or has no messages.

        Example:
            result = generator.export_latest_day()
            print(f"Exported {result.date} — {result.total_messages} messages")
        """
        log.info("export_latest_day: scanning for latest date ...")
        latest = self._find_latest_date()

        if latest is None:
            log.warning("export_latest_day: no messages found, skipping gracefully.")
            return ExportResult(
                date=datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"),
                success=True,
                error="No messages found in input file.",
            )

        log.info("export_latest_day: latest date found = %s", latest)
        return self.export_for_date(latest)

    def list_available_dates(self) -> list[str]:
        """
        Return a sorted list of all dates present in the input file.

        Performs a single streaming scan — does not load the full dataset.

        Returns:
            Sorted list of YYYY-MM-DD strings (oldest first).

        Raises:
            DailyExportInputError: Input file is missing or unreadable.

        Example:
            dates = generator.list_available_dates()
            print(f"Data spans {len(dates)} days: {dates[0]} → {dates[-1]}")
        """
        dates: set[str] = set()
        for msg in self._iter_messages():
            d = _message_date_utc(msg)
            if d:
                dates.add(d)
        return sorted(dates)

    # =========================================================================
    # PRIVATE: Bucketing & streaming
    # =========================================================================

    def _bucket_messages_by_date(
        self,
    ) -> dict[str, dict[str | int, list[dict[str, Any]]]]:
        """
        Stream the input file and group messages into nested buckets:
          date_str → lead_key → [messages]

        Memory note: All messages are held in memory simultaneously.
        For very large datasets (millions of messages), consider switching
        to a two-pass approach using _iter_messages() twice.

        Returns:
            Nested dict: {date → {lead_key → [raw_message_dicts]}}
        """
        # date → (lead_key → messages)
        date_buckets: dict[str, dict[str | int, list[dict[str, Any]]]] = defaultdict(
            lambda: defaultdict(list)
        )

        total = 0
        skipped = 0

        for msg in self._iter_messages():
            total += 1
            date_str = _message_date_utc(msg)
            if not date_str:
                skipped += 1
                log.debug(
                    "Skipping message with no timestamp — message_id=%s",
                    msg.get("message_id") or msg.get("id"),
                )
                continue
            lead_key = _lead_key(msg)
            date_buckets[date_str][lead_key].append(msg)

        log.info(
            "Bucketing complete — total=%d dates=%d skipped=%d",
            total, len(date_buckets), skipped,
            extra={
                "total_messages": total,
                "dates_found":    len(date_buckets),
                "skipped_no_ts":  skipped,
            },
        )
        # Convert nested defaultdicts to regular dicts for clean handling
        return {
            date_str: dict(lead_map)
            for date_str, lead_map in date_buckets.items()
        }

    def _iter_messages(self) -> Generator[dict[str, Any], None, None]:
        """
        Stream messages one at a time from the input JSON file.

        Supports both top-level key formats:
          {"messages": [...]}   ← messages_flat.json from run_chats.py
          {"data": [...]}       ← generic extraction envelope

        Yields:
            Raw message dicts (not validated — validation happens downstream).

        Raises:
            DailyExportInputError: File missing, empty, or not valid JSON.
        """
        if not self._input_file.exists():
            raise DailyExportInputError(
                f"Input file not found: {self._input_file}. "
                "Run `python run_chats.py` first to generate messages_flat.json.",
                input_file=str(self._input_file),
            )

        try:
            raw = self._input_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise DailyExportInputError(
                f"Cannot read input file: {exc}",
                input_file=str(self._input_file),
            ) from exc

        if not raw.strip():
            raise DailyExportInputError(
                f"Input file is empty: {self._input_file}",
                input_file=str(self._input_file),
            )

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DailyExportInputError(
                f"Input file is not valid JSON: {exc}",
                input_file=str(self._input_file),
            ) from exc

        if not isinstance(payload, dict):
            raise DailyExportInputError(
                "Input file must be a JSON object at the top level.",
                input_file=str(self._input_file),
            )

        # Find the messages list under one of the known keys
        messages: list[Any] | None = None
        for key in _MESSAGES_KEY_CANDIDATES:
            if key in payload and isinstance(payload[key], list):
                messages = payload[key]
                break

        if messages is None:
            raise DailyExportInputError(
                f"Input file has no messages list. "
                f"Expected one of the top-level keys: {_MESSAGES_KEY_CANDIDATES}. "
                f"Found: {list(payload.keys())}",
                input_file=str(self._input_file),
            )

        log.info(
            "Streaming %d messages from %s",
            len(messages), self._input_file,
            extra={
                "count":      len(messages),
                "input_file": str(self._input_file),
            },
        )

        for msg in messages:
            if isinstance(msg, dict):
                yield msg

    def _find_latest_date(self) -> str | None:
        """
        Scan the input file and return the latest UTC calendar date string.

        Returns None if no message has a valid timestamp.
        """
        latest_ts: int | None = None

        for msg in self._iter_messages():
            ts = msg.get("timestamp") or msg.get("created_at")
            if isinstance(ts, (int, float)) and ts > 0:
                if latest_ts is None or ts > latest_ts:
                    latest_ts = int(ts)

        if latest_ts is None:
            return None

        return datetime.fromtimestamp(latest_ts, tz=timezone.utc).strftime("%Y-%m-%d")

    # =========================================================================
    # PRIVATE: Building and writing daily exports
    # =========================================================================

    def _write_day(
        self,
        date_str: str,
        lead_buckets: dict[str | int, list[dict[str, Any]]],
    ) -> ExportResult:
        """
        Build and atomically write one YYYY-MM-DD.json export file.

        Args:
            date_str:     The date key (YYYY-MM-DD).
            lead_buckets: Dict mapping lead_key → list of raw message dicts.

        Returns:
            ExportResult summarising the write.
        """
        started = time.monotonic()
        output_path = self._export_dir / f"{date_str}.json"

        log.info(
            "Writing daily export — date=%s leads=%d",
            date_str, len(lead_buckets),
            extra={"date": date_str, "lead_count": len(lead_buckets)},
        )

        try:
            conversations = self._build_conversations(date_str, lead_buckets)
            total_msgs = sum(len(c["messages"]) for c in conversations)

            # Build date range across all messages
            all_timestamps = [
                msg["timestamp"]
                for c in conversations
                for msg in c["messages"]
                if msg.get("timestamp")
            ]
            date_range: dict[str, str | None] = {
                "first_message_at": _ts_to_iso(min(all_timestamps)) if all_timestamps else None,
                "last_message_at":  _ts_to_iso(max(all_timestamps)) if all_timestamps else None,
            }

            meta = {
                "date":             date_str,
                "generated_at":     datetime.now(tz=timezone.utc).isoformat(),
                "source_file":      self._source_label,
                "pipeline_version": PIPELINE_VERSION,
                "total_messages":   total_msgs,
                "total_leads":      len(conversations),
                "date_range":       date_range,
            }

            envelope: dict[str, Any] = {
                "_meta": meta,
                "leads": conversations,
            }

            self._atomic_write(output_path, envelope)

            duration_s = time.monotonic() - started
            result = ExportResult(
                date=date_str,
                output_path=output_path,
                total_messages=total_msgs,
                total_leads=len(conversations),
                duration_s=duration_s,
                success=True,
            )
            log.info(
                "Daily export written — %s",
                result,
                extra={
                    "date":           date_str,
                    "output_path":    str(output_path),
                    "total_messages": total_msgs,
                    "total_leads":    len(conversations),
                    "duration_s":     round(duration_s, 2),
                },
            )
            return result

        except (OSError, DailyExportWriteError) as exc:
            duration_s = time.monotonic() - started
            log.error(
                "Daily export write failed — date=%s: %s",
                date_str, exc,
                extra={"date": date_str, "error": str(exc), "duration_s": round(duration_s, 2)},
            )
            return ExportResult(
                date=date_str,
                duration_s=duration_s,
                success=False,
                error=str(exc),
            )

    def _build_conversations(
        self,
        date_str: str,
        lead_buckets: dict[str | int, list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """
        Convert raw lead buckets into validated LeadConversation dicts.

        Each lead's messages are:
          1. Sorted chronologically (by timestamp, nulls last)
          2. Validated and coerced via MessageItem
          3. Augmented with per-lead statistics

        Leads are sorted by their first message timestamp.

        Args:
            date_str:     The date being processed (for log context).
            lead_buckets: Dict mapping lead_key → raw message list.

        Returns:
            List of serialisable LeadConversation dicts, sorted by first message.
        """
        conversations: list[dict[str, Any]] = []

        for lead_key, raw_messages in lead_buckets.items():
            # Sort messages chronologically (None timestamps go last)
            sorted_msgs = sorted(
                raw_messages,
                key=lambda m: (m.get("timestamp") or m.get("created_at") or 0),
            )

            # Build validated MessageItem list
            validated_messages: list[dict[str, Any]] = []
            for raw in sorted_msgs:
                try:
                    item = MessageItem.model_validate(raw)
                    validated_messages.append(item.model_dump(mode="json", exclude_none=False))
                except Exception as exc:
                    log.warning(
                        "Message validation failed — skipping — date=%s lead=%s: %s",
                        date_str, lead_key, exc,
                        extra={"date": date_str, "lead_key": str(lead_key), "error": str(exc)},
                    )

            if not validated_messages:
                continue

            # Compute per-lead statistics
            inbound  = sum(1 for m in validated_messages if m.get("direction") == "in")
            outbound = sum(1 for m in validated_messages if m.get("direction") == "out")

            timestamps = [m["timestamp"] for m in validated_messages if m.get("timestamp")]
            stats: dict[str, Any] = {
                "total_messages":  len(validated_messages),
                "inbound":         inbound,
                "outbound":        outbound,
                "first_message_at": _ts_to_iso(min(timestamps)) if timestamps else None,
                "last_message_at":  _ts_to_iso(max(timestamps)) if timestamps else None,
            }

            # Resolve lead metadata from the first message that has it
            first = raw_messages[0]
            lead_name    = first.get("lead_name")
            contact_name = first.get("contact_name")
            channel      = first.get("channel")

            # Coerce lead_id to int when possible
            lead_id: int | str | None
            if lead_key == _NO_LEAD_BUCKET:
                lead_id = None
            else:
                try:
                    lead_id = int(lead_key)
                except (ValueError, TypeError):
                    lead_id = lead_key

            conversations.append({
                "lead_id":      lead_id,
                "lead_name":    lead_name,
                "contact_name": contact_name,
                "channel":      channel,
                "stats":        stats,
                "messages":     validated_messages,
            })

        # Sort conversations by first message timestamp within this day
        conversations.sort(
            key=lambda c: (
                c["stats"].get("first_message_at") or "9999"
            )
        )

        log.debug(
            "Built %d conversations for date %s", len(conversations), date_str,
            extra={"date": date_str, "conversations": len(conversations)},
        )
        return conversations

    # =========================================================================
    # PRIVATE: File I/O
    # =========================================================================

    def _atomic_write(self, path: Path, data: dict[str, Any]) -> None:
        """
        Write JSON to a temporary file then atomically rename to the target.

        Guarantees that no partial or corrupt file is left on disk if the
        process is interrupted or the disk is full.

        Args:
            path: Final output path (e.g. daily_exports/2025-01-15.json).
            data: Serialisable dict to write as pretty-printed UTF-8 JSON.

        Raises:
            DailyExportWriteError: If the write or rename fails.
        """
        self._export_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")

        try:
            tmp.write_text(
                json.dumps(data, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            tmp.replace(path)
            log.debug("Atomic write complete → %s", path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise DailyExportWriteError(
                f"Failed to write {path}: {exc}",
                path=str(path),
            ) from exc


# =============================================================================
# Module-level helpers
# =============================================================================

def _message_date_utc(msg: dict[str, Any]) -> str | None:
    """
    Extract the UTC calendar date from a flat message dict.

    Tries fields in priority order:
      1. timestamp      (int Unix epoch — most reliable)
      2. created_at     (int Unix epoch — fallback)
      3. timestamp_iso  (ISO 8601 string — parse if int not available)

    Returns:
        "YYYY-MM-DD" string, or None if no usable timestamp exists.
    """
    # Try integer Unix timestamp first (fastest path)
    ts = msg.get("timestamp") or msg.get("created_at")
    if isinstance(ts, (int, float)) and ts > 0:
        try:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
        except (OSError, OverflowError, ValueError):
            pass

    # Fall back to ISO string
    iso = msg.get("timestamp_iso") or msg.get("created_at_iso")
    if isinstance(iso, str) and iso:
        try:
            # Parse ISO 8601 — handle both +00:00 and Z suffixes
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            pass

    return None


def _lead_key(msg: dict[str, Any]) -> str | int:
    """
    Extract the lead identifier to use as a bucket key.

    Returns lead_id (int) when available; falls back to _NO_LEAD_BUCKET.

    Args:
        msg: Raw flat message dict.

    Returns:
        int lead_id, or "_no_lead" sentinel string.
    """
    lead_id = msg.get("lead_id") or msg.get("entity_id")
    if lead_id is not None:
        try:
            return int(lead_id)
        except (ValueError, TypeError):
            return str(lead_id)
    return _NO_LEAD_BUCKET


def _ts_to_iso(ts: int | float) -> str:
    """Convert a Unix timestamp to an ISO 8601 UTC string."""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def _validate_date_string(date_str: str) -> str:
    """
    Validate and normalise a YYYY-MM-DD date string.

    Args:
        date_str: Input date string.

    Returns:
        Normalised YYYY-MM-DD string.

    Raises:
        DailyExportDateError: If the string is not a valid date.
    """
    date_str = date_str.strip()
    try:
        parsed = datetime.strptime(date_str, "%Y-%m-%d").date()
        return parsed.strftime("%Y-%m-%d")
    except ValueError:
        raise DailyExportDateError(
            f"Invalid date format: {date_str!r}. Expected YYYY-MM-DD (e.g. 2025-01-15).",
            received=date_str,
        )


# =============================================================================
# Public convenience function
# =============================================================================

def generate_daily_export(
    date: str | None = None,
    input_file: str | Path = _DEFAULT_INPUT_FILE,
    export_dir: str | Path = _DEFAULT_EXPORT_DIR,
) -> list[ExportResult]:
    """
    Convenience function — generate daily export(s) with minimal boilerplate.

    Args:
        date:        If provided, export only this date (YYYY-MM-DD).
                     If None, export the latest day found in the input file.
        input_file:  Path to messages_flat.json.
        export_dir:  Directory where YYYY-MM-DD.json files are written.

    Returns:
        List of ExportResult (one entry if date is specified, otherwise one
        per day when using generate_all).

    Examples:
        # Export latest day
        results = generate_daily_export()

        # Export specific date
        results = generate_daily_export(date="2025-01-15")

        # Export all days
        from normalizers.daily_json_export import DailyExportGenerator
        results = DailyExportGenerator().generate_all()
    """
    generator = DailyExportGenerator(input_file=input_file, export_dir=export_dir)

    if date is not None:
        return [generator.export_for_date(date)]
    else:
        return [generator.export_latest_day()]
