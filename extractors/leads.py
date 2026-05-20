"""
extractors/leads.py
===================
Lead extraction with full pagination and Pydantic validation.

Extraction flow:
  1. Start at page 1 with limit=250
  2. For each page: fetch raw dicts from KommoClient
  3. Validate each raw dict against LeadModel
  4. Accumulate valid records; send invalid to dead-letter file
  5. Stop when page returns 0 records (HTTP 204 or empty _embedded)
  6. Return all validated LeadModel instances

LeadModel captures the core fields needed for Milestone 1.
Custom fields are stored as a raw dict for maximum flexibility —
their IDs are account-specific and change between Kommo installations.

Usage:
    from extractors.leads import LeadExtractor
    from api.kommo_client import KommoClient
    from outputs.json_writer import JsonWriter

    extractor = LeadExtractor(client=kommo_client, writer=json_writer)
    leads = extractor.extract_all()
    # leads is List[LeadModel]
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from api.kommo_client import KommoClient
from outputs.json_writer import JsonWriter
from utils.logger import get_logger

log = get_logger(__name__)

# Safety cap: stop pagination after this many pages regardless of response
MAX_PAGES = 10_000


# =============================================================================
# Pydantic Model
# =============================================================================

class LeadModel(BaseModel):
    """
    Validated representation of a Kommo lead record.

    Maps to /api/v4/leads response fields.
    Non-required fields default to None to handle partial API responses.
    """

    id: int = Field(..., description="Kommo internal lead ID")
    name: str | None = Field(None, description="Lead name / title")
    price: int | None = Field(None, description="Lead budget / value")
    status_id: int | None = Field(None, description="Current pipeline stage ID")
    pipeline_id: int | None = Field(None, description="Pipeline the lead belongs to")
    responsible_user_id: int | None = Field(None, description="Assigned user ID")
    group_id: int | None = Field(None, description="Assigned group ID")
    created_by: int | None = Field(None, description="User ID who created the lead")
    updated_by: int | None = Field(None, description="User ID who last updated the lead")
    created_at: int | None = Field(None, description="Creation Unix timestamp")
    updated_at: int | None = Field(None, description="Last update Unix timestamp")
    closed_at: int | None = Field(None, description="Closure Unix timestamp (if closed)")
    loss_reason_id: int | None = Field(None, description="Loss reason ID (if lost)")
    is_deleted: bool = Field(default=False)
    score: int | None = Field(None, description="Lead score")
    account_id: int | None = Field(None)
    custom_fields_values: list[dict[str, Any]] | None = Field(
        None,
        description="Account-specific custom field values (raw, not validated)",
    )
    tags: list[dict[str, Any]] | None = Field(None, description="Tags attached to the lead")


# =============================================================================
# Extractor
# =============================================================================

class LeadExtractor:
    """
    Extracts all leads from Kommo with full pagination.

    Args:
        client: KommoClient instance (open HTTP session).
        writer: JsonWriter for persisting results + dead-letter records.
    """

    def __init__(self, client: KommoClient, writer: JsonWriter) -> None:
        self._client = client
        self._writer = writer

    def extract_all(self) -> list[LeadModel]:
        """
        Paginate through /api/v4/leads and return all validated leads.

        Returns:
            List of LeadModel instances for all leads in the account.

        Raises:
            KommoAPIError: On unrecoverable API errors.
        """
        # TODO: Implement pagination loop
        # for page in range(1, MAX_PAGES + 1):
        #     raw_records = self._client.get_leads(page=page)
        #     if not raw_records:
        #         break
        #     validated, failed = self._validate_records(raw_records)
        #     all_leads.extend(validated)
        #     if failed: self._writer.write_dead_letter("leads", failed, page)
        #     log.info("leads_page_extracted", page=page, count=len(validated))
        raise NotImplementedError("LeadExtractor.extract_all — to be implemented in Phase 4")

    def _validate_records(
        self,
        raw_records: list[dict[str, Any]],
    ) -> tuple[list[LeadModel], list[dict[str, Any]]]:
        """
        Validate raw API dicts against LeadModel.

        Returns:
            (valid_records, failed_records) tuple.
            Failed records are raw dicts preserved for dead-letter logging.
        """
        # TODO: Implement per-record Pydantic validation with try/except
        raise NotImplementedError
