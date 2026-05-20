"""
extractors/tasks.py
===================
Task extraction with full pagination and Pydantic validation.

Kommo tasks can belong to different entity types (leads, contacts).
This extractor fetches all tasks by default but accepts filters to
narrow by entity_type or completion status.

Extraction is structurally identical to leads — same pagination loop,
same dead-letter pattern.

Usage:
    from extractors.tasks import TaskExtractor

    extractor = TaskExtractor(client=kommo_client, writer=json_writer)
    tasks = extractor.extract_all()
    # tasks is List[TaskModel]
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from api.kommo_client import KommoClient
from outputs.json_writer import JsonWriter
from utils.logger import get_logger

log = get_logger(__name__)

MAX_PAGES = 10_000


# =============================================================================
# Pydantic Models
# =============================================================================

class TaskModel(BaseModel):
    """
    Validated representation of a Kommo task record.

    Maps to /api/v4/tasks response fields.
    """

    id: int = Field(..., description="Task ID")
    created_by: int | None = Field(None, description="User ID who created the task")
    updated_by: int | None = Field(None, description="User ID who last updated the task")
    created_at: int | None = Field(None, description="Creation Unix timestamp")
    updated_at: int | None = Field(None, description="Last update Unix timestamp")
    responsible_user_id: int | None = Field(None, description="Assigned user ID")
    group_id: int | None = Field(None, description="Assigned group ID")
    entity_id: int | None = Field(None, description="ID of the linked entity (lead/contact)")
    entity_type: str | None = Field(
        None,
        description="Type of linked entity: 'leads' or 'contacts'",
    )
    is_completed: bool = Field(default=False, description="Whether the task is completed")
    task_type_id: int | None = Field(None, description="Task type (call, meeting, etc.)")
    text: str | None = Field(None, description="Task description / notes")
    duration: int | None = Field(None, description="Estimated duration in seconds")
    complete_till: int | None = Field(
        None,
        description="Deadline as Unix timestamp",
    )
    result: dict[str, Any] | None = Field(None, description="Task completion result")
    account_id: int | None = Field(None)


# =============================================================================
# Extractor
# =============================================================================

class TaskExtractor:
    """
    Extracts all tasks from Kommo with full pagination.

    Args:
        client: KommoClient instance (open HTTP session).
        writer: JsonWriter for persisting results + dead-letter records.
    """

    def __init__(self, client: KommoClient, writer: JsonWriter) -> None:
        self._client = client
        self._writer = writer

    def extract_all(self) -> list[TaskModel]:
        """
        Paginate through /api/v4/tasks and return all validated tasks.

        Returns:
            List of TaskModel instances.

        Raises:
            KommoAPIError: On unrecoverable API errors.
        """
        # TODO: Implement pagination loop (mirrors LeadExtractor.extract_all)
        raise NotImplementedError("TaskExtractor.extract_all — to be implemented in Phase 4")

    def _validate_records(
        self,
        raw_records: list[dict[str, Any]],
    ) -> tuple[list[TaskModel], list[dict[str, Any]]]:
        """Validate raw task dicts against TaskModel."""
        # TODO: Implement per-record Pydantic validation with try/except
        raise NotImplementedError
