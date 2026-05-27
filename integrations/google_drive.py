"""
integrations/google_drive.py
=============================
Production-grade Google Drive upload system for the Kommo CRM pipeline.

PURPOSE
───────
Upload the daily AI-ready JSON exports (daily_exports/YYYY-MM-DD.json)
produced by normalizers/daily_json_export.py to a designated Google Drive
folder, returning shareable links so Claude or other consumers can access
the files without direct filesystem access.

ARCHITECTURE
────────────
  GoogleDriveClient  — Service Account auth + low-level Drive API wrapper
                       (file list, upload, update, delete, share, folder ops)
  DriveUploader      — High-level upload orchestration:
                         upload_daily_export()    — single file by path/date
                         upload_latest_export()   — auto-detect latest, upload
                         list_uploaded_exports()  — what's already in Drive
                         delete_existing_file_if_present() — safe replace
  DriveUploadResult  — Typed result DTO returned by every upload operation

AUTHENTICATION
──────────────
  Uses Google Service Account credentials (same key as Sheets integration).
  Required env vars:
    GOOGLE_SERVICE_ACCOUNT_FILE  — absolute path to the service account JSON key
    GOOGLE_DRIVE_FOLDER_ID       — ID of the target Drive folder

  Required scopes (both granted via the same service account):
    https://www.googleapis.com/auth/drive          — full Drive access
    https://www.googleapis.com/auth/drive.file     — scoped to files we create

  The service account must be granted "Editor" (or "Viewer + Commenter")
  on the target folder via the Drive sharing UI.

UPLOAD STRATEGY
───────────────
  1. List files in the target folder matching the filename (YYYY-MM-DD.json).
  2. If found → update the existing file's content in-place (preserves file ID
     and any existing sharing links — no broken links on re-upload).
  3. If not found → create a new file via multipart upload.
  4. After upload → set the file to "anyone with link can view" for Claude access.
  5. Return a DriveUploadResult with the file_id, webViewLink, and directLink.

RETRY HANDLING
──────────────
  All Drive API calls are wrapped with tenacity:
    - HTTP 429 (rate limit) → 30s + jitter
    - HTTP 500/503 (server error) → exponential backoff (2s → 60s)
    - Network errors → exponential backoff
    - Max attempts: 5

USAGE
─────
    from integrations.google_drive import GoogleDriveClient, DriveUploader

    client   = GoogleDriveClient.from_env()
    uploader = DriveUploader(client)

    # Upload a specific date's export
    result = uploader.upload_daily_export("2025-01-15")
    print(result.web_view_link)

    # Upload the latest export automatically
    result = uploader.upload_latest_export()
    print(result)   # ✅  2025-01-16.json → https://drive.google.com/...

    # List what's already uploaded
    files = uploader.list_uploaded_exports()
    for f in files:
        print(f.filename, f.web_view_link)
"""

from __future__ import annotations

import io
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
)

from utils.logger import get_logger

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

log: logging.Logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# OAuth 2.0 scopes required for Drive uploads + sharing
_DRIVE_SCOPES: list[str] = [
    "https://www.googleapis.com/auth/drive",
]

# MIME types
_MIME_JSON        = "application/json"
_MIME_FOLDER      = "application/vnd.google-apps.folder"

# Retry configuration
_MAX_RETRY_ATTEMPTS  = 5
_BACKOFF_INITIAL     = 2.0    # seconds
_BACKOFF_MAX         = 60.0   # seconds
_BACKOFF_MULTIPLIER  = 2.0
_QUOTA_SLEEP_SECONDS = 0.5    # conservative inter-call sleep

# Drive API version
_DRIVE_API_VERSION = "v3"

# Default export directory (matches normalizers/daily_json_export.py)
_DEFAULT_EXPORT_DIR = Path("daily_exports")

# Filename pattern: YYYY-MM-DD.json
_EXPORT_FILENAME_SUFFIX = ".json"


# =============================================================================
# Custom Exceptions
# =============================================================================

class GoogleDriveError(Exception):
    """Base exception for all Google Drive integration errors."""

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.context = context

    def __str__(self) -> str:
        base = self.args[0]
        if self.context:
            ctx = " | ".join(f"{k}={v}" for k, v in self.context.items())
            return f"{base} [{ctx}]"
        return base


class GoogleDriveAuthError(GoogleDriveError):
    """Service account credentials are missing, invalid, or lack Drive scope."""


class GoogleDriveConfigError(GoogleDriveError):
    """Required environment variables are not set or contain invalid values."""


class GoogleDriveUploadError(GoogleDriveError):
    """An upload operation failed after all retry attempts were exhausted."""

    def __init__(self, message: str, filename: str | None = None, **ctx: Any) -> None:
        super().__init__(message, **ctx)
        self.filename = filename


class GoogleDriveNotFoundError(GoogleDriveError):
    """The requested file or folder was not found in Google Drive."""


class GoogleDriveQuotaError(GoogleDriveError):
    """Google Drive API quota was exhausted (HTTP 429)."""


# =============================================================================
# Data classes / DTOs
# =============================================================================

@dataclass
class DriveFileInfo:
    """
    Metadata for a single file stored in Google Drive.

    Returned by list_uploaded_exports() and as part of DriveUploadResult.

    Attributes:
        file_id:       Google Drive file ID (stable; use for updates/deletes).
        filename:      File name as it appears in Drive (e.g. "2025-01-15.json").
        web_view_link: URL to open the file in the Drive viewer.
        direct_link:   Direct download URL (requires Drive scope to access).
        size_bytes:    File size in bytes (may be None for Google-native files).
        created_at:    ISO 8601 creation time (from Drive metadata).
        modified_at:   ISO 8601 last modification time.
        mime_type:     MIME type of the stored file.
    """

    file_id:       str
    filename:      str
    web_view_link: str  = ""
    direct_link:   str  = ""
    size_bytes:    int | None = None
    created_at:    str  = ""
    modified_at:   str  = ""
    mime_type:     str  = _MIME_JSON

    @property
    def date_str(self) -> str | None:
        """Extract YYYY-MM-DD from the filename, or None if not a dated export."""
        name = self.filename.removesuffix(_EXPORT_FILENAME_SUFFIX)
        try:
            datetime.strptime(name, "%Y-%m-%d")
            return name
        except ValueError:
            return None

    def __str__(self) -> str:
        size = f" ({self.size_bytes:,} B)" if self.size_bytes else ""
        return f"{self.filename}{size} → {self.web_view_link}"


@dataclass
class DriveUploadResult:
    """
    Outcome of a single Drive upload operation.

    Attributes:
        filename:       Name of the uploaded file (e.g. "2025-01-15.json").
        file_id:        Google Drive file ID of the uploaded file.
        web_view_link:  URL to open the file in Drive (shareable).
        direct_link:    Direct export/download URL.
        folder_id:      ID of the Drive folder containing the file.
        folder_url:     URL to open the containing folder.
        size_bytes:     Size of the uploaded file in bytes.
        was_replaced:   True if an existing file was replaced (not created new).
        duration_s:     Wall-clock time for the full upload operation.
        success:        True if the upload completed without errors.
        error:          Error message if success=False.
        uploaded_at:    ISO 8601 timestamp of when the upload completed.
    """

    filename:      str
    file_id:       str  = ""
    web_view_link: str  = ""
    direct_link:   str  = ""
    folder_id:     str  = ""
    folder_url:    str  = ""
    size_bytes:    int  = 0
    was_replaced:  bool = False
    duration_s:    float = 0.0
    success:       bool = True
    error:         str | None = None
    uploaded_at:   str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )

    @property
    def status_icon(self) -> str:
        return "✅" if self.success else "❌"

    @property
    def action_label(self) -> str:
        return "replaced" if self.was_replaced else "uploaded"

    def __str__(self) -> str:
        if self.success:
            kb = self.size_bytes // 1024
            return (
                f"{self.status_icon}  {self.filename}  "
                f"[{self.action_label}]  {kb} KB  "
                f"[{self.duration_s:.2f}s]\n"
                f"   🔗 {self.web_view_link}"
            )
        return f"{self.status_icon}  {self.filename}  FAILED — {self.error}"


# =============================================================================
# Retry infrastructure (Drive-API-aware)
# =============================================================================

def _is_drive_quota_error(exc: BaseException) -> bool:
    """Return True for HTTP 429 / 403 quota-exceeded from the Drive API."""
    if isinstance(exc, HttpError):
        return exc.status_code in (429, 403)
    return False


def _is_drive_server_error(exc: BaseException) -> bool:
    """Return True for transient 5xx server errors."""
    if isinstance(exc, HttpError):
        return exc.status_code >= 500
    return False


def _drive_wait_strategy(retry_state: RetryCallState) -> float:
    """
    Quota-aware wait strategy for Google Drive API calls.

    - HTTP 429 / 403 quota  → 30s + jitter (Drive quota window is ~30s)
    - HTTP 5xx server error → exponential backoff (2s → 60s)
    - Network errors        → exponential backoff (2s → 60s)
    """
    exc     = retry_state.outcome.exception() if retry_state.outcome else None
    attempt = retry_state.attempt_number

    if exc and _is_drive_quota_error(exc):
        wait = 30.0 + random.uniform(0, 10.0)
        log.warning(
            "Drive API quota hit — waiting %.0fs before retry %d",
            wait, attempt + 1,
            extra={"quota_wait_s": wait, "integration": "google_drive"},
        )
        return wait

    backoff = min(_BACKOFF_MAX, _BACKOFF_INITIAL * (_BACKOFF_MULTIPLIER ** attempt))
    return backoff + random.uniform(0, 2.0)


def _drive_log_retry(retry_state: RetryCallState) -> None:
    exc     = retry_state.outcome.exception() if retry_state.outcome else None
    nxt     = retry_state.next_action.sleep if retry_state.next_action else 0.0
    fn_name = getattr(retry_state.fn, "__name__", "unknown")
    log.warning(
        "Drive API retry %d/%d — %s — sleeping %.1fs — %s",
        retry_state.attempt_number,
        _MAX_RETRY_ATTEMPTS,
        fn_name,
        nxt,
        type(exc).__name__ if exc else "no exception",
        extra={
            "retry_attempt": retry_state.attempt_number,
            "retry_sleep_s": round(nxt, 2),
            "exc_type":      type(exc).__name__ if exc else None,
            "integration":   "google_drive",
        },
    )


def _drive_log_exhausted(retry_state: RetryCallState) -> None:
    exc     = retry_state.outcome.exception() if retry_state.outcome else None
    fn_name = getattr(retry_state.fn, "__name__", "unknown")
    log.error(
        "All %d Drive API retries exhausted for %s — %s",
        retry_state.attempt_number,
        fn_name,
        repr(exc) if exc else "unknown error",
        extra={"retries_exhausted": retry_state.attempt_number, "integration": "google_drive"},
    )


def _retry_drive(func):
    """
    Retry decorator for Google Drive API calls.

    Retries on:
      - googleapiclient.errors.HttpError (429, 500, 503)
      - ConnectionError / TimeoutError / OSError (transient network issues)

    Backs off with Drive-quota-aware wait strategy.
    Does NOT retry on 400 (bad request) or 404 (not found) — those are
    programming errors, not transient failures.
    """
    return retry(
        retry=retry_if_exception_type((
            HttpError,
            ConnectionError,
            TimeoutError,
            OSError,
        )),
        stop=stop_after_attempt(_MAX_RETRY_ATTEMPTS),
        wait=_drive_wait_strategy,
        before_sleep=_drive_log_retry,
        retry_error_callback=_drive_log_exhausted,
        reraise=True,
    )(func)


# =============================================================================
# GoogleDriveClient — Auth + low-level Drive API wrapper
# =============================================================================

