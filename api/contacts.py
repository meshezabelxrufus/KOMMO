"""
api/contacts.py
===============
Contact extraction module for the Kommo CRM integration.

RESPONSIBILITY
──────────────
This module extracts all contacts from Kommo and their linked leads.
Contacts are essential for the AI analysis pipeline — they provide the
person-level identity (name, phone, email) needed to attribute messages
to specific individuals in conversation data.

KOMMO API NOTES
───────────────
  Endpoint  : GET /api/v4/contacts
  Response  : { "_embedded": { "contacts": [...] } }
  Pagination: page + limit (max 250/page), stops on HTTP 204
  Linked leads are in _embedded.leads (request with with=leads)

  Contact → Lead relationship:
    Each contact can be linked to multiple leads.
    The primary link is stored in _embedded.leads[0].

EXTRACTION FLOW
───────────────
  1. GET /api/v4/contacts?page=1&limit=250&with=leads
  2. Parse _embedded.contacts
  3. Validate each dict against ContactRecord (Pydantic)
  4. Extract linked lead IDs from _embedded.leads
  5. Accumulate valid / route invalid to dead-letter
  6. Repeat until HTTP 204 or empty page
  7. Write outputs/contacts.json (atomic)

USAGE
─────
    from auth.oauth import KommoOAuthClient
    from api.client import KommoAPIClient
    from api.contacts import ContactsExtractor

    oauth = KommoOAuthClient()
    with KommoAPIClient(oauth) as client:
        extractor = ContactsExtractor(client, output_dir="outputs")
        result    = extractor.extract_all()

    print(f"Extracted {result.total_records} contacts → {result.output_path}")
"""

from __future__ import annotations

import json
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
_KOMMO_API_PATH    = "/contacts"
_EMBEDDED_KEY      = "contacts"
_DEFAULT_PAGE_SIZE = 250
_DEFAULT_OUTPUT_DIR = Path("outputs")

# Request embedded linked leads alongside contacts
_DEFAULT_WITH_PARAMS = "leads"


# =============================================================================
# CustomField — nested model for Kommo custom field values
# =============================================================================

class CustomFieldValue(BaseModel):
    """A single value within a contact's custom field."""
    model_config = {"extra": "ignore"}

    field_id:   int | None  = Field(None, description="Custom field definition ID")
    field_name: str | None  = Field(None, description="Custom field display name")
    field_code: str | None  = Field(None, description="System code (e.g. 'PHONE', 'EMAIL')")
    field_type: str | None  = Field(None, description="Field type (text, multitext, etc.)")
    values:     list[dict[str, Any]] | None = Field(
        None,
        description="List of value dicts — each has 'value' and optionally 'enum_code'",
    )


# =============================================================================
# ContactRecord — Pydantic Model
# =============================================================================

