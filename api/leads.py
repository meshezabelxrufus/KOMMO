"""
api/leads.py
============
Lead extraction module for the Kommo CRM integration.

RESPONSIBILITY
──────────────
This module owns everything related to lead data:
  - LeadRecord   : Validated Pydantic model for a single lead
  - LeadsExtractor: Paginates /api/v4/leads, validates, and saves to JSON

EXTRACTION FLOW
───────────────
  1. LeadsExtractor.extract_all() is called
  2. Internally calls KommoAPIClient.paginate("/leads")
     → yields pages of raw dicts
  3. Each raw dict is validated against LeadRecord (Pydantic)
     → Valid records → accumulated to results list
     → Invalid records → logged + written to dead-letter file
  4. After all pages: results written to outputs/leads.json
  5. Returns ExtractionResult summary (counts, path, duration)

KOMMO API NOTES
───────────────
  - Endpoint : GET /api/v4/leads
  - Response : { "_embedded": { "leads": [...] } }
  - Pagination: page + limit params (max 250/page)
  - HTTP 204  : End of data (no more records on this page)
  - Custom fields are stored raw — their IDs are account-specific

USAGE
─────
    from dotenv import load_dotenv
    load_dotenv()

    from auth.oauth import KommoOAuthClient
    from api.client import KommoAPIClient
    from api.leads import LeadsExtractor

    oauth  = KommoOAuthClient()

    with KommoAPIClient(oauth) as client:
        extractor = LeadsExtractor(client, output_dir="outputs")
        result = extractor.extract_all()

    print(f"Extracted {result.total_records} leads → {result.output_path}")
    print(f"Failed:   {result.failed_records}")
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

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
_KOMMO_API_PATH = "/leads"
_EMBEDDED_KEY   = "leads"
_DEFAULT_PAGE_SIZE = 250
_DEFAULT_OUTPUT_DIR = Path("outputs")


# =============================================================================
# LeadRecord — Pydantic Model
# =============================================================================

class LeadRecord(BaseModel):
    """
    Validated, typed representation of a single Kommo lead.

    Maps to the fields returned by GET /api/v4/leads.
    All optional fields default to None — Kommo may omit them for
    leads in certain states (e.g. closed_at is null for open leads).

    Pydantic v2 automatically:
      - Coerces compatible types (e.g. "123" → 123 for int fields)
      - Strips extra fields not in the model (model_config)
      - Raises ValidationError for structurally invalid records
    """

    model_config = {"extra": "ignore"}   # Silently ignore unknown fields

    # ------------------------------------------------------------------
    # Core identity
    # ------------------------------------------------------------------
    id: int = Field(..., description="Kommo internal lead ID (primary key)")
    name: str | None = Field(None, description="Lead title / name")

    # ------------------------------------------------------------------
    # Pipeline & stage
    # ------------------------------------------------------------------
    pipeline_id: int | None = Field(
        None,
        description="ID of the pipeline this lead belongs to",
    )
    status_id: int | None = Field(
        None,
        description="Current pipeline stage (status) ID",
    )

    # ------------------------------------------------------------------
    # Ownership
    # ------------------------------------------------------------------
    responsible_user_id: int | None = Field(
        None,
        description="ID of the user responsible for this lead",
    )
    group_id: int | None = Field(
        None,
        description="ID of the group responsible for this lead",
    )

    # ------------------------------------------------------------------
    # Timestamps (Unix epoch seconds from Kommo)
    # ------------------------------------------------------------------
    created_at: int | None = Field(None, description="Lead creation Unix timestamp")
    updated_at: int | None = Field(None, description="Last modification Unix timestamp")
    closed_at: int | None = Field(None, description="Closure Unix timestamp (null if open)")

    # ------------------------------------------------------------------
    # Financial
    # ------------------------------------------------------------------
    price: int | None = Field(None, description="Lead value / budget in account currency")
    loss_reason_id: int | None = Field(None, description="Loss reason ID if the lead was lost")

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------
    is_deleted: bool = Field(default=False)
    score: int | None = Field(None, description="Computed lead score (if scoring enabled)")
    account_id: int | None = Field(None, description="Kommo account ID")

    # ------------------------------------------------------------------
    # Raw / dynamic fields (stored as-is — IDs are account-specific)
    # ------------------------------------------------------------------
    custom_fields_values: list[dict[str, Any]] | None = Field(
        None,
        description="Account-specific custom fields (raw, validated only as list)",
    )
    tags: list[dict[str, Any]] | None = Field(
        None,
        description="Tags attached to the lead (from _embedded.tags if requested)",
    )

    # ------------------------------------------------------------------
    # Computed helpers (not from API — added during extraction)
    # ------------------------------------------------------------------
    created_at_iso: str | None = Field(
        None,
        description="ISO 8601 representation of created_at (added during extraction)",
    )
    updated_at_iso: str | None = Field(
        None,
        description="ISO 8601 representation of updated_at (added during extraction)",
    )

    @model_validator(mode="after")
    def compute_iso_timestamps(self) -> "LeadRecord":
        """Convert Unix timestamps → ISO 8601 strings for readability."""
        if self.created_at:
            self.created_at_iso = datetime.fromtimestamp(
                self.created_at, tz=timezone.utc
            ).isoformat()
        if self.updated_at:
            self.updated_at_iso = datetime.fromtimestamp(
                self.updated_at, tz=timezone.utc
            ).isoformat()
        return self


# =============================================================================
# ExtractionResult — Summary object returned after extraction
# =============================================================================

@dataclass
class ExtractionResult:
    """
    Summary returned by LeadsExtractor.extract_all().

    Provides counts, file path, and timing for logging and orchestration.
    """

    entity: str = "leads"
    total_records: int = 0
    failed_records: int = 0
    pages_fetched: int = 0
    output_path: Path | None = None
    dead_letter_path: Path | None = None
    duration_seconds: float = 0.0
    started_at: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )
    finished_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a log-friendly dict representation."""
        return {
            "entity": self.entity,
            "total_records": self.total_records,
            "failed_records": self.failed_records,
            "pages_fetched": self.pages_fetched,
            "output_path": str(self.output_path),
            "dead_letter_path": str(self.dead_letter_path) if self.dead_letter_path else None,
            "duration_seconds": round(self.duration_seconds, 2),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


# =============================================================================
# LeadsExtractor
# =============================================================================

class LeadsExtractor:
    """
    Extracts all leads from Kommo with full pagination, validation, and persistence.

    Args:
        client:     An open KommoAPIClient instance (use as context manager).
        output_dir: Directory where leads.json and error files are written.
                    Defaults to "outputs/". Created automatically if missing.
        page_size:  Records per API page (max 250, Kommo's limit).
        extra_params: Additional query parameters sent with every page request.
                      Useful for filtering, e.g.:
                        {"filter[updated_at][from]": 1718000000}
                        {"with": "contacts,tags"}

    Example:
        with KommoAPIClient(oauth) as client:
            extractor = LeadsExtractor(client, output_dir="outputs")
            result    = extractor.extract_all()
    """

    def __init__(
        self,
        client: KommoAPIClient,
        output_dir: str | Path = _DEFAULT_OUTPUT_DIR,
        page_size: int = _DEFAULT_PAGE_SIZE,
        extra_params: dict[str, Any] | None = None,
    ) -> None:
        self._client = client
        self._output_dir = Path(output_dir)
        self._error_dir  = self._output_dir / "errors"
        self._page_size  = min(page_size, 250)
        self._extra_params = extra_params or {}

    # =========================================================================
    # PUBLIC: Main Entry Point
    # =========================================================================

    @retry_api_call
    def extract_all(self) -> ExtractionResult:
        """
        Paginate through all leads, validate each record, and write to JSON.

        The method never raises on individual record validation failures —
        it logs them and routes them to a dead-letter file instead.
        It DOES raise on unrecoverable API errors (network down, auth failed).

        Returns:
            ExtractionResult with total counts, file paths, and duration.

        Raises:
            KommoClientError: Unrecoverable API or network failure.
        """
        result = ExtractionResult()
        started = time.monotonic()

        logger.info("Lead extraction started", extra={"page_size": self._page_size})

        all_leads: list[dict[str, Any]] = []
        failed_leads: list[dict[str, Any]] = []

        # ------------------------------------------------------------------
        # Paginate through /leads
        # ------------------------------------------------------------------
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
                all_leads.extend(valid)
                failed_leads.extend(failed)
                result.pages_fetched = page_num

                logger.info(
                    "Leads page processed",
                    extra={
                        "page": page_num,
                        "valid": len(valid),
                        "failed": len(failed),
                        "total_so_far": len(all_leads),
                    },
                )

        except KommoNotFoundError:
            # Account has no leads — not an error
            logger.info("No leads found in account (404 from API)")

        except KommoClientError as exc:
            # Log and re-raise — caller decides how to handle
            logger.error(
                "Lead extraction failed with API error",
                extra={
                    "status_code": exc.status_code,
                    "error": str(exc),
                    "records_collected_before_failure": len(all_leads),
                },
            )
            raise

        # ------------------------------------------------------------------
        # Persist valid leads
        # ------------------------------------------------------------------
        result.total_records = len(all_leads)
        result.failed_records = len(failed_leads)

        if all_leads:
            result.output_path = self._write_json(
                filename="leads.json",
                records=all_leads,
            )

        # ------------------------------------------------------------------
        # Persist dead-letter records (validation failures)
        # ------------------------------------------------------------------
        if failed_leads:
            result.dead_letter_path = self._write_dead_letter(failed_leads)
            logger.warning(
                "Some lead records failed validation — written to dead-letter file",
                extra={
                    "failed_count": len(failed_leads),
                    "dead_letter_path": str(result.dead_letter_path),
                },
            )

        # ------------------------------------------------------------------
        # Finalise result
        # ------------------------------------------------------------------
        result.duration_seconds = time.monotonic() - started
        result.finished_at = datetime.now(tz=timezone.utc).isoformat()

        logger.info("Lead extraction complete", extra=result.as_dict())

        return result

    # =========================================================================
    # PUBLIC: Targeted Extraction Helpers
    # =========================================================================

    def extract_updated_since(self, unix_timestamp: int) -> ExtractionResult:
        """
        Extract only leads updated after the given Unix timestamp.

        Useful for incremental runs (Milestone 2).

        Args:
            unix_timestamp: Only fetch leads updated at or after this time.

        Returns:
            ExtractionResult for the incremental batch.

        Example:
            import time
            # Get leads updated in the last 24 hours
            since = int(time.time()) - 86400
            result = extractor.extract_updated_since(since)
        """
        original_params = dict(self._extra_params)
        self._extra_params["filter[updated_at][from]"] = unix_timestamp
        try:
            return self.extract_all()
        finally:
            self._extra_params = original_params  # Always restore — prevents filter leaking into future calls

    def fetch_single(self, lead_id: int) -> LeadRecord | None:
        """
        Fetch a single lead by its ID.

        Args:
            lead_id: Kommo lead ID.

        Returns:
            Validated LeadRecord, or None if the lead does not exist.
        """
        logger.info("Fetching single lead", extra={"lead_id": lead_id})
        try:
            response = self._client.get(f"/leads/{lead_id}")
            raw = response.json()
            return LeadRecord.model_validate(raw)
        except KommoNotFoundError:
            logger.warning("Lead not found", extra={"lead_id": lead_id})
            return None
        except Exception as exc:
            logger.error("Failed to fetch lead", extra={"lead_id": lead_id, "error": str(exc)})
            raise

    # =========================================================================
    # PRIVATE: Validation
    # =========================================================================

    def _validate_page(
        self,
        raw_records: list[dict[str, Any]],
        page_num: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """
        Validate a page of raw lead dicts against LeadRecord.

        Args:
            raw_records: Raw dicts from the _embedded.leads API response.
            page_num:    Current page number (for error logging context).

        Returns:
            (valid_records, failed_records) — both as serialisable dicts.
            Valid records are already serialised via .model_dump() for JSON output.
        """
        from pydantic import ValidationError

        valid: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []

        for raw in raw_records:
            try:
                record = LeadRecord.model_validate(raw)
                valid.append(record.model_dump(mode="json"))
            except ValidationError as exc:
                lead_id = raw.get("id", "unknown")
                logger.warning(
                    "Lead validation failed — routing to dead-letter",
                    extra={
                        "lead_id": lead_id,
                        "page": page_num,
                        "validation_errors": exc.error_count(),
                        "errors": exc.errors(include_url=False),
                    },
                )
                # Preserve the raw record + validation error for debugging
                failed.append({
                    "_raw": raw,
                    "_validation_errors": exc.errors(include_url=False),
                    "_page": page_num,
                })

        return valid, failed

    # =========================================================================
    # PRIVATE: File I/O
    # =========================================================================

    def _write_json(self, filename: str, records: list[dict[str, Any]]) -> Path:
        """
        Atomically write records to a JSON file in the output directory.

        Wraps the data in a metadata envelope:
            {
                "_meta": { "count": N, "extracted_at": "...", "entity": "leads" },
                "data": [ ... ]
            }

        Args:
            filename: Output filename (e.g. "leads.json").
            records:  List of serialised LeadRecord dicts.

        Returns:
            Path to the written file.
        """
        self._output_dir.mkdir(parents=True, exist_ok=True)
        output_path = self._output_dir / filename

        envelope: dict[str, Any] = {
            "_meta": {
                "entity": "leads",
                "count": len(records),
                "extracted_at": datetime.now(tz=timezone.utc).isoformat(),
                "source": "kommo_api_v4",
            },
            "data": records,
        }

        self._atomic_write(output_path, envelope)

        logger.info(
            "Leads written to disk",
            extra={"path": str(output_path), "count": len(records)},
        )
        return output_path

    def _write_dead_letter(self, failed_records: list[dict[str, Any]]) -> Path:
        """
        Write validation-failed records to a dead-letter file.

        Args:
            failed_records: Raw records + their validation errors.

        Returns:
            Path to the dead-letter file.
        """
        self._error_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
        dl_path = self._error_dir / f"leads_failed_{timestamp}.json"

        envelope: dict[str, Any] = {
            "_meta": {
                "entity": "leads",
                "type": "dead_letter",
                "count": len(failed_records),
                "created_at": datetime.now(tz=timezone.utc).isoformat(),
                "note": "These records failed Pydantic validation. Fix root cause and replay.",
            },
            "data": failed_records,
        }

        self._atomic_write(dl_path, envelope)
        return dl_path

    @staticmethod
    def _atomic_write(path: Path, data: dict[str, Any]) -> None:
        """
        Write data to a temp file then atomically rename to the target path.

        Prevents partial/corrupted files if the process is interrupted.

        Args:
            path: Final destination path.
            data: Data to serialise as pretty-printed JSON.
        """
        tmp_path = path.with_suffix(".tmp")
        try:
            tmp_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            tmp_path.replace(path)   # Atomic on POSIX, near-atomic on Windows
        except OSError:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise
