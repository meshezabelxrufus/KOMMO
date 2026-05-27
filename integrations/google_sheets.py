"""
integrations/google_sheets.py
==============================
Production-grade Google Sheets integration layer for Milestone 2.

ARCHITECTURE
────────────
  GoogleSheetsClient   — Auth + low-level gspread wrapper (retry, rate-limit)
  SheetsWriter         — High-level batch write operations per worksheet
  SheetsSyncResult     — Typed result DTO returned after each sync operation
  WorksheetConfig      — Pydantic model defining per-worksheet column schema

AUTHENTICATION
──────────────
  Uses Google Service Account credentials.
  Credentials file path:  GOOGLE_SERVICE_ACCOUNT_FILE  (env var)
  Spreadsheet target ID:  GOOGLE_SHEETS_SPREADSHEET_ID (env var)

  Required Google API scopes:
    - https://www.googleapis.com/auth/spreadsheets
    - https://www.googleapis.com/auth/drive (for worksheet creation only)

WRITE STRATEGY
──────────────
  ALL writes are batch operations:
    1. Clear the target worksheet range (preserves header row)
    2. Convert records to a 2-D list of scalar values
    3. Write the entire dataset in one API call (update / values.batchUpdate)

  Row-by-row appends are NEVER used. This approach:
    - Prevents duplicate rows on retry
    - Is significantly faster for large datasets (1 API call vs N)
    - Guarantees referential integrity (sheet always reflects JSON state)

WORKSHEETS
──────────
  Leads         — Core lead records from outputs/leads.json
  Messages      — Flattened chat messages from outputs/messages_flat.json
  Daily_Summary — Aggregated statistics per entity per day

RETRY HANDLING
──────────────
  The Google Sheets API enforces a 60-requests-per-minute quota.
  We use tenacity with exponential backoff + quota-aware sleep.
  gspread raises APIError (429 / 503) which we treat as retryable.

USAGE
─────
    from integrations.google_sheets import GoogleSheetsClient, SheetsWriter

    client = GoogleSheetsClient.from_env()
    writer = SheetsWriter(client)

    with open("outputs/leads.json") as f:
        leads_data = json.load(f)

    result = writer.write_leads(leads_data["data"])
    print(result.rows_written, result.worksheet_name)

    result = writer.write_daily_summary(
        leads_count=len(leads_data["data"]),
        messages_count=0,
        meta=leads_data["_meta"],
    )
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import gspread
import gspread.exceptions
from google.oauth2.service_account import Credentials
from pydantic import BaseModel, Field, field_validator, model_validator
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from utils.logger import get_logger

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

log: logging.Logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Google OAuth scopes required for Sheets read/write + Drive (worksheet creation)
_SCOPES: list[str] = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Worksheet names
SHEET_LEADS         = "Leads"
SHEET_MESSAGES      = "Messages"
SHEET_DAILY_SUMMARY = "Daily_Summary"

# Google Sheets API column/row limits
_MAX_COLS = 18_278   # ZZZ
_MAX_ROWS = 10_000_000

# Batch write retry config
_MAX_RETRY_ATTEMPTS = 5
_BACKOFF_INITIAL    = 2.0   # seconds
_BACKOFF_MAX        = 120.0  # seconds (quota resets in ~60s)
_BACKOFF_MULTIPLIER = 2.0

# Sheets API quota: ~60 reads + 60 writes per minute per project
# We add a small mandatory inter-call sleep to stay safe
_QUOTA_SLEEP_SECONDS = 1.1


# =============================================================================
# Custom Exceptions
# =============================================================================

class GoogleSheetsError(Exception):
    """Base exception for all Google Sheets integration errors."""

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.context = context

    def __str__(self) -> str:
        base = self.args[0]
        if self.context:
            ctx = " | ".join(f"{k}={v}" for k, v in self.context.items())
            return f"{base} [{ctx}]"
        return base


class GoogleSheetsAuthError(GoogleSheetsError):
    """Service account credentials are missing, invalid, or lack required scopes."""


class GoogleSheetsConfigError(GoogleSheetsError):
    """Required environment variables are not set or contain invalid values."""


class GoogleSheetsWriteError(GoogleSheetsError):
    """A batch write operation failed after all retry attempts were exhausted."""

    def __init__(self, message: str, worksheet: str | None = None, **ctx: Any) -> None:
        super().__init__(message, **ctx)
        self.worksheet = worksheet


class GoogleSheetsQuotaError(GoogleSheetsError):
    """Google Sheets API quota was exhausted (HTTP 429)."""


# =============================================================================
# Pydantic Models
# =============================================================================

class WorksheetConfig(BaseModel):
    """
    Schema definition for a single Google Sheets worksheet.

    Defines the ordered column headers and how each field maps
    from the source JSON record to a sheet cell value.

    Attributes:
        name:    Worksheet tab name (e.g. "Leads").
        headers: Ordered list of column header strings.
        fields:  Ordered list of keys to extract from each record dict.
                 Must be the same length as `headers`.
                 Use dotted paths (e.g. "result.text") for nested access.

    Example:
        WorksheetConfig(
            name="Leads",
            headers=["Lead ID", "Name", "Status"],
            fields=["id", "name", "status_id"],
        )
    """

    name:    str          = Field(..., min_length=1, description="Worksheet tab name")
    headers: list[str]    = Field(..., min_length=1, description="Column header labels")
    fields:  list[str]    = Field(..., min_length=1, description="Source record field names")

    @model_validator(mode="after")
    def headers_fields_same_length(self) -> "WorksheetConfig":
        if len(self.headers) != len(self.fields):
            raise ValueError(
                f"WorksheetConfig '{self.name}': "
                f"headers ({len(self.headers)}) and fields ({len(self.fields)}) "
                f"must have the same length."
            )
        return self

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, v: Any) -> str:
        if not isinstance(v, str):
            raise ValueError("Worksheet name must be a string")
        return v.strip()


class SheetsSyncResult(BaseModel):
    """
    Typed result returned by every SheetsWriter write operation.

    Attributes:
        worksheet_name:  Name of the worksheet that was written.
        rows_written:    Number of data rows written (excludes header row).
        columns_written: Number of columns in the written schema.
        duration_s:      Elapsed wall-clock time for the full operation.
        spreadsheet_url: Direct URL to the spreadsheet (convenience link).
        success:         True if the write completed without errors.
        error:           Error message if success=False.
        written_at:      ISO 8601 timestamp of when the write completed.
    """

    worksheet_name:  str
    rows_written:    int   = 0
    columns_written: int   = 0
    duration_s:      float = 0.0
    spreadsheet_url: str   = ""
    success:         bool  = True
    error:           str | None = None
    written_at:      str   = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )

    @property
    def status_icon(self) -> str:
        return "✅" if self.success else "❌"

    def __str__(self) -> str:
        if self.success:
            return (
                f"{self.status_icon}  {self.worksheet_name}: "
                f"{self.rows_written:,} rows × {self.columns_written} cols "
                f"[{self.duration_s:.1f}s]"
            )
        return f"{self.status_icon}  {self.worksheet_name}: FAILED — {self.error}"


# =============================================================================
# Retry logic (Sheets-API-aware)
# =============================================================================

def _is_quota_error(exc: BaseException) -> bool:
    """Return True for HTTP 429 / 503 from the Google Sheets API."""
    if isinstance(exc, gspread.exceptions.APIError):
        code = getattr(exc, "response", None)
        if code is not None:
            status = getattr(code, "status_code", 0)
            return status in (429, 500, 503)
    return False


def _log_retry(retry_state: RetryCallState) -> None:
    exc     = retry_state.outcome.exception() if retry_state.outcome else None
    nxt     = retry_state.next_action.sleep if retry_state.next_action else 0.0
    fn_name = getattr(retry_state.fn, "__name__", "unknown")
    log.warning(
        "Sheets API retry %d/%d — %s — sleeping %.1fs — %s",
        retry_state.attempt_number,
        _MAX_RETRY_ATTEMPTS,
        fn_name,
        nxt,
        type(exc).__name__ if exc else "no exception",
        extra={
            "retry_attempt":  retry_state.attempt_number,
            "retry_sleep_s":  round(nxt, 2),
            "exc_type":       type(exc).__name__ if exc else None,
            "integration":    "google_sheets",
        },
    )


def _log_exhausted(retry_state: RetryCallState) -> None:
    exc     = retry_state.outcome.exception() if retry_state.outcome else None
    fn_name = getattr(retry_state.fn, "__name__", "unknown")
    log.error(
        "All %d Sheets API retries exhausted for %s — %s",
        retry_state.attempt_number,
        fn_name,
        repr(exc) if exc else "unknown error",
        extra={"retries_exhausted": retry_state.attempt_number, "integration": "google_sheets"},
    )


def _sheets_backoff_wait(retry_state: RetryCallState) -> float:
    """
    Wait strategy for Google Sheets quota errors.

    If the error is a 429 (quota exceeded), wait at least 65 seconds
    (Sheets quota resets every 60s) plus random jitter.
    Otherwise use standard exponential backoff.
    """
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    attempt = retry_state.attempt_number

    if exc and _is_quota_error(exc):
        # Quota window is 60 seconds; add jitter to avoid thundering herd
        wait = 65.0 + random.uniform(0, 10.0)
        log.warning(
            "Sheets API quota hit — waiting %.0fs before retry %d",
            wait, attempt + 1,
            extra={"quota_wait_s": wait, "integration": "google_sheets"},
        )
        return wait

    backoff = min(_BACKOFF_MAX, _BACKOFF_INITIAL * (_BACKOFF_MULTIPLIER ** attempt))
    return backoff + random.uniform(0, 2.0)


def _retry_sheets(func):
    """
    Retry decorator for Google Sheets API calls.

    Retries on:
      - gspread.exceptions.APIError  (includes 429, 500, 503)
      - ConnectionError / TimeoutError (transient network issues)

    Backs off with quota-aware sleep strategy.
    """
    return retry(
        retry=retry_if_exception_type((
            gspread.exceptions.APIError,
            gspread.exceptions.GSpreadException,
            ConnectionError,
            TimeoutError,
            OSError,
        )),
        stop=stop_after_attempt(_MAX_RETRY_ATTEMPTS),
        wait=_sheets_backoff_wait,
        before_sleep=_log_retry,
        retry_error_callback=_log_exhausted,
        reraise=True,
    )(func)


# =============================================================================
# GoogleSheetsClient — Auth + low-level gspread wrapper
# =============================================================================

class GoogleSheetsClient:
    """
    Authenticated gspread client bound to a specific Google Spreadsheet.

    Handles:
      - Service account authentication via credentials JSON file
      - Worksheet lookup and auto-creation
      - Quota-safe API interactions (rate limiting + retry)

    Args:
        credentials_path:  Absolute path to the service account JSON key file.
        spreadsheet_id:    Google Sheets spreadsheet ID (from the URL).

    Raises:
        GoogleSheetsAuthError:   Credentials file missing or invalid.
        GoogleSheetsConfigError: spreadsheet_id or credentials_path empty.

    Example:
        # Preferred: load from environment variables
        client = GoogleSheetsClient.from_env()

        # Or pass directly (useful in tests)
        client = GoogleSheetsClient(
            credentials_path=Path("/path/to/credentials.json"),
            spreadsheet_id="1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
        )
    """

    def __init__(
        self,
        credentials_path: str | Path,
        spreadsheet_id: str,
    ) -> None:
        credentials_path = Path(credentials_path)

        # ── Validate inputs ────────────────────────────────────────────
        if not spreadsheet_id or not spreadsheet_id.strip():
            raise GoogleSheetsConfigError(
                "GOOGLE_SHEETS_SPREADSHEET_ID is empty. "
                "Set it to the ID from your spreadsheet URL: "
                "https://docs.google.com/spreadsheets/d/<ID>/edit"
            )

        if not credentials_path.exists():
            raise GoogleSheetsAuthError(
                f"Service account credentials file not found: {credentials_path}. "
                "Set GOOGLE_SERVICE_ACCOUNT_FILE to the correct path.",
                path=str(credentials_path),
            )

        self._spreadsheet_id   = spreadsheet_id.strip()
        self._credentials_path = credentials_path

        # ── Build credentials and gspread client ───────────────────────
        try:
            creds  = Credentials.from_service_account_file(
                str(credentials_path), scopes=_SCOPES
            )
            self._gc = gspread.authorize(creds)
            log.info(
                "Google Sheets client authenticated",
                extra={
                    "spreadsheet_id":   self._spreadsheet_id,
                    "credentials_file": str(credentials_path),
                    "integration":      "google_sheets",
                },
            )
        except (ValueError, KeyError, FileNotFoundError) as exc:
            raise GoogleSheetsAuthError(
                f"Failed to load service account credentials: {exc}",
                credentials_path=str(credentials_path),
            ) from exc

        # ── Open the target spreadsheet (validates access) ─────────────
        self._spreadsheet = self._open_spreadsheet()

    # ------------------------------------------------------------------
    # Class method: construct from environment variables
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> "GoogleSheetsClient":
        """
        Construct a GoogleSheetsClient from environment variables.

        Required environment variables:
            GOOGLE_SERVICE_ACCOUNT_FILE     Path to service account JSON key
            GOOGLE_SHEETS_SPREADSHEET_ID    Spreadsheet ID from the URL

        Raises:
            GoogleSheetsConfigError: If either required variable is not set.

        Example:
            client = GoogleSheetsClient.from_env()
        """
        creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
        sheet_id   = os.environ.get("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip()

        missing = []
        if not creds_path:
            missing.append("GOOGLE_SERVICE_ACCOUNT_FILE")
        if not sheet_id:
            missing.append("GOOGLE_SHEETS_SPREADSHEET_ID")

        if missing:
            raise GoogleSheetsConfigError(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                "Add them to your .env file. See .env.example for the full template."
            )

        return cls(
            credentials_path=creds_path,
            spreadsheet_id=sheet_id,
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def spreadsheet_id(self) -> str:
        """The Google Sheets spreadsheet ID this client is bound to."""
        return self._spreadsheet_id

    @property
    def spreadsheet_url(self) -> str:
        """Direct URL to open the spreadsheet in a browser."""
        return f"https://docs.google.com/spreadsheets/d/{self._spreadsheet_id}/edit"

    @property
    def spreadsheet_title(self) -> str:
        """Human-readable title of the spreadsheet."""
        return self._spreadsheet.title

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_or_create_worksheet(
        self,
        name: str,
        rows: int = 10_000,
        cols: int = 50,
    ) -> gspread.Worksheet:
        """
        Return the named worksheet, creating it if it doesn't exist.

        Args:
            name: Tab name (e.g. "Leads").
            rows: Initial row count when creating a new worksheet.
            cols: Initial column count when creating a new worksheet.

        Returns:
            gspread.Worksheet instance.

        Raises:
            GoogleSheetsWriteError: If the worksheet cannot be created.
        """
        try:
            ws = self._spreadsheet.worksheet(name)
            log.debug(
                "Worksheet found: '%s'",
                name,
                extra={"worksheet": name, "integration": "google_sheets"},
            )
            return ws
        except gspread.exceptions.WorksheetNotFound:
            log.info(
                "Worksheet '%s' not found — creating it",
                name,
                extra={"worksheet": name, "integration": "google_sheets"},
            )
            return self._create_worksheet(name, rows=rows, cols=cols)

    def batch_write(
        self,
        worksheet: gspread.Worksheet,
        data: list[list[Any]],
        *,
        start_cell: str = "A1",
    ) -> None:
        """
        Write a 2-D array to the worksheet in a single API call.

        Clears the target range first (rows 2+) to prevent stale data,
        then writes `data` starting at `start_cell`.

        The first row in `data` MUST be the header row.
        Row 1 (the header) is NOT cleared — it is overwritten safely.

        Args:
            worksheet:  Target gspread.Worksheet.
            data:       2-D list; data[0] = headers, data[1:] = records.
            start_cell: Top-left cell of the write region (default "A1").

        Raises:
            GoogleSheetsWriteError: If the write fails after all retries.
            ValueError:             If data is empty.
        """
        if not data:
            raise ValueError("batch_write: data must be a non-empty 2-D list")

        num_rows = len(data)
        num_cols = max(len(row) for row in data)
        ws_name  = worksheet.title

        log.info(
            "Sheets batch write starting — worksheet='%s' rows=%d cols=%d",
            ws_name, num_rows, num_cols,
            extra={
                "worksheet":  ws_name,
                "rows":       num_rows,
                "cols":       num_cols,
                "start_cell": start_cell,
                "integration": "google_sheets",
            },
        )

        self._clear_data_rows(worksheet)
        self._write_with_retry(worksheet, data, start_cell)

        log.info(
            "Sheets batch write complete — worksheet='%s' rows=%d cols=%d",
            ws_name, num_rows, num_cols,
            extra={
                "worksheet":   ws_name,
                "rows_written": num_rows,
                "integration": "google_sheets",
            },
        )

    def format_header_row(self, worksheet: gspread.Worksheet) -> None:
        """
        Apply bold + background formatting to row 1 (the header row).

        Uses the Sheets batchUpdate API to apply:
          - Bold font weight
          - Light blue background (#D0E4F7)
          - Freeze row 1

        Note: Formatting is a best-effort operation — failure is logged
        as a WARNING but does NOT raise an exception (data is already written).

        Args:
            worksheet: The worksheet whose header row to format.
        """
        try:
            _time_quota_sleep()
            spreadsheet = worksheet.spreadsheet
            ws_id       = worksheet.id
            num_cols    = worksheet.col_count

            requests: list[dict[str, Any]] = [
                # Bold + background colour on row 1
                {
                    "repeatCell": {
                        "range": {
                            "sheetId":          ws_id,
                            "startRowIndex":    0,
                            "endRowIndex":      1,
                            "startColumnIndex": 0,
                            "endColumnIndex":   num_cols,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "textFormat": {"bold": True},
                                "backgroundColor": {
                                    "red":   0.816,
                                    "green": 0.894,
                                    "blue":  0.969,
                                },
                            }
                        },
                        "fields": "userEnteredFormat(textFormat,backgroundColor)",
                    }
                },
                # Freeze row 1
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId":    ws_id,
                            "gridProperties": {"frozenRowCount": 1},
                        },
                        "fields": "gridProperties.frozenRowCount",
                    }
                },
            ]

            spreadsheet.batch_update({"requests": requests})
            log.debug(
                "Header formatted — worksheet='%s'",
                worksheet.title,
                extra={"worksheet": worksheet.title, "integration": "google_sheets"},
            )

        except Exception as exc:
            log.warning(
                "Header formatting failed (non-fatal) — worksheet='%s': %s",
                worksheet.title, exc,
                extra={"worksheet": worksheet.title, "integration": "google_sheets"},
            )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @_retry_sheets
    def _open_spreadsheet(self) -> gspread.Spreadsheet:
        """Open the target spreadsheet, with retry on transient errors."""
        _time_quota_sleep()
        return self._gc.open_by_key(self._spreadsheet_id)

    @_retry_sheets
    def _create_worksheet(
        self, name: str, rows: int, cols: int
    ) -> gspread.Worksheet:
        """Create a new worksheet tab, with retry."""
        _time_quota_sleep()
        try:
            ws = self._spreadsheet.add_worksheet(title=name, rows=rows, cols=cols)
            log.info(
                "Worksheet created: '%s' (%d rows × %d cols)",
                name, rows, cols,
                extra={"worksheet": name, "integration": "google_sheets"},
            )
            return ws
        except gspread.exceptions.APIError as exc:
            raise GoogleSheetsWriteError(
                f"Failed to create worksheet '{name}': {exc}",
                worksheet=name,
            ) from exc

    @_retry_sheets
    def _clear_data_rows(self, worksheet: gspread.Worksheet) -> None:
        """
        Clear all rows from row 2 downward, preserving the header row.

        Uses range notation "A2:ZZZ" to clear only data rows.
        This means re-running will not accumulate duplicate headers.
        """
        _time_quota_sleep()
        try:
            worksheet.batch_clear(["A2:ZZZ"])
            log.debug(
                "Cleared data rows — worksheet='%s'",
                worksheet.title,
                extra={"worksheet": worksheet.title, "integration": "google_sheets"},
            )
        except gspread.exceptions.APIError as exc:
            log.warning(
                "Clear failed (will overwrite anyway) — worksheet='%s': %s",
                worksheet.title, exc,
                extra={"worksheet": worksheet.title, "integration": "google_sheets"},
            )
            # Non-fatal: overwrite will still succeed

    @_retry_sheets
    def _write_with_retry(
        self,
        worksheet: gspread.Worksheet,
        data: list[list[Any]],
        start_cell: str,
    ) -> None:
        """Execute the actual batch write with retry decoration."""
        _time_quota_sleep()
        worksheet.update(data, start_cell, value_input_option="USER_ENTERED")


# =============================================================================
# Field extraction helpers
# =============================================================================

def _extract_field(record: dict[str, Any], field_path: str) -> Any:
    """
    Extract a value from a nested dict using dot-notation path.

    Examples:
        _extract_field({"a": {"b": 42}}, "a.b")   → 42
        _extract_field({"result": {"text": "ok"}}, "result.text") → "ok"
        _extract_field({"x": None}, "x.y")          → None
        _extract_field(record, "nonexistent")        → None

    Args:
        record:     Source dict (typically a deserialized JSON object).
        field_path: Dot-delimited key path (e.g. "result.text").

    Returns:
        The extracted value, or None if any key in the path is missing.
    """
    parts  = field_path.split(".")
    cursor: Any = record
    for part in parts:
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(part)
        if cursor is None:
            return None
    return cursor


def _coerce_cell(value: Any) -> str | int | float | bool:
    """
    Coerce a Python value to a Sheets-compatible scalar.

    Google Sheets cannot store arbitrary Python objects — every cell
    must be a scalar (str, int, float, bool, or empty string).

    Rules:
      - None / missing         → ""  (empty cell)
      - dict / list            → JSON-encoded string
      - datetime               → ISO 8601 string
      - bool                   → preserved as bool (Sheets checkbox-friendly)
      - int / float            → preserved as-is
      - str                    → stripped
      - Everything else        → str(value)

    Args:
        value: Raw Python value from a JSON record.

    Returns:
        Sheets-compatible scalar.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _records_to_rows(
    records: Sequence[dict[str, Any]],
    config: WorksheetConfig,
) -> list[list[Any]]:
    """
    Convert a list of record dicts to a 2-D list for Sheets batch write.

    The first row in the output is always the header row (from config.headers).
    Subsequent rows are extracted field values, coerced to Sheets scalars.

    Args:
        records: List of source dicts (one per row).
        config:  WorksheetConfig defining headers and field paths.

    Returns:
        2-D list: [[header1, header2, ...], [val1, val2, ...], ...]
        Returns [[header1, header2, ...]] if records is empty
        (preserves headers even with no data).
    """
    rows: list[list[Any]] = [list(config.headers)]

    for record in records:
        row = [
            _coerce_cell(_extract_field(record, field))
            for field in config.fields
        ]
        rows.append(row)

    return rows


# =============================================================================
# Worksheet schema definitions
# =============================================================================

#: Column schema for the "Leads" worksheet.
LEADS_WORKSHEET_CONFIG = WorksheetConfig(
    name=SHEET_LEADS,
    headers=[
        "Lead ID",
        "Lead Name",
        "Pipeline ID",
        "Status ID",
        "Responsible User ID",
        "Group ID",
        "Price",
        "Loss Reason ID",
        "Is Deleted",
        "Score",
        "Account ID",
        "Created At (UTC)",
        "Updated At (UTC)",
        "Closed At (UTC)",
        "Tags",
        "Custom Fields (JSON)",
    ],
    fields=[
        "id",
        "name",
        "pipeline_id",
        "status_id",
        "responsible_user_id",
        "group_id",
        "price",
        "loss_reason_id",
        "is_deleted",
        "score",
        "account_id",
        "created_at_iso",
        "updated_at_iso",
        "closed_at_iso",
        "tags",
        "custom_fields_values",
    ],
)

#: Column schema for the "Messages" worksheet.
#: Maps to the flattened schema from outputs/messages_flat.json.
MESSAGES_WORKSHEET_CONFIG = WorksheetConfig(
    name=SHEET_MESSAGES,
    headers=[
        "Message ID",
        "Chat ID",
        "Lead ID",
        "Direction",
        "Type",
        "Author ID",
        "Author Type",
        "Text",
        "Timestamp (UTC)",
        "Created At (Unix)",
        "Media URL",
        "Origin",
        "Chat Created At",
        "Contact ID",
        "Responsible User ID",
    ],
    fields=[
        "id",
        "chat_id",
        "lead_id",
        "direction",
        "type",
        "author.id",
        "author.type",
        "text",
        "timestamp_iso",
        "created_at",
        "media_url",
        "origin",
        "chat_created_at_iso",
        "contact_id",
        "responsible_user_id",
    ],
)

#: Column schema for the "Daily_Summary" worksheet.
DAILY_SUMMARY_WORKSHEET_CONFIG = WorksheetConfig(
    name=SHEET_DAILY_SUMMARY,
    headers=[
        "Run Date (UTC)",
        "Run Timestamp (ISO)",
        "Entity",
        "Records Extracted",
        "Source File",
        "Extracted At (UTC)",
        "Pipeline Version",
        "Notes",
    ],
    fields=[
        "_summary_date",
        "_summary_run_ts",
        "_summary_entity",
        "_summary_count",
        "_summary_source",
        "_summary_extracted_at",
        "_summary_version",
        "_summary_notes",
    ],
)


# =============================================================================
# SheetsWriter — High-level batch write operations
# =============================================================================

class SheetsWriter:
    """
    High-level writer that maps Kommo CRM data to Google Sheets worksheets.

    Uses GoogleSheetsClient for authentication and transport.
    All writes are batch operations — no row-by-row appends.

    Args:
        client: Authenticated GoogleSheetsClient instance.

    Example:
        client = GoogleSheetsClient.from_env()
        writer = SheetsWriter(client)

        # Write leads
        with open("outputs/leads.json") as f:
            payload = json.load(f)

        result = writer.write_leads(payload["data"])
        print(result)   # ✅  Leads: 24,796 rows × 16 cols [12.3s]
    """

    # Pipeline version tag embedded in Daily_Summary rows
    PIPELINE_VERSION = "milestone-2.0"

    def __init__(self, client: GoogleSheetsClient) -> None:
        self._client = client
        log.info(
            "SheetsWriter initialised — spreadsheet_id=%s",
            client.spreadsheet_id,
            extra={
                "spreadsheet_id":  client.spreadsheet_id,
                "spreadsheet_url": client.spreadsheet_url,
                "integration":     "google_sheets",
            },
        )

    # ------------------------------------------------------------------
    # Public write methods
    # ------------------------------------------------------------------

    def write_leads(
        self,
        records: list[dict[str, Any]],
    ) -> SheetsSyncResult:
        """
        Batch-write Kommo lead records to the 'Leads' worksheet.

        Converts the data array from outputs/leads.json into a flat
        2-D table and writes it in a single API call.

        Args:
            records: List of lead dicts from leads.json["data"].
                     May be empty — will write headers only.

        Returns:
            SheetsSyncResult with rows_written, duration, and status.

        Raises:
            GoogleSheetsWriteError: If the write fails after all retries.

        Example:
            with open("outputs/leads.json") as f:
                data = json.load(f)
            result = writer.write_leads(data["data"])
        """
        return self._write_worksheet(
            config=LEADS_WORKSHEET_CONFIG,
            records=records,
            entity_label="leads",
        )

    def write_messages(
        self,
        records: list[dict[str, Any]],
    ) -> SheetsSyncResult:
        """
        Batch-write flattened chat message records to the 'Messages' worksheet.

        Maps to the AI-ready flattened schema from outputs/messages_flat.json.

        Args:
            records: List of message dicts from messages_flat.json["messages"]
                     (or the flat list — the schema is the same).

        Returns:
            SheetsSyncResult with rows_written, duration, and status.

        Example:
            with open("outputs/messages_flat.json") as f:
                data = json.load(f)
            # Try both common top-level keys
            msgs = data.get("messages") or data.get("data") or []
            result = writer.write_messages(msgs)
        """
        return self._write_worksheet(
            config=MESSAGES_WORKSHEET_CONFIG,
            records=records,
            entity_label="messages",
        )

    def write_daily_summary(
        self,
        leads_count: int,
        messages_count: int,
        meta: dict[str, Any] | None = None,
        tasks_count: int | None = None,
        pipelines_count: int | None = None,
        notes: str = "",
    ) -> SheetsSyncResult:
        """
        Append an aggregated daily summary row to the 'Daily_Summary' worksheet.

        Unlike write_leads / write_messages, this method APPENDS a new row
        instead of overwriting the sheet — so you accumulate a history of
        every extraction run over time.

        Each call adds one row per entity (leads, messages, tasks, pipelines)
        plus a totals row.

        Args:
            leads_count:     Number of lead records extracted in this run.
            messages_count:  Number of message records extracted.
            meta:            Optional _meta dict from any extraction output
                             (used to read extracted_at timestamp).
            tasks_count:     Optional number of task records.
            pipelines_count: Optional number of pipeline records.
            notes:           Optional free-text note for this run.

        Returns:
            SheetsSyncResult summarising the rows written.

        Example:
            result = writer.write_daily_summary(
                leads_count=24796,
                messages_count=18500,
                meta=leads_meta,
            )
        """
        started = time.monotonic()
        ws_name = SHEET_DAILY_SUMMARY

        log.info(
            "Writing daily summary — leads=%d messages=%d",
            leads_count, messages_count,
            extra={
                "leads_count":    leads_count,
                "messages_count": messages_count,
                "worksheet":      ws_name,
                "integration":    "google_sheets",
            },
        )

        try:
            ws = self._client.get_or_create_worksheet(ws_name)

            # Build summary rows — one per entity
            run_ts     = datetime.now(tz=timezone.utc)
            run_date   = run_ts.strftime("%Y-%m-%d")
            run_iso    = run_ts.isoformat()
            extracted_at = (meta or {}).get("extracted_at", run_iso)

            entity_rows: list[tuple[str, int, str]] = [
                ("leads",    leads_count,    "outputs/leads.json"),
                ("messages", messages_count, "outputs/messages_flat.json"),
            ]
            if tasks_count is not None:
                entity_rows.append(("tasks", tasks_count, "outputs/tasks.json"))
            if pipelines_count is not None:
                entity_rows.append(("pipelines", pipelines_count, "outputs/pipelines.json"))

            # Build data rows
            new_rows: list[list[Any]] = []
            for entity, count, source in entity_rows:
                new_rows.append([
                    run_date,
                    run_iso,
                    entity,
                    count,
                    source,
                    extracted_at,
                    self.PIPELINE_VERSION,
                    notes,
                ])

            # Ensure header exists on first run
            self._ensure_daily_summary_header(ws)

            # Append rows (summary uses append, not overwrite)
            self._append_rows_with_retry(ws, new_rows)

            duration_s = time.monotonic() - started
            result = SheetsSyncResult(
                worksheet_name=ws_name,
                rows_written=len(new_rows),
                columns_written=len(DAILY_SUMMARY_WORKSHEET_CONFIG.headers),
                duration_s=duration_s,
                spreadsheet_url=self._client.spreadsheet_url,
                success=True,
            )
            log.info(
                "Daily summary written — %s",
                result,
                extra={
                    "worksheet":    ws_name,
                    "rows_written": len(new_rows),
                    "duration_s":   round(duration_s, 2),
                    "integration":  "google_sheets",
                },
            )
            return result

        except (GoogleSheetsError, gspread.exceptions.GSpreadException) as exc:
            duration_s = time.monotonic() - started
            log.error(
                "Daily summary write failed — %s: %s",
                ws_name, exc,
                extra={
                    "worksheet":   ws_name,
                    "error":       str(exc),
                    "duration_s":  round(duration_s, 2),
                    "integration": "google_sheets",
                },
            )
            return SheetsSyncResult(
                worksheet_name=ws_name,
                duration_s=duration_s,
                success=False,
                error=str(exc),
                spreadsheet_url=self._client.spreadsheet_url,
            )

    # ------------------------------------------------------------------
    # Generic worksheet writer
    # ------------------------------------------------------------------

    def _write_worksheet(
        self,
        config: WorksheetConfig,
        records: list[dict[str, Any]],
        entity_label: str,
    ) -> SheetsSyncResult:
        """
        Internal method: convert records → 2-D rows → batch-write to sheet.

        Args:
            config:       Worksheet schema (headers + field paths).
            records:      Source records to write.
            entity_label: Human-readable label for logging (e.g. "leads").

        Returns:
            SheetsSyncResult.
        """
        started  = time.monotonic()
        ws_name  = config.name

        log.info(
            "Sheets write starting — worksheet='%s' entity=%s records=%d",
            ws_name, entity_label, len(records),
            extra={
                "worksheet":   ws_name,
                "entity":      entity_label,
                "record_count": len(records),
                "integration": "google_sheets",
            },
        )

        try:
            # 1. Ensure worksheet exists (auto-create if needed)
            ws = self._client.get_or_create_worksheet(ws_name)

            # 2. Convert records to 2-D row list
            rows = _records_to_rows(records, config)

            # 3. Batch-write (clear data rows + write all in one call)
            self._client.batch_write(ws, rows)

            # 4. Format header row (best-effort — non-fatal if it fails)
            self._client.format_header_row(ws)

            duration_s = time.monotonic() - started
            data_rows  = len(rows) - 1   # exclude header

            result = SheetsSyncResult(
                worksheet_name=ws_name,
                rows_written=data_rows,
                columns_written=len(config.headers),
                duration_s=duration_s,
                spreadsheet_url=self._client.spreadsheet_url,
                success=True,
            )

            log.info(
                "Sheets write complete — %s",
                result,
                extra={
                    "worksheet":     ws_name,
                    "entity":        entity_label,
                    "rows_written":  data_rows,
                    "cols_written":  len(config.headers),
                    "duration_s":    round(duration_s, 2),
                    "integration":   "google_sheets",
                },
            )
            return result

        except (GoogleSheetsError, gspread.exceptions.GSpreadException) as exc:
            duration_s = time.monotonic() - started
            log.error(
                "Sheets write failed — worksheet='%s' entity=%s: %s",
                ws_name, entity_label, exc,
                extra={
                    "worksheet":   ws_name,
                    "entity":      entity_label,
                    "error":       str(exc),
                    "duration_s":  round(duration_s, 2),
                    "integration": "google_sheets",
                },
            )
            return SheetsSyncResult(
                worksheet_name=ws_name,
                duration_s=duration_s,
                success=False,
                error=str(exc),
                spreadsheet_url=self._client.spreadsheet_url,
            )

    # ------------------------------------------------------------------
    # Daily summary helpers
    # ------------------------------------------------------------------

    def _ensure_daily_summary_header(self, ws: gspread.Worksheet) -> None:
        """
        Write the Daily_Summary header row if row 1 is empty.

        Idempotent — if row 1 already has content, does nothing.
        """
        try:
            _time_quota_sleep()
            first_row = ws.row_values(1)
            if not any(v.strip() for v in first_row):
                ws.update([DAILY_SUMMARY_WORKSHEET_CONFIG.headers], "A1")
                self._client.format_header_row(ws)
                log.debug(
                    "Daily_Summary header written",
                    extra={"worksheet": SHEET_DAILY_SUMMARY, "integration": "google_sheets"},
                )
        except gspread.exceptions.GSpreadException as exc:
            log.warning(
                "Could not ensure Daily_Summary header (non-fatal): %s", exc,
                extra={"worksheet": SHEET_DAILY_SUMMARY, "integration": "google_sheets"},
            )

    @_retry_sheets
    def _append_rows_with_retry(
        self,
        ws: gspread.Worksheet,
        rows: list[list[Any]],
    ) -> None:
        """Append rows to the worksheet after the last non-empty row, with retry."""
        _time_quota_sleep()
        ws.append_rows(rows, value_input_option="USER_ENTERED")


# =============================================================================
# Module-level utility: quota sleep
# =============================================================================

def _time_quota_sleep() -> None:
    """
    Sleep briefly between successive Google Sheets API calls.

    The Sheets API enforces ~60 requests/minute for write operations.
    Sleeping 1.1 seconds between calls keeps us safely within quota
    without needing to track a request counter.

    This is called before every API call by the GoogleSheetsClient methods.
    """
    time.sleep(_QUOTA_SLEEP_SECONDS)


# =============================================================================
# Convenience: load JSON output file
# =============================================================================

def load_json_output(file_path: str | Path) -> dict[str, Any]:
    """
    Load a Kommo extraction output JSON file from disk.

    Handles the standard output format:
      {
        "_meta": { ... },
        "data":  [ ... ]
      }

    Also handles the flattened messages format:
      {
        "_meta":    { ... },
        "messages": [ ... ]
      }

    Args:
        file_path: Path to the JSON output file.

    Returns:
        Full parsed dict.

    Raises:
        FileNotFoundError: If the file does not exist.
        json.JSONDecodeError: If the file is not valid JSON.
        ValueError: If the file is empty.

    Example:
        payload = load_json_output("outputs/leads.json")
        records = payload.get("data") or []
    """
    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(
            f"Output file not found: {path}. "
            "Run the extraction pipeline first (python main.py)."
        )

    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        raise ValueError(f"Output file is empty: {path}")

    return json.loads(raw)