class ContactRecord(BaseModel):
    """
    Validated, typed representation of a single Kommo contact.

    Maps to GET /api/v4/contacts response fields.
    Linked leads are extracted from _embedded.leads when requested
    with the `with=leads` query parameter.

    Custom fields (PHONE, EMAIL, etc.) are stored in both raw form
    (`custom_fields_values`) and as extracted convenience fields
    (`phone_numbers`, `email_addresses`).
    """

    model_config = {"extra": "ignore"}

    # ------------------------------------------------------------------
    # Core identity
    # ------------------------------------------------------------------
    id:   int  = Field(..., description="Kommo internal contact ID (primary key)")
    name: str | None = Field(None, description="Contact full name")
    first_name: str | None = Field(None, description="First name (if split)")
    last_name:  str | None = Field(None, description="Last name (if split)")

    # ------------------------------------------------------------------
    # Ownership
    # ------------------------------------------------------------------
    responsible_user_id: int | None = Field(
        None, description="ID of user responsible for this contact"
    )
    group_id: int | None = Field(None, description="Responsible team group ID")
    created_by: int | None = Field(None, description="User ID who created this contact")

    # ------------------------------------------------------------------
    # Timestamps
    # ------------------------------------------------------------------
    created_at: int | None = Field(None, description="Creation Unix timestamp")
    updated_at: int | None = Field(None, description="Last modification Unix timestamp")

    # ------------------------------------------------------------------
    # Linked entities
    # ------------------------------------------------------------------
    linked_leads_ids: list[int] = Field(
        default_factory=list,
        description="IDs of all leads linked to this contact",
    )
    closest_task_at: int | None = Field(
        None, description="Unix timestamp of the nearest open task"
    )

    # ------------------------------------------------------------------
    # Custom fields (raw)
    # ------------------------------------------------------------------
    custom_fields_values: list[dict[str, Any]] | None = Field(
        None,
        description="Account-specific custom fields (raw Kommo format)",
    )

    # ------------------------------------------------------------------
    # Extracted contact details (derived from custom_fields_values)
    # ------------------------------------------------------------------
    phone_numbers: list[str] = Field(
        default_factory=list,
        description="All phone numbers extracted from custom fields",
    )
    email_addresses: list[str] = Field(
        default_factory=list,
        description="All email addresses extracted from custom fields",
    )

    # ------------------------------------------------------------------
    # ISO timestamps (computed)
    # ------------------------------------------------------------------
    created_at_iso: str | None = Field(None, description="ISO 8601 of created_at")
    updated_at_iso: str | None = Field(None, description="ISO 8601 of updated_at")

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------
    is_deleted: bool = Field(default=False)
    account_id: int | None = Field(None, description="Kommo account ID")
    tags: list[dict[str, Any]] | None = Field(
        None, description="Tags attached to this contact"
    )

    @model_validator(mode="before")
    @classmethod
    def extract_embedded_fields(cls, data: Any) -> Any:
        """
        Extract linked lead IDs from _embedded.leads before validation.

        Kommo embeds linked leads under _embedded.leads when the
        request includes `with=leads`. We flatten these to a simple
        list of IDs on the ContactRecord.
        """
        if not isinstance(data, dict):
            return data

        embedded = data.get("_embedded", {})
        leads    = embedded.get("leads", [])
        if leads and not data.get("linked_leads_ids"):
            data["linked_leads_ids"] = [
                lead["id"] for lead in leads if isinstance(lead, dict) and "id" in lead
            ]
        return data

    @model_validator(mode="after")
    def extract_contact_details(self) -> "ContactRecord":
        """
        Extract phone numbers and email addresses from custom_fields_values.

        Kommo stores phone/email as custom fields with field_code = 'PHONE' / 'EMAIL'.
        We pull them out into flat lists for easy downstream use.
        """
        if not self.custom_fields_values:
            return self

        for cf in self.custom_fields_values:
            if not isinstance(cf, dict):
                continue
            code   = (cf.get("field_code") or "").upper()
            values = cf.get("values") or []

            for v in values:
                raw = v.get("value", "") if isinstance(v, dict) else str(v)
                if not raw:
                    continue
                if code == "PHONE":
                    self.phone_numbers.append(str(raw).strip())
                elif code == "EMAIL":
                    self.email_addresses.append(str(raw).strip())

        return self

    @model_validator(mode="after")
    def compute_iso_timestamps(self) -> "ContactRecord":
        """Convert Unix timestamps to ISO 8601 strings."""
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
# ContactExtractionResult
# =============================================================================

@dataclass
class ContactExtractionResult:
    """Summary returned by ContactsExtractor.extract_all()."""

    entity:           str   = "contacts"
    total_records:    int   = 0
    failed_records:   int   = 0
    pages_fetched:    int   = 0
    contacts_with_leads: int = 0   # Contacts that have at least one linked lead
    contacts_with_phone: int = 0   # Contacts with ≥1 phone number extracted
    contacts_with_email: int = 0   # Contacts with ≥1 email address extracted
    output_path:      Path | None = None
    dead_letter_path: Path | None = None
    duration_seconds: float = 0.0
    started_at:       str   = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )
    finished_at:      str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity":               self.entity,
            "total_records":        self.total_records,
            "failed_records":       self.failed_records,
            "pages_fetched":        self.pages_fetched,
            "contacts_with_leads":  self.contacts_with_leads,
            "contacts_with_phone":  self.contacts_with_phone,
            "contacts_with_email":  self.contacts_with_email,
            "output_path":          str(self.output_path),
            "duration_seconds":     round(self.duration_seconds, 2),
            "started_at":           self.started_at,
            "finished_at":          self.finished_at,
        }


# =============================================================================
# ContactsExtractor
# =============================================================================