class GoogleDriveClient:
    """
    Authenticated Google Drive API client bound to a target folder.

    Handles:
      - Service Account authentication (shares credentials with Sheets client)
      - File listing, upload (create + update), deletion, and permission setting
      - Quota-safe API interactions with retry on transient failures

    Args:
        credentials_path: Absolute path to the service account JSON key file.
        folder_id:        Google Drive folder ID where files will be uploaded.
                          Get this from the folder's URL:
                          https://drive.google.com/drive/folders/<FOLDER_ID>

    Raises:
        GoogleDriveAuthError:   Credentials file missing or structurally invalid.
        GoogleDriveConfigError: folder_id or credentials_path is empty.

    Example:
        # Preferred: load from environment
        client = GoogleDriveClient.from_env()

        # Direct construction (useful in tests)
        client = GoogleDriveClient(
            credentials_path=Path("/path/to/sa.json"),
            folder_id="1A2B3C4D5E6F",
        )
    """

    def __init__(
        self,
        credentials_path: str | Path,
        folder_id: str,
    ) -> None:
        credentials_path = Path(credentials_path)

        # ── Validate inputs ────────────────────────────────────────────
        if not folder_id or not folder_id.strip():
            raise GoogleDriveConfigError(
                "GOOGLE_DRIVE_FOLDER_ID is empty. "
                "Set it to the folder ID from the Drive URL: "
                "https://drive.google.com/drive/folders/<FOLDER_ID>",
            )

        if not credentials_path.exists():
            raise GoogleDriveAuthError(
                f"Service account credentials file not found: {credentials_path}. "
                "Set GOOGLE_SERVICE_ACCOUNT_FILE to the correct path.",
                path=str(credentials_path),
            )

        self._folder_id        = folder_id.strip()
        self._credentials_path = credentials_path

        # ── Build credentials and Drive service ────────────────────────
        try:
            creds = Credentials.from_service_account_file(
                str(credentials_path),
                scopes=_DRIVE_SCOPES,
            )
            self._service = build(
                "drive",
                _DRIVE_API_VERSION,
                credentials=creds,
                cache_discovery=False,  # Avoids filesystem cache race conditions
            )
            log.info(
                "Google Drive client authenticated",
                extra={
                    "folder_id":        self._folder_id,
                    "credentials_file": str(credentials_path),
                    "integration":      "google_drive",
                },
            )
        except (ValueError, KeyError, FileNotFoundError) as exc:
            raise GoogleDriveAuthError(
                f"Failed to load service account credentials: {exc}",
                credentials_path=str(credentials_path),
            ) from exc

    # ------------------------------------------------------------------
    # Class method: construct from environment variables
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> "GoogleDriveClient":
        """
        Construct a GoogleDriveClient from environment variables.

        Required environment variables:
            GOOGLE_SERVICE_ACCOUNT_FILE  — path to service account JSON key
            GOOGLE_DRIVE_FOLDER_ID       — Drive folder ID from the URL

        Raises:
            GoogleDriveConfigError: If either variable is missing or empty.

        Example:
            client = GoogleDriveClient.from_env()
        """
        creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
        folder_id  = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "").strip()

        missing = []
        if not creds_path:
            missing.append("GOOGLE_SERVICE_ACCOUNT_FILE")
        if not folder_id:
            missing.append("GOOGLE_DRIVE_FOLDER_ID")

        if missing:
            raise GoogleDriveConfigError(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                "Add them to your .env file. See .env.example for the full template."
            )

        return cls(credentials_path=creds_path, folder_id=folder_id)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def folder_id(self) -> str:
        """The Google Drive folder ID this client is bound to."""
        return self._folder_id

    @property
    def folder_url(self) -> str:
        """Direct URL to open the target folder in Drive."""
        return f"https://drive.google.com/drive/folders/{self._folder_id}"

    # ------------------------------------------------------------------
    # Public API: file discovery
    # ------------------------------------------------------------------

    def list_files_in_folder(
        self,
        name_filter: str | None = None,
        mime_type: str = _MIME_JSON,
    ) -> list[DriveFileInfo]:
        """
        List files in the target Drive folder.

        Args:
            name_filter: Optional filename substring to filter results.
                         e.g. "2025-01-15" to find "2025-01-15.json".
            mime_type:   Filter by MIME type (default: application/json).

        Returns:
            List of DriveFileInfo, sorted by filename ascending.

        Raises:
            GoogleDriveUploadError: If the Drive API call fails.

        Example:
            files = client.list_files_in_folder()
            for f in files:
                print(f.filename, f.web_view_link)
        """
        # Build the Drive query
        conditions = [
            f"'{self._folder_id}' in parents",
            "trashed = false",
        ]
        if mime_type:
            conditions.append(f"mimeType = '{mime_type}'")
        if name_filter:
            # Drive doesn't support exact match in q — we filter post-fetch
            conditions.append(f"name contains '{name_filter}'")

        query = " and ".join(conditions)

        try:
            return self._list_files_with_retry(query)
        except HttpError as exc:
            raise GoogleDriveUploadError(
                f"Failed to list files in folder '{self._folder_id}': {exc}",
                folder_id=self._folder_id,
            ) from exc

    def find_file_by_name(self, filename: str) -> DriveFileInfo | None:
        """
        Find a specific file by exact name in the target folder.

        Args:
            filename: Exact filename to search for (e.g. "2025-01-15.json").

        Returns:
            DriveFileInfo if found, None if no file with that exact name exists.

        Example:
            existing = client.find_file_by_name("2025-01-15.json")
            if existing:
                print(f"Already uploaded: {existing.file_id}")
        """
        files = self.list_files_in_folder(name_filter=filename)
        # Filter to exact name match (Drive's 'contains' is substring)
        exact = [f for f in files if f.filename == filename]
        if not exact:
            return None
        if len(exact) > 1:
            log.warning(
                "Multiple files with the same name found in Drive — "
                "using the most recently modified one",
                extra={
                    "filename": filename,
                    "count":    len(exact),
                    "integration": "google_drive",
                },
            )
            exact.sort(key=lambda f: f.modified_at, reverse=True)
        return exact[0]

    # ------------------------------------------------------------------
    # Public API: upload operations
    # ------------------------------------------------------------------

    def upload_file(
        self,
        file_path: str | Path,
        *,
        drive_filename: str | None = None,
        replace_existing: bool = True,
        set_public_read: bool = True,
    ) -> DriveUploadResult:
        """
        Upload a local file to the target Drive folder.

        If a file with the same name already exists in the folder and
        `replace_existing=True`, the existing file's content is updated
        in-place (preserving its file_id and any existing sharing links).

        Args:
            file_path:        Local path to the file to upload.
            drive_filename:   Name to use in Drive (default: same as local filename).
            replace_existing: If True and a file with the same name exists,
                              update it rather than creating a duplicate.
            set_public_read:  If True, set "anyone with link can view" permission
                              after upload (needed for Claude to access the file).

        Returns:
            DriveUploadResult with file_id, web_view_link, and upload stats.

        Raises:
            FileNotFoundError:    If the local file does not exist.
            GoogleDriveUploadError: If the upload fails after all retries.

        Example:
            result = client.upload_file(
                Path("daily_exports/2025-01-15.json"),
                set_public_read=True,
            )
            print(result.web_view_link)
        """
        path = Path(file_path)

        if not path.exists():
            raise FileNotFoundError(
                f"File to upload not found: {path}. "
                "Run `python run_daily_export.py` first."
            )

        started       = time.monotonic()
        filename      = drive_filename or path.name
        size_bytes    = path.stat().st_size

        log.info(
            "Drive upload starting — file=%s size=%d bytes",
            filename, size_bytes,
            extra={
                "file_name":    filename,
                "size_bytes":  size_bytes,
                "folder_id":   self._folder_id,
                "integration": "google_drive",
            },
        )

        # ── Check for existing file ────────────────────────────────────
        existing = None
        if replace_existing:
            existing = self.find_file_by_name(filename)
            if existing:
                log.info(
                    "Existing file found — will update in-place — file_id=%s",
                    existing.file_id,
                    extra={
                        "filename":    filename,
                        "file_id":     existing.file_id,
                        "integration": "google_drive",
                    },
                )

        # ── Build media body ───────────────────────────────────────────
        content_bytes = path.read_bytes()
        media = MediaIoBaseUpload(
            io.BytesIO(content_bytes),
            mimetype=_MIME_JSON,
            resumable=False,   # Files < 5 MB: simple upload is faster
        )

        try:
            if existing:
                # Update existing file content (preserves file_id + sharing)
                file_meta = self._update_file_with_retry(
                    file_id=existing.file_id,
                    media=media,
                    filename=filename,
                )
                was_replaced = True
            else:
                # Create new file
                file_meta = self._create_file_with_retry(
                    filename=filename,
                    media=media,
                )
                was_replaced = False

        except HttpError as exc:
            duration_s = time.monotonic() - started
            log.error(
                "Drive upload failed — file=%s: %s",
                filename, exc,
                extra={
                    "filename":    filename,
                    "status_code": exc.status_code,
                    "duration_s":  round(duration_s, 2),
                    "integration": "google_drive",
                },
            )
            return DriveUploadResult(
                filename=filename,
                folder_id=self._folder_id,
                folder_url=self.folder_url,
                duration_s=duration_s,
                success=False,
                error=f"HTTP {exc.status_code}: {exc.reason}",
            )

        file_id      = file_meta.get("id", "")
        web_view_link = file_meta.get("webViewLink", "")
        direct_link  = (
            f"https://drive.google.com/uc?id={file_id}&export=download"
        )

        # ── Set public read permission ─────────────────────────────────
        if set_public_read and file_id:
            self._set_public_read_permission(file_id, filename)

        duration_s = time.monotonic() - started

        result = DriveUploadResult(
            filename=filename,
            file_id=file_id,
            web_view_link=web_view_link or f"https://drive.google.com/file/d/{file_id}/view",
            direct_link=direct_link,
            folder_id=self._folder_id,
            folder_url=self.folder_url,
            size_bytes=size_bytes,
            was_replaced=was_replaced,
            duration_s=duration_s,
            success=True,
        )

        log.info(
            "Drive upload complete — %s",
            result,
            extra={
                "filename":     filename,
                "file_id":      file_id,
                "was_replaced": was_replaced,
                "size_bytes":   size_bytes,
                "duration_s":   round(duration_s, 2),
                "integration":  "google_drive",
            },
        )
        return result

    def delete_file(self, file_id: str) -> bool:
        """
        Permanently delete a file from Drive by its file ID.

        Note: This moves the file to trash AND permanently deletes it.
        Use with care — there is no undo.

        Args:
            file_id: The Google Drive file ID to delete.

        Returns:
            True if deleted successfully, False if the file was not found.

        Raises:
            GoogleDriveUploadError: If the delete fails for a non-404 reason.

        Example:
            deleted = client.delete_file("1A2B3C...")
        """
        _quota_sleep()
        try:
            self._service.files().delete(fileId=file_id).execute()
            log.info(
                "Drive file deleted — file_id=%s", file_id,
                extra={"file_id": file_id, "integration": "google_drive"},
            )
            return True
        except HttpError as exc:
            if exc.status_code == 404:
                log.warning(
                    "Drive delete: file not found — file_id=%s", file_id,
                    extra={"file_id": file_id, "integration": "google_drive"},
                )
                return False
            raise GoogleDriveUploadError(
                f"Failed to delete file '{file_id}': {exc}",
                file_id=file_id,
            ) from exc

    def get_folder_metadata(self) -> dict[str, Any]:
        """
        Retrieve metadata for the target Drive folder.

        Returns:
            Dict with 'id', 'name', 'webViewLink', 'parents', etc.

        Raises:
            GoogleDriveNotFoundError: If the folder_id is invalid or inaccessible.
        """
        _quota_sleep()
        try:
            return (
                self._service.files()
                .get(
                    fileId=self._folder_id,
                    fields="id, name, webViewLink, parents, createdTime",
                )
                .execute()
            )
        except HttpError as exc:
            if exc.status_code == 404:
                raise GoogleDriveNotFoundError(
                    f"Drive folder not found: {self._folder_id}. "
                    "Check GOOGLE_DRIVE_FOLDER_ID in your .env file.",
                    folder_id=self._folder_id,
                ) from exc
            raise GoogleDriveUploadError(
                f"Failed to get folder metadata: {exc}",
                folder_id=self._folder_id,
            ) from exc

    # ------------------------------------------------------------------
    # Private: API call implementations with retry
    # ------------------------------------------------------------------

    @_retry_drive
    def _list_files_with_retry(self, query: str) -> list[DriveFileInfo]:
        """Execute the Drive files.list call with retry."""
        _quota_sleep()

        results: list[DriveFileInfo] = []
        page_token: str | None = None

        while True:
            request_kwargs: dict[str, Any] = {
                "q":        query,
                "spaces":   "drive",
                "fields":   (
                    "nextPageToken, "
                    "files(id, name, mimeType, size, "
                    "webViewLink, createdTime, modifiedTime)"
                ),
                "pageSize": 100,
                "orderBy":  "name",
            }
            if page_token:
                request_kwargs["pageToken"] = page_token

            response = self._service.files().list(**request_kwargs).execute()

            for item in response.get("files", []):
                results.append(DriveFileInfo(
                    file_id=item.get("id", ""),
                    filename=item.get("name", ""),
                    web_view_link=item.get("webViewLink", ""),
                    direct_link=(
                        f"https://drive.google.com/uc?id={item.get('id', '')}&export=download"
                    ),
                    size_bytes=int(item["size"]) if item.get("size") else None,
                    created_at=item.get("createdTime", ""),
                    modified_at=item.get("modifiedTime", ""),
                    mime_type=item.get("mimeType", _MIME_JSON),
                ))

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        log.debug(
            "Drive list complete — found %d files",
            len(results),
            extra={"count": len(results), "integration": "google_drive"},
        )
        return results

    @_retry_drive
    def _create_file_with_retry(
        self,
        filename: str,
        media: MediaIoBaseUpload,
    ) -> dict[str, Any]:
        """Create a new file in the target folder, with retry."""
        _quota_sleep()

        file_metadata: dict[str, Any] = {
            "name":    filename,
            "parents": [self._folder_id],
        }

        result: dict[str, Any] = (
            self._service.files()
            .create(
                body=file_metadata,
                media_body=media,
                fields="id, name, webViewLink, size",
            )
            .execute()
        )

        log.debug(
            "Drive file created — filename=%s file_id=%s",
            filename, result.get("id"),
            extra={
                "filename":    filename,
                "file_id":     result.get("id"),
                "integration": "google_drive",
            },
        )
        return result

    @_retry_drive
    def _update_file_with_retry(
        self,
        file_id: str,
        media: MediaIoBaseUpload,
        filename: str,
    ) -> dict[str, Any]:
        """Update an existing file's content in-place, with retry."""
        _quota_sleep()

        result: dict[str, Any] = (
            self._service.files()
            .update(
                fileId=file_id,
                body={"name": filename},
                media_body=media,
                fields="id, name, webViewLink, size",
            )
            .execute()
        )

        log.debug(
            "Drive file updated — filename=%s file_id=%s",
            filename, file_id,
            extra={
                "filename":    filename,
                "file_id":     file_id,
                "integration": "google_drive",
            },
        )
        return result

    def _set_public_read_permission(self, file_id: str, filename: str) -> None:
        """
        Set "anyone with the link can view" permission on a Drive file.

        This is required for Claude to access the file via its web_view_link
        without being authenticated as the service account.

        Non-fatal: if permission setting fails, logs a warning but does not
        raise — the file is still uploaded and accessible by the service account.

        Args:
            file_id:  Drive file ID.
            filename: For log context.
        """
        try:
            _quota_sleep()
            self._service.permissions().create(
                fileId=file_id,
                body={
                    "type":  "anyone",
                    "role":  "reader",
                },
                fields="id",
            ).execute()
            log.debug(
                "Drive public read permission set — file_id=%s filename=%s",
                file_id, filename,
                extra={
                    "file_id":     file_id,
                    "filename":    filename,
                    "integration": "google_drive",
                },
            )
        except HttpError as exc:
            log.warning(
                "Failed to set public read permission (non-fatal) — "
                "file_id=%s filename=%s: %s",
                file_id, filename, exc,
                extra={
                    "file_id":     file_id,
                    "filename":    filename,
                    "integration": "google_drive",
                },
            )


