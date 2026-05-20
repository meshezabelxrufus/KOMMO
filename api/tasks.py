"""
api/tasks.py
============
Task extraction module for the Kommo CRM integration.

KOMMO API BEHAVIOUR
───────────────────
  Endpoint : GET /api/v4/tasks
  Auth     : Bearer token (injected by KommoAPIClient)

  Response shape:
    {
      "_embedded": {
        "tasks": [
          {
            "id": 5001,
            "created_by": 7712,
            "responsible_user_id": 7712,
            "entity_id": 10482301,
            "entity_type": "leads",
            "is_completed": false,
            "task_type_id": 1,
            "text": "Follow up with client",
            "complete_till": 1736934225,
            "created_at": 1736847825,
            "updated_at": 1736847825
          }
        ]
      }
    }

  Key facts:
    - Paginated: page + limit (max 250/page), stops on HTTP 204.
    - Tasks link to entities via entity_type ("leads" | "contacts") + entity_id.
    - task_type_id: 1=Call, 2=Meeting, 3=Email (account-configurable).
    - complete_till is the task deadline as a Unix timestamp.
    - Completed tasks remain in the API — use is_completed filter if needed.

EXTRACTION FLOW
───────────────
  1. GET /api/v4/tasks?page=1&limit=250
  2. Parse _embedded.tasks
  3. Validate each dict against TaskRecord (Pydantic)
  4. Accumulate valid / route invalid to dead-letter
  5. Repeat until HTTP 204 or empty page
  6. Write outputs/tasks.json (atomic)

USAGE
─────
    from dotenv import load_dotenv
    load_dotenv()

    from auth.oauth import KommoOAuthClient
    from api.client import KommoAPIClient
    from api.tasks import TasksExtractor

    oauth = KommoOAuthClient()
    with KommoAPIClient(oauth) as client:
        extractor = TasksExtractor(client, output_dir="outputs")
        result    = extractor.extract_all()

    print(f"Extracted {result.total_records} tasks → {result.output_path}")
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from api.client import KommoAPIClient, KommoClientError, KommoNotFoundError
from utils.logger import get_logger
from utils.retry import retry_api_call

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_KOMMO_API_PATH   = "/tasks"
_EMBEDDED_KEY     = "tasks"
_DEFAULT_PAGE_SIZE  = 250
_DEFAULT_OUTPUT_DIR = Path("outputs")

# Kommo built-in task type IDs (account-configurable, these are defaults)
TASK_TYPE_CALL    = 1
TASK_TYPE_MEETING = 2
TASK_TYPE_EMAIL   = 3

_TASK_TYPE_LABELS: dict[int, str] = {
    TASK_TYPE_CALL:    "call",
    TASK_TYPE_MEETING: "meeting",
    TASK_TYPE_EMAIL:   "email",
}

# Valid entity types tasks can be linked to
_VALID_ENTITY_TYPES = {"leads", "contacts", "companies", "customers"}


# =============================================================================
# SlimTaskRecord — 6 core fields only
# =============================================================================

class SlimTaskRecord(BaseModel):
    """
    Minimal validated task record containing exactly the 6 core fields
    needed for most downstream use cases.

    Use this when you want a clean, lightweight output without the full
    set of computed/metadata fields that TaskRecord provides.

    Fields:
        task_id              : Kommo internal task ID
        text                 : Task description / notes
        entity_id            : ID of the linked CRM entity (lead/contact)
        due_date             : Deadline as ISO 8601 string (human-readable)
        due_date_unix        : Deadline as Unix timestamp (machine-readable)
        is_completed         : Whether the task has been completed
        responsible_user_id  : User ID assigned to complete the task
    """

    model_config = {"populate_by_name": True, "extra": "ignore"}

    task_id:             int       = Field(..., alias="id",   description="Kommo task ID")
    text:                str | None = Field(None,             description="Task description / notes")
    entity_id:           int | None = Field(None,            description="Linked CRM entity ID")
    due_date:            str | None = Field(None,            description="Deadline in ISO 8601 format")
    due_date_unix:       int | None = Field(None,            description="Deadline as Unix timestamp")
    is_completed:        bool       = Field(default=False,   description="Completion status")
    responsible_user_id: int | None = Field(None,            description="Assigned user ID")

    @model_validator(mode="before")
    @classmethod
    def map_due_date(cls, values: dict[str, Any]) -> dict[str, Any]:
        """
        Map Kommo's `complete_till` → `due_date_unix` + `due_date` (ISO string).

        Kommo stores the deadline as `complete_till` (Unix timestamp).
        We surface it as both the raw Unix value and a human-readable ISO string.
        """
        complete_till = values.get("complete_till")
        if complete_till:
            values["due_date_unix"] = complete_till
            values["due_date"] = datetime.fromtimestamp(
                complete_till, tz=timezone.utc
            ).isoformat()
        return values

    @classmethod
    def from_full(cls, task: "TaskRecord") -> "SlimTaskRecord":
        """
        Downcast a full TaskRecord to a SlimTaskRecord.

        Args:
            task: Validated TaskRecord instance.

        Returns:
            SlimTaskRecord with only the 6 core fields populated.
        """
        return cls(
            id=task.id,
            text=task.text,
            entity_id=task.entity_id,
            due_date=task.complete_till_iso,
            due_date_unix=task.complete_till,
            is_completed=task.is_completed,
            responsible_user_id=task.responsible_user_id,
        )


# =============================================================================
# Pydantic Model
# =============================================================================

class TaskRecord(BaseModel):
    """
    Validated, typed representation of a single Kommo task.

    Maps to the fields returned by GET /api/v4/tasks.
    All optional fields default to None — tasks in different states
    may have different fields populated.

    Attributes:
        id:                   Kommo internal task ID.
        created_by:           User ID who created the task.
        updated_by:           User ID who last modified the task.
        responsible_user_id:  User ID assigned to complete the task.
        group_id:             Group ID responsible for the task.
        entity_id:            ID of the linked CRM entity (lead/contact).
        entity_type:          Type of linked entity ("leads" | "contacts").
        is_completed:         Whether the task has been completed.
        task_type_id:         Task type (1=Call, 2=Meeting, 3=Email, custom).
        task_type_label:      Human-readable task type (computed).
        text:                 Task description / notes.
        duration:             Estimated duration in seconds.
        complete_till:        Deadline as Unix timestamp.
        complete_till_iso:    ISO 8601 deadline string (computed).
        result:               Completion result dict (set when completed).
        created_at:           Creation Unix timestamp.
        updated_at:           Last modification Unix timestamp.
        created_at_iso:       ISO 8601 creation string (computed).
        updated_at_iso:       ISO 8601 update string (computed).
        account_id:           Kommo account ID.
    """

    model_config = {"populate_by_name": True, "extra": "ignore"}

    # ------------------------------------------------------------------
    # Core identity
    # ------------------------------------------------------------------
    id: int = Field(..., description="Kommo task ID")

    # ------------------------------------------------------------------
    # Ownership
    # ------------------------------------------------------------------
    created_by:          int | None = Field(None, description="Creator user ID")
    updated_by:          int | None = Field(None, description="Last modifier user ID")
    responsible_user_id: int | None = Field(None, description="Assigned user ID")
    group_id:            int | None = Field(None, description="Assigned group ID")

    # ------------------------------------------------------------------
    # Linked entity
    # ------------------------------------------------------------------
    entity_id:   int | None = Field(None, description="Linked entity ID (lead/contact)")
    entity_type: str | None = Field(
        None,
        description="Linked entity type: 'leads' | 'contacts' | 'companies'",
    )

    # ------------------------------------------------------------------
    # Task details
    # ------------------------------------------------------------------
    is_completed:  bool      = Field(default=False, description="Completion status")
    task_type_id:  int | None = Field(None, description="Task type: 1=Call, 2=Meeting, 3=Email")
    task_type_label: str      = Field(default="unknown", description="Human-readable task type")
    text:          str | None = Field(None, description="Task description / notes")
    duration:      int | None = Field(None, description="Estimated duration (seconds)")
    complete_till: int | None = Field(None, description="Deadline Unix timestamp")

    # ------------------------------------------------------------------
    # Completion result (populated when is_completed=True)
    # ------------------------------------------------------------------
    result: dict[str, Any] | None = Field(
        None,
        description="Task result on completion (text, note, etc.)",
    )

    # ------------------------------------------------------------------
    # Timestamps
    # ------------------------------------------------------------------
    created_at: int | None = Field(None, description="Creation Unix timestamp")
    updated_at: int | None = Field(None, description="Last update Unix timestamp")
    account_id: int | None = Field(None)

    # ------------------------------------------------------------------
    # Computed — ISO strings and labels (not from API)
    # ------------------------------------------------------------------
    created_at_iso:   str | None = Field(None)
    updated_at_iso:   str | None = Field(None)
    complete_till_iso: str | None = Field(None, description="ISO 8601 deadline string")
    is_overdue:       bool        = Field(
        default=False,
        description="True if deadline has passed and task is not completed",
    )

    @model_validator(mode="after")
    def compute_derived_fields(self) -> "TaskRecord":
        """Compute human-readable labels and ISO timestamps after validation."""
        # Task type label
        if self.task_type_id is not None:
            self.task_type_label = _TASK_TYPE_LABELS.get(
                self.task_type_id, f"custom_{self.task_type_id}"
            )

        # ISO timestamps
        if self.created_at:
            self.created_at_iso = datetime.fromtimestamp(
                self.created_at, tz=timezone.utc
            ).isoformat()
        if self.updated_at:
            self.updated_at_iso = datetime.fromtimestamp(
                self.updated_at, tz=timezone.utc
            ).isoformat()
        if self.complete_till:
            self.complete_till_iso = datetime.fromtimestamp(
                self.complete_till, tz=timezone.utc
            ).isoformat()

        # Overdue flag
        if self.complete_till and not self.is_completed:
            self.is_overdue = time.time() > self.complete_till

        return self


# =============================================================================
# ExtractionResult
# =============================================================================

@dataclass
class TaskExtractionResult:
    """Summary returned by TasksExtractor.extract_all()."""

    entity:          str       = "tasks"
    total_records:   int       = 0
    failed_records:  int       = 0
    pages_fetched:   int       = 0
    completed_count: int       = 0
    overdue_count:   int       = 0
    output_path:     Path | None = None
    dead_letter_path: Path | None = None
    duration_seconds: float    = 0.0
    started_at:      str       = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )
    finished_at:     str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity":           self.entity,
            "total_records":    self.total_records,
            "failed_records":   self.failed_records,
            "pages_fetched":    self.pages_fetched,
            "completed_count":  self.completed_count,
            "overdue_count":    self.overdue_count,
            "output_path":      str(self.output_path),
            "dead_letter_path": str(self.dead_letter_path) if self.dead_letter_path else None,
            "duration_seconds": round(self.duration_seconds, 2),
            "started_at":       self.started_at,
            "finished_at":      self.finished_at,
        }


# =============================================================================
# TasksExtractor
# =============================================================================

class TasksExtractor:
    """
    Extracts all tasks from Kommo with full pagination and Pydantic validation.

    Identical pattern to LeadsExtractor — paginate, validate, persist.
    Adds computed fields (is_overdue, task_type_label, ISO timestamps)
    and task-specific summary statistics in ExtractionResult.

    Args:
        client:        Open KommoAPIClient instance.
        output_dir:    Directory for output JSON files.
        page_size:     Records per API page (max 250).
        extra_params:  Additional query params for filtering:
                         {"filter[is_completed]": 0}   — only open tasks
                         {"filter[entity_type]": "leads"} — tasks on leads
                         {"filter[responsible_user_id][]": 7712}
    """

    def __init__(
        self,
        client: KommoAPIClient,
        output_dir: str | Path = _DEFAULT_OUTPUT_DIR,
        page_size: int = _DEFAULT_PAGE_SIZE,
        extra_params: dict[str, Any] | None = None,
    ) -> None:
        self._client      = client
        self._output_dir  = Path(output_dir)
        self._error_dir   = self._output_dir / "errors"
        self._page_size   = min(page_size, 250)
        self._extra_params = extra_params or {}

    # =========================================================================
    # PUBLIC: Main Entry Point
    # =========================================================================

    @retry_api_call
    def extract_all(self) -> TaskExtractionResult:
        """
        Paginate all tasks, validate each record, and write to JSON.

        Returns:
            TaskExtractionResult with counts, file paths, and duration.

        Raises:
            KommoClientError: Unrecoverable API or network failure.
        """
        result  = TaskExtractionResult()
        started = time.monotonic()

        logger.info("Task extraction started", extra={"page_size": self._page_size})

        all_tasks:    list[dict[str, Any]] = []
        failed_tasks: list[dict[str, Any]] = []

        try:
            for page_num, raw_page in enumerate(
                self._client.paginate(
                    path=_KOMMO_API_PATH,
                    resource=_EMBEDDED_KEY,
                    page_size=self._page_size,
                    params=self._extra_params,
                ),
                start=1,
            ):
                valid, failed = self._validate_page(raw_page, page_num)
                all_tasks.extend(valid)
                failed_tasks.extend(failed)
                result.pages_fetched = page_num

                logger.info(
                    "Tasks page processed",
                    extra={
                        "page":         page_num,
                        "valid":        len(valid),
                        "failed":       len(failed),
                        "total_so_far": len(all_tasks),
                    },
                )

        except KommoNotFoundError:
            logger.info("No tasks found in account (404 from API)")

        except KommoClientError as exc:
            logger.error(
                "Task extraction failed",
                extra={"status_code": exc.status_code, "error": str(exc)},
            )
            raise

        # ------------------------------------------------------------------
        # Compute summary statistics from validated records
        # ------------------------------------------------------------------
        result.total_records   = len(all_tasks)
        result.failed_records  = len(failed_tasks)
        result.completed_count = sum(1 for t in all_tasks if t.get("is_completed"))
        result.overdue_count   = sum(1 for t in all_tasks if t.get("is_overdue"))

        # ------------------------------------------------------------------
        # Persist
        # ------------------------------------------------------------------
        if all_tasks:
            result.output_path = self._write_json("tasks.json", all_tasks)

        if failed_tasks:
            result.dead_letter_path = self._write_dead_letter(failed_tasks)
            logger.warning(
                "Some task records failed validation",
                extra={
                    "failed_count":    len(failed_tasks),
                    "dead_letter_path": str(result.dead_letter_path),
                },
            )

        result.duration_seconds = time.monotonic() - started
        result.finished_at = datetime.now(tz=timezone.utc).isoformat()

        logger.info("Task extraction complete", extra=result.as_dict())
        return result

    # =========================================================================
    # PUBLIC: Filtered Extraction Helpers
    # =========================================================================

    def extract_open_tasks(self) -> TaskExtractionResult:
        """
        Extract only incomplete (open) tasks.

        Useful when you only care about actionable tasks, not historical data.
        """
        self._extra_params["filter[is_completed]"] = 0
        return self.extract_all()

    def extract_for_entity(
        self,
        entity_type: str,
        entity_id: int | None = None,
    ) -> TaskExtractionResult:
        """
        Extract tasks linked to a specific entity type (and optionally ID).

        Args:
            entity_type: "leads" | "contacts" | "companies"
            entity_id:   Optional specific entity ID.

        Returns:
            TaskExtractionResult for the filtered batch.

        Example:
            # All tasks linked to leads
            result = extractor.extract_for_entity("leads")

            # Tasks for a specific lead
            result = extractor.extract_for_entity("leads", entity_id=10482301)
        """
        if entity_type not in _VALID_ENTITY_TYPES:
            raise ValueError(
                f"Invalid entity_type '{entity_type}'. "
                f"Must be one of: {sorted(_VALID_ENTITY_TYPES)}"
            )
        self._extra_params["filter[entity_type]"] = entity_type
        if entity_id:
            self._extra_params["filter[entity_id][]"] = entity_id
        return self.extract_all()

    def extract_updated_since(self, unix_timestamp: int) -> TaskExtractionResult:
        """
        Extract only tasks updated after the given Unix timestamp.

        Args:
            unix_timestamp: Lower bound for updated_at filter.
        """
        original_params = dict(self._extra_params)
        self._extra_params["filter[updated_at][from]"] = unix_timestamp
        try:
            return self.extract_all()
        finally:
            self._extra_params = original_params  # Always restore — prevents filter leaking into future calls

    def extract_slim(self, filename: str = "tasks_slim.json") -> tuple[TaskExtractionResult, Path | None]:
        """
        Extract all tasks and write a lean 6-field JSON alongside the full output.

        This is the most targeted extraction mode — produces a compact JSON
        containing exactly:
            task_id, text, entity_id, due_date, is_completed, responsible_user_id

        The full tasks.json is also written (same run, no extra API calls).

        Args:
            filename: Output filename for the slim JSON (default: tasks_slim.json).

        Returns:
            (TaskExtractionResult, slim_output_path) tuple.

        Example:
            with KommoAPIClient(oauth) as client:
                extractor = TasksExtractor(client)
                result, slim_path = extractor.extract_slim()

            print(f"Full:  {result.output_path}")
            print(f"Slim:  {slim_path}")
        """
        from pydantic import ValidationError

        result  = TaskExtractionResult()
        started = time.monotonic()
        slim_path: Path | None = None

        logger.info("Slim task extraction started", extra={"page_size": self._page_size})

        all_full:  list[dict[str, Any]] = []   # Full records → tasks.json
        all_slim:  list[dict[str, Any]] = []   # Slim records → tasks_slim.json
        failed:    list[dict[str, Any]] = []

        try:
            for page_num, raw_page in enumerate(
                self._client.paginate(
                    path=_KOMMO_API_PATH,
                    resource=_EMBEDDED_KEY,
                    page_size=self._page_size,
                    params=self._extra_params,
                ),
                start=1,
            ):
                for raw in raw_page:
                    try:
                        full_record = TaskRecord.model_validate(raw)
                        slim_record = SlimTaskRecord.from_full(full_record)
                        all_full.append(full_record.model_dump(mode="json"))
                        all_slim.append(slim_record.model_dump(mode="json", by_alias=False))
                    except ValidationError as exc:
                        task_id = raw.get("id", "unknown")
                        logger.warning(
                            "Task validation failed — routing to dead-letter",
                            extra={"task_id": task_id, "page": page_num},
                        )
                        failed.append({
                            "_raw":               raw,
                            "_validation_errors": exc.errors(include_url=False),
                            "_page":              page_num,
                        })

                result.pages_fetched = page_num
                logger.info(
                    "Tasks page processed",
                    extra={"page": page_num, "records": len(raw_page)},
                )

        except KommoNotFoundError:
            logger.info("No tasks found in account (404 from API)")

        except KommoClientError as exc:
            logger.error("Task extraction failed", extra={"error": str(exc)})
            raise

        result.total_records   = len(all_full)
        result.failed_records  = len(failed)
        result.completed_count = sum(1 for t in all_full if t.get("is_completed"))
        result.overdue_count   = sum(1 for t in all_full if t.get("is_overdue"))

        if all_full:
            result.output_path = self._write_json("tasks.json", all_full)
            slim_path = self._write_slim_json(filename, all_slim)

        if failed:
            result.dead_letter_path = self._write_dead_letter(failed)

        result.duration_seconds = time.monotonic() - started
        result.finished_at = datetime.now(tz=timezone.utc).isoformat()

        logger.info(
            "Slim task extraction complete",
            extra={**result.as_dict(), "slim_path": str(slim_path)},
        )
        return result, slim_path

    # =========================================================================
    # PRIVATE: Validation
    # =========================================================================

    def _validate_page(
        self,
        raw_records: list[dict[str, Any]],
        page_num: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """
        Validate a page of raw task dicts against TaskRecord.

        Args:
            raw_records: Raw dicts from _embedded.tasks API response.
            page_num:    Current page number for error context.

        Returns:
            (valid_records, failed_records) tuple.
        """
        from pydantic import ValidationError

        valid:  list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []

        for raw in raw_records:
            try:
                record = TaskRecord.model_validate(raw)
                valid.append(record.model_dump(mode="json"))
            except ValidationError as exc:
                task_id = raw.get("id", "unknown")
                logger.warning(
                    "Task validation failed — routing to dead-letter",
                    extra={
                        "task_id":           task_id,
                        "page":              page_num,
                        "validation_errors": exc.error_count(),
                    },
                )
                failed.append({
                    "_raw":               raw,
                    "_validation_errors": exc.errors(include_url=False),
                    "_page":              page_num,
                })

        return valid, failed

    # =========================================================================
    # PRIVATE: File I/O
    # =========================================================================

    def _write_json(self, filename: str, records: list[dict[str, Any]]) -> Path:
        """Atomically write task records to a JSON file with metadata envelope."""
        self._output_dir.mkdir(parents=True, exist_ok=True)
        output_path = self._output_dir / filename

        completed = sum(1 for r in records if r.get("is_completed"))
        overdue   = sum(1 for r in records if r.get("is_overdue"))

        envelope: dict[str, Any] = {
            "_meta": {
                "entity":          "tasks",
                "count":           len(records),
                "completed_count": completed,
                "open_count":      len(records) - completed,
                "overdue_count":   overdue,
                "extracted_at":    datetime.now(tz=timezone.utc).isoformat(),
                "source":          "kommo_api_v4",
            },
            "data": records,
        }

        self._atomic_write(output_path, envelope)
        logger.info(
            "Tasks written to disk",
            extra={"path": str(output_path), "count": len(records)},
        )
        return output_path

    def _write_slim_json(self, filename: str, slim_records: list[dict[str, Any]]) -> Path:
        """
        Write the 6-field slim task records to a separate JSON file.

        Output envelope matches the full tasks.json structure for consistency.

        Args:
            filename:     Output filename (e.g. "tasks_slim.json").
            slim_records: List of SlimTaskRecord.model_dump() dicts.

        Returns:
            Path to the written slim file.
        """
        self._output_dir.mkdir(parents=True, exist_ok=True)
        output_path = self._output_dir / filename

        completed = sum(1 for r in slim_records if r.get("is_completed"))

        envelope: dict[str, Any] = {
            "_meta": {
                "entity":          "tasks",
                "variant":         "slim",
                "fields":          ["task_id", "text", "entity_id", "due_date",
                                    "is_completed", "responsible_user_id"],
                "count":           len(slim_records),
                "completed_count": completed,
                "open_count":      len(slim_records) - completed,
                "extracted_at":    datetime.now(tz=timezone.utc).isoformat(),
                "source":          "kommo_api_v4",
            },
            "data": slim_records,
        }

        self._atomic_write(output_path, envelope)
        logger.info(
            "Slim tasks written to disk",
            extra={"path": str(output_path), "count": len(slim_records)},
        )
        return output_path

    def _write_dead_letter(self, failed: list[dict[str, Any]]) -> Path:
        """Write validation-failed task records to dead-letter file."""
        self._error_dir.mkdir(parents=True, exist_ok=True)
        ts      = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
        dl_path = self._error_dir / f"tasks_failed_{ts}.json"

        envelope: dict[str, Any] = {
            "_meta": {
                "entity":     "tasks",
                "type":       "dead_letter",
                "count":      len(failed),
                "created_at": datetime.now(tz=timezone.utc).isoformat(),
                "note":       "These records failed Pydantic validation.",
            },
            "data": failed,
        }

        self._atomic_write(dl_path, envelope)
        return dl_path

    @staticmethod
    def _atomic_write(path: Path, data: dict[str, Any]) -> None:
        """Write JSON to a temp file then atomically rename to the target."""
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