class ContactsExtractor:
    """
    Extracts all contacts from Kommo with pagination, validation, and persistence.

    Automatically requests linked lead IDs using Kommo's `with=leads` parameter,
    enabling downstream message attribution without additional API calls.

    Args:
        client:       An open KommoAPIClient instance.
        output_dir:   Directory for contacts.json and error files.
        page_size:    Records per API page (max 250).
        include_leads: Request linked lead IDs from the API (default: True).
                       Set False to speed up extraction if leads are not needed.
        extra_params: Additional query parameters for every page request.

    Example:
        with KommoAPIClient(oauth) as client:
            extractor = ContactsExtractor(client, output_dir="outputs")
            result    = extractor.extract_all()
    """

    def __init__(
        self,
        client: KommoAPIClient,
        output_dir: str | Path = _DEFAULT_OUTPUT_DIR,
        page_size: int = _DEFAULT_PAGE_SIZE,
        include_leads: bool = True,
        extra_params: dict[str, Any] | None = None,
    ) -> None:
        self._client       = client
        self._output_dir   = Path(output_dir)
        self._error_dir    = self._output_dir / "errors"
        self._page_size    = min(page_size, 250)
        self._include_leads = include_leads
        self._extra_params  = extra_params or {}

        if include_leads and "with" not in self._extra_params:
            self._extra_params["with"] = _DEFAULT_WITH_PARAMS

    # =========================================================================
    # PUBLIC: Main Entry Point
    # =========================================================================

    @retry_api_call
    def extract_all(self) -> ContactExtractionResult:
        """
        Paginate all contacts, validate each record, and write to JSON.

        Never raises on individual validation failures — failed records go
        to a dead-letter file. Raises on unrecoverable API errors.

        Returns:
            ContactExtractionResult with counts, paths, and duration.

        Raises:
            KommoClientError: Unrecoverable API or network failure.
        """
        result  = ContactExtractionResult()
        started = time.monotonic()

        logger.info(
            "Contact extraction started",
            extra={"page_size": self._page_size, "include_leads": self._include_leads},
        )

        all_contacts:    list[dict[str, Any]] = []
        failed_contacts: list[dict[str, Any]] = []

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
                all_contacts.extend(valid)
                failed_contacts.extend(failed)
                result.pages_fetched = page_num

                logger.info(
                    "Contacts page processed",
                    extra={
                        "page":         page_num,
                        "valid":        len(valid),
                        "failed":       len(failed),
                        "total_so_far": len(all_contacts),
                    },
                )

        except KommoNotFoundError:
            logger.info("No contacts found in account (404 from API)")

        except KommoClientError as exc:
            logger.error(
                "Contact extraction failed with API error",
                extra={
                    "status_code":    exc.status_code,
                    "error":          str(exc),
                    "collected_so_far": len(all_contacts),
                },
            )
            raise

        # ------------------------------------------------------------------
        # Compute enrichment stats
        # ------------------------------------------------------------------
        result.total_records  = len(all_contacts)
        result.failed_records = len(failed_contacts)

        for c in all_contacts:
            if c.get("linked_leads_ids"):
                result.contacts_with_leads += 1
            if c.get("phone_numbers"):
                result.contacts_with_phone += 1
            if c.get("email_addresses"):
                result.contacts_with_email += 1

        # ------------------------------------------------------------------
        # Persist valid contacts
        # ------------------------------------------------------------------
        if all_contacts:
            result.output_path = self._write_json("contacts.json", all_contacts)

        # ------------------------------------------------------------------
        # Persist dead-letter records
        # ------------------------------------------------------------------
        if failed_contacts:
            result.dead_letter_path = self._write_dead_letter(failed_contacts)
            logger.warning(
                "Some contact records failed validation → dead-letter",
                extra={
                    "failed_count":     len(failed_contacts),
                    "dead_letter_path": str(result.dead_letter_path),
                },
            )

        result.duration_seconds = time.monotonic() - started
        result.finished_at      = datetime.now(tz=timezone.utc).isoformat()

        logger.info("Contact extraction complete", extra=result.as_dict())
        return result

    # =========================================================================
    # PUBLIC: Targeted Extraction Helpers
    # =========================================================================

    def extract_updated_since(self, unix_timestamp: int) -> ContactExtractionResult:
        """
        Extract only contacts updated after the given Unix timestamp.

        Args:
            unix_timestamp: Only fetch contacts updated at or after this time.

        Returns:
            ContactExtractionResult for the incremental batch.
        """
        original_params = dict(self._extra_params)
        self._extra_params["filter[updated_at][from]"] = unix_timestamp
        try:
            return self.extract_all()
        finally:
            self._extra_params = original_params

    def fetch_single(self, contact_id: int) -> ContactRecord | None:
        """
        Fetch and validate a single contact by ID.

        Args:
            contact_id: Kommo contact ID.

        Returns:
            Validated ContactRecord, or None if not found.
        """
        logger.info("Fetching single contact", extra={"contact_id": contact_id})
        try:
            response = self._client.get(f"/contacts/{contact_id}", params={"with": "leads"})
            return ContactRecord.model_validate(response.json())
        except KommoNotFoundError:
            logger.warning("Contact not found", extra={"contact_id": contact_id})
            return None
        except Exception as exc:
            logger.error("Failed to fetch contact", extra={"contact_id": contact_id, "error": str(exc)})
            raise

    def build_lead_contact_map(self) -> dict[int, list[dict[str, Any]]]:
        """
        Build a mapping of lead_id → list of contact summaries.

        Useful for the normalizer layer — allows instant lookup of
        which contacts are associated with a given lead.

        Returns:
            Dict: { lead_id: [{"contact_id": int, "name": str, "phone": str, "email": str}, ...] }

        Example:
            lead_map = extractor.build_lead_contact_map()
            contacts_for_lead = lead_map.get(10482301, [])
        """
        logger.info("Building lead → contact map (full extraction)")
        result = self.extract_all()

        if not result.output_path or not result.output_path.exists():
            logger.warning("No contacts extracted — lead_contact_map will be empty")
            return {}

        raw = json.loads(result.output_path.read_text(encoding="utf-8"))
        records: list[dict] = raw.get("data", [])

        lead_map: dict[int, list[dict[str, Any]]] = {}
        for contact in records:
            summary = {
                "contact_id": contact.get("id"),
                "name":       contact.get("name"),
                "phone":      contact.get("phone_numbers", [None])[0],
                "email":      contact.get("email_addresses", [None])[0],
            }
            for lead_id in contact.get("linked_leads_ids", []):
                lead_map.setdefault(lead_id, []).append(summary)

        logger.info(
            "Lead-contact map built",
            extra={"unique_leads": len(lead_map), "total_contacts": len(records)},
        )
        return lead_map

    # =========================================================================
    # PRIVATE: Validation
    # =========================================================================

    def _validate_page(
        self,
        raw_records: list[dict[str, Any]],
        page_num: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Validate a page of raw contact dicts against ContactRecord."""
        from pydantic import ValidationError

        valid:  list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []

        for raw in raw_records:
            try:
                record = ContactRecord.model_validate(raw)
                valid.append(record.model_dump(mode="json"))
            except ValidationError as exc:
                contact_id = raw.get("id", "unknown")
                logger.warning(
                    "Contact validation failed — routing to dead-letter",
                    extra={
                        "contact_id":        contact_id,
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
        """Atomically write contact records to JSON with metadata envelope."""
        self._output_dir.mkdir(parents=True, exist_ok=True)
        output_path = self._output_dir / filename

        envelope: dict[str, Any] = {
            "_meta": {
                "entity":       "contacts",
                "count":        len(records),
                "extracted_at": datetime.now(tz=timezone.utc).isoformat(),
                "source":       "kommo_api_v4",
                "includes":     "linked_lead_ids, phone_numbers, email_addresses",
            },
            "data": records,
        }

        self._atomic_write(output_path, envelope)
        logger.info(
            "Contacts written to disk",
            extra={"path": str(output_path), "count": len(records)},
        )
        return output_path

    def _write_dead_letter(self, failed_records: list[dict[str, Any]]) -> Path:
        """Write validation-failed records to a timestamped dead-letter file."""
        self._error_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
        dl_path   = self._error_dir / f"contacts_failed_{timestamp}.json"

        envelope: dict[str, Any] = {
            "_meta": {
                "entity":     "contacts",
                "type":       "dead_letter",
                "count":      len(failed_records),
                "created_at": datetime.now(tz=timezone.utc).isoformat(),
                "note":       "These records failed Pydantic validation. Fix root cause and replay.",
            },
            "data": failed_records,
        }
        self._atomic_write(dl_path, envelope)
        return dl_path

    @staticmethod
    def _atomic_write(path: Path, data: dict[str, Any]) -> None:
        """Write JSON via tmp → rename for crash safety."""
        tmp_path = path.with_suffix(".tmp")
        try:
            tmp_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            tmp_path.replace(path)
        except OSError:
            tmp_path.unlink(missing_ok=True)
            raise