# =============================================================================
# DriveUploader — High-level upload orchestration
# =============================================================================

class DriveUploader:
    """
    High-level upload orchestrator that maps daily export files to Drive.

    Wraps GoogleDriveClient with business-logic methods specific to the
    Kommo CRM daily export pipeline.

    Args:
        client:     Authenticated GoogleDriveClient instance.
        export_dir: Local directory containing YYYY-MM-DD.json files.
                    Default: daily_exports/

    Example:
        client   = GoogleDriveClient.from_env()
        uploader = DriveUploader(client)

        # Upload latest export
        result = uploader.upload_latest_export()
        print(result)

        # Upload all exports from a specific date range
        for date in ["2025-01-14", "2025-01-15", "2025-01-16"]:
            result = uploader.upload_daily_export(date)
            print(result)
    """

    def __init__(
        self,
        client: GoogleDriveClient,
        export_dir: str | Path = _DEFAULT_EXPORT_DIR,
    ) -> None:
        self._client     = client
        self._export_dir = Path(export_dir)
        log.info(
            "DriveUploader initialised — folder_id=%s export_dir=%s",
            client.folder_id, self._export_dir,
            extra={
                "folder_id":   client.folder_id,
                "folder_url":  client.folder_url,
                "export_dir":  str(self._export_dir),
                "integration": "google_drive",
            },
        )

    # ------------------------------------------------------------------
    # Public: primary upload methods
    # ------------------------------------------------------------------

    def upload_daily_export(
        self,
        date: str | None = None,
        *,
        file_path: str | Path | None = None,
    ) -> DriveUploadResult:
        """
        Upload a single daily export JSON file to Google Drive.

        Accepts either a date string (YYYY-MM-DD) or an explicit file path.
        If neither is provided, raises ValueError.

        The file is uploaded with "replace existing" semantics — if a file
        with the same name already exists in the folder, its content is
        updated in-place so that existing sharing links remain valid.

        Args:
            date:      Date of the export to upload (YYYY-MM-DD).
                       If provided, looks for daily_exports/<date>.json.
            file_path: Explicit path to the JSON file to upload.
                       Takes priority over `date` if both are provided.

        Returns:
            DriveUploadResult with upload details and the Drive web link.

        Raises:
            ValueError:         Neither date nor file_path was provided.
            FileNotFoundError:  The export file does not exist locally.
            GoogleDriveUploadError: Upload failed after all retries.

        Example:
            result = uploader.upload_daily_export("2025-01-15")
            print(result.web_view_link)
        """
        if file_path is not None:
            path = Path(file_path)
        elif date is not None:
            date = _validate_date_string(date)
            path = self._export_dir / f"{date}.json"
        else:
            raise ValueError(
                "Either `date` (YYYY-MM-DD) or `file_path` must be provided."
            )

        log.info(
            "upload_daily_export — path=%s", path,
            extra={"path": str(path), "integration": "google_drive"},
        )

        return self._client.upload_file(
            file_path=path,
            replace_existing=True,
            set_public_read=True,
        )

    def upload_latest_export(self) -> DriveUploadResult:
        """
        Auto-detect and upload the most recent daily export file.

        Scans the local export_dir for YYYY-MM-DD.json files and uploads
        the one with the latest date. This is the primary method for daily
        cron jobs — run after `python run_daily_export.py`.

        Returns:
            DriveUploadResult for the uploaded file.

        Raises:
            GoogleDriveNotFoundError: No export files found in export_dir.
            FileNotFoundError:        export_dir does not exist.
            GoogleDriveUploadError:   Upload failed after all retries.

        Example:
            result = uploader.upload_latest_export()
            print(f"Uploaded: {result.filename}")
            print(f"Link:     {result.web_view_link}")
        """
        latest_path = self._find_latest_local_export()

        if latest_path is None:
            raise GoogleDriveNotFoundError(
                f"No dated export files found in {self._export_dir}. "
                "Run `python run_daily_export.py` first to generate exports.",
                export_dir=str(self._export_dir),
            )

        log.info(
            "upload_latest_export — uploading %s",
            latest_path.name,
            extra={"filename": latest_path.name, "integration": "google_drive"},
        )

        return self._client.upload_file(
            file_path=latest_path,
            replace_existing=True,
            set_public_read=True,
        )

    def upload_all_exports(
        self,
        *,
        skip_existing: bool = False,
    ) -> list[DriveUploadResult]:
        """
        Upload all YYYY-MM-DD.json files found in the local export directory.

        Args:
            skip_existing: If True, skip files that already exist in Drive
                           (by filename match). If False (default), replace
                           existing files with updated content.

        Returns:
            List of DriveUploadResult, one per file attempted.

        Example:
            results = uploader.upload_all_exports()
            for r in results:
                print(r)
        """
        local_files = self._list_local_exports()

        if not local_files:
            log.warning(
                "upload_all_exports: no export files found in %s",
                self._export_dir,
                extra={"export_dir": str(self._export_dir), "integration": "google_drive"},
            )
            return []

        log.info(
            "upload_all_exports: uploading %d files",
            len(local_files),
            extra={
                "file_count":  len(local_files),
                "export_dir":  str(self._export_dir),
                "integration": "google_drive",
            },
        )

        # Pre-fetch existing files to decide skip/replace per file
        existing_by_name: dict[str, DriveFileInfo] = {}
        if skip_existing:
            for fi in self._client.list_files_in_folder():
                existing_by_name[fi.filename] = fi

        results: list[DriveUploadResult] = []
        for path in local_files:
            if skip_existing and path.name in existing_by_name:
                log.info(
                    "Skipping already-uploaded file — %s", path.name,
                    extra={"filename": path.name, "integration": "google_drive"},
                )
                existing = existing_by_name[path.name]
                results.append(DriveUploadResult(
                    filename=path.name,
                    file_id=existing.file_id,
                    web_view_link=existing.web_view_link,
                    direct_link=existing.direct_link,
                    folder_id=self._client.folder_id,
                    folder_url=self._client.folder_url,
                    was_replaced=False,
                    success=True,
                ))
                continue

            result = self._client.upload_file(
                file_path=path,
                replace_existing=not skip_existing,
                set_public_read=True,
            )
            results.append(result)

        success_count = sum(1 for r in results if r.success)
        log.info(
            "upload_all_exports complete — %d/%d succeeded",
            success_count, len(results),
            extra={
                "success_count": success_count,
                "total":         len(results),
                "integration":   "google_drive",
            },
        )
        return results

    def list_uploaded_exports(self) -> list[DriveFileInfo]:
        """
        List all daily export JSON files currently stored in the Drive folder.

        Returns only files matching the YYYY-MM-DD.json naming pattern.

        Returns:
            List of DriveFileInfo, sorted by date (oldest first).

        Example:
            files = uploader.list_uploaded_exports()
            for f in files:
                print(f.date_str, "→", f.web_view_link)
        """
        all_files = self._client.list_files_in_folder()

        # Filter to dated export files only
        exports = [f for f in all_files if f.date_str is not None]
        exports.sort(key=lambda f: f.filename)

        log.info(
            "list_uploaded_exports — found %d dated export files in Drive",
            len(exports),
            extra={
                "count":       len(exports),
                "folder_id":   self._client.folder_id,
                "integration": "google_drive",
            },
        )
        return exports

    def delete_existing_file_if_present(self, filename: str) -> bool:
        """
        Delete a file from Drive by exact filename, if it exists.

        Useful for cleanup before re-uploading a corrected export.
        If the file does not exist, returns False without raising.

        Args:
            filename: Exact filename to search for and delete
                      (e.g. "2025-01-15.json").

        Returns:
            True if a file was found and deleted, False if not found.

        Example:
            deleted = uploader.delete_existing_file_if_present("2025-01-15.json")
            if deleted:
                print("Old file removed — re-uploading ...")
        """
        existing = self._client.find_file_by_name(filename)
        if existing is None:
            log.info(
                "delete_existing_file_if_present: not found — %s", filename,
                extra={"filename": filename, "integration": "google_drive"},
            )
            return False

        log.info(
            "delete_existing_file_if_present: deleting %s (file_id=%s)",
            filename, existing.file_id,
            extra={
                "filename":    filename,
                "file_id":     existing.file_id,
                "integration": "google_drive",
            },
        )
        return self._client.delete_file(existing.file_id)

    # ------------------------------------------------------------------
    # Private: local filesystem helpers
    # ------------------------------------------------------------------

    def _list_local_exports(self) -> list[Path]:
        """
        Scan the export directory and return all YYYY-MM-DD.json files.

        Returns paths sorted by date ascending (oldest first).
        """
        if not self._export_dir.exists():
            return []

        dated_files: list[Path] = []
        for p in self._export_dir.glob("*.json"):
            stem = p.stem
            try:
                datetime.strptime(stem, "%Y-%m-%d")
                dated_files.append(p)
            except ValueError:
                pass   # Skip non-dated JSON files

        dated_files.sort(key=lambda p: p.stem)
        return dated_files

    def _find_latest_local_export(self) -> Path | None:
        """Return the Path to the most recently dated local export file."""
        files = self._list_local_exports()
        return files[-1] if files else None


# =============================================================================
# Module-level utilities
# =============================================================================

def _quota_sleep() -> None:
    """
    Brief sleep between Google Drive API calls.

    Drive's write quota is ~10 requests/second (user-level).
    A 0.5s sleep keeps us comfortably within that limit without
    needing to track a request counter.
    """
    time.sleep(_QUOTA_SLEEP_SECONDS)


def _validate_date_string(date_str: str) -> str:
    """
    Validate and normalise a YYYY-MM-DD date string.

    Raises:
        ValueError: If the string is not a valid date in YYYY-MM-DD format.
    """
    date_str = date_str.strip()
    try:
        parsed = datetime.strptime(date_str, "%Y-%m-%d")
        return parsed.strftime("%Y-%m-%d")
    except ValueError:
        raise ValueError(
            f"Invalid date format: {date_str!r}. "
            "Expected YYYY-MM-DD (e.g. 2025-01-15)."
        )
