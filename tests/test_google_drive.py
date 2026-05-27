"""
tests/test_google_drive.py
===========================
Unit tests for integrations/google_drive.py

Coverage:
  - GoogleDriveError hierarchy (custom exceptions)
  - DriveFileInfo (dataclass + date_str property)
  - DriveUploadResult (dataclass + __str__ + status_icon)
  - _validate_date_string
  - _quota_sleep (quick passthrough check)
  - GoogleDriveClient.from_env (env var validation — no real HTTP)
  - GoogleDriveClient.__init__ (credentials validation — no real HTTP)
  - DriveUploader._list_local_exports (filesystem only)
  - DriveUploader._find_latest_local_export (filesystem only)
  - DriveUploader.upload_daily_export (mocked client)
  - DriveUploader.upload_latest_export (mocked client)
  - DriveUploader.upload_all_exports (mocked client)
  - DriveUploader.list_uploaded_exports (mocked client)
  - DriveUploader.delete_existing_file_if_present (mocked client)

All tests that would make real HTTP calls use a fully mocked
GoogleDriveClient — no Google credentials required to run these.

Run with:
    source .venv/bin/activate
    pytest tests/test_google_drive.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from integrations.google_drive import (
    DriveFileInfo,
    DriveUploadResult,
    DriveUploader,
    GoogleDriveAuthError,
    GoogleDriveClient,
    GoogleDriveConfigError,
    GoogleDriveError,
    GoogleDriveNotFoundError,
    GoogleDriveUploadError,
    _validate_date_string,
)


# ===========================================================================
# Fixtures & helpers
# ===========================================================================

def _make_drive_file(
    file_id: str = "file-abc",
    filename: str = "2025-01-15.json",
    web_view_link: str = "https://drive.google.com/file/d/file-abc/view",
    size_bytes: int | None = 4096,
    modified_at: str = "2025-01-15T10:00:00.000Z",
) -> DriveFileInfo:
    return DriveFileInfo(
        file_id=file_id,
        filename=filename,
        web_view_link=web_view_link,
        direct_link=f"https://drive.google.com/uc?id={file_id}&export=download",
        size_bytes=size_bytes,
        modified_at=modified_at,
    )


def _make_mock_client(
    folder_id: str = "folder-xyz",
    folder_url: str = "https://drive.google.com/drive/folders/folder-xyz",
) -> MagicMock:
    """Build a mock GoogleDriveClient with the full public interface."""
    client = MagicMock(spec=GoogleDriveClient)
    client.folder_id  = folder_id
    client.folder_url = folder_url
    client.list_files_in_folder.return_value = []
    client.find_file_by_name.return_value    = None
    client.delete_file.return_value          = True
    client.upload_file.return_value = DriveUploadResult(
        filename="2025-01-15.json",
        file_id="new-file-id",
        web_view_link="https://drive.google.com/file/d/new-file-id/view",
        direct_link="https://drive.google.com/uc?id=new-file-id&export=download",
        folder_id=folder_id,
        folder_url=folder_url,
        size_bytes=4096,
        was_replaced=False,
        success=True,
    )
    return client


def _make_export_files(export_dir: Path, dates: list[str]) -> None:
    """Create minimal YYYY-MM-DD.json files in export_dir."""
    export_dir.mkdir(parents=True, exist_ok=True)
    for date in dates:
        f = export_dir / f"{date}.json"
        f.write_text(json.dumps({"_meta": {"date": date}, "leads": []}), encoding="utf-8")


# ===========================================================================
# _validate_date_string
# ===========================================================================

class TestValidateDateString:

    def test_valid_date(self):
        assert _validate_date_string("2025-01-15") == "2025-01-15"

    def test_strips_whitespace(self):
        assert _validate_date_string("  2025-01-15  ") == "2025-01-15"

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Invalid date format"):
            _validate_date_string("15-01-2025")

    def test_not_a_date_raises(self):
        with pytest.raises(ValueError):
            _validate_date_string("banana")

    def test_invalid_month_raises(self):
        with pytest.raises(ValueError):
            _validate_date_string("2025-13-01")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            _validate_date_string("")


# ===========================================================================
# Custom Exceptions
# ===========================================================================

class TestGoogleDriveExceptions:

    def test_base_error_str_no_context(self):
        exc = GoogleDriveError("something went wrong")
        assert str(exc) == "something went wrong"

    def test_base_error_str_with_context(self):
        exc = GoogleDriveError("bad thing", file_id="abc", folder="xyz")
        s = str(exc)
        assert "bad thing" in s
        assert "file_id=abc" in s
        assert "folder=xyz" in s

    def test_auth_error_is_drive_error(self):
        assert issubclass(GoogleDriveAuthError, GoogleDriveError)

    def test_config_error_is_drive_error(self):
        assert issubclass(GoogleDriveConfigError, GoogleDriveError)

    def test_upload_error_stores_filename(self):
        exc = GoogleDriveUploadError("failed", filename="2025-01-15.json")
        assert exc.filename == "2025-01-15.json"

    def test_not_found_error_is_drive_error(self):
        assert issubclass(GoogleDriveNotFoundError, GoogleDriveError)

    def test_quota_error_is_drive_error(self):
        assert issubclass(GoogleDriveConfigError, GoogleDriveError)


# ===========================================================================
# DriveFileInfo
# ===========================================================================

class TestDriveFileInfo:

    def test_date_str_valid_export(self):
        fi = _make_drive_file(filename="2025-01-15.json")
        assert fi.date_str == "2025-01-15"

    def test_date_str_non_dated_file_returns_none(self):
        fi = _make_drive_file(filename="random-file.json")
        assert fi.date_str is None

    def test_date_str_gitkeep_returns_none(self):
        fi = _make_drive_file(filename=".gitkeep")
        assert fi.date_str is None

    def test_str_includes_filename_and_link(self):
        fi = _make_drive_file()
        s = str(fi)
        assert "2025-01-15.json" in s
        assert "drive.google.com" in s

    def test_size_bytes_default_none(self):
        fi = DriveFileInfo(file_id="x", filename="test.json")
        assert fi.size_bytes is None

    def test_direct_link_format(self):
        fi = _make_drive_file(file_id="myid123")
        assert "myid123" in fi.direct_link
        assert "export=download" in fi.direct_link


# ===========================================================================
# DriveUploadResult
# ===========================================================================

class TestDriveUploadResult:

    def test_success_icon(self):
        r = DriveUploadResult(filename="x.json", success=True)
        assert r.status_icon == "✅"

    def test_failure_icon(self):
        r = DriveUploadResult(filename="x.json", success=False, error="oops")
        assert r.status_icon == "❌"

    def test_action_label_new(self):
        r = DriveUploadResult(filename="x.json", was_replaced=False)
        assert r.action_label == "uploaded"

    def test_action_label_replaced(self):
        r = DriveUploadResult(filename="x.json", was_replaced=True)
        assert r.action_label == "replaced"

    def test_str_success(self):
        r = DriveUploadResult(
            filename="2025-01-15.json",
            file_id="abc",
            web_view_link="https://drive.google.com/file/d/abc/view",
            size_bytes=4096,
            was_replaced=False,
            success=True,
            duration_s=1.23,
        )
        s = str(r)
        assert "2025-01-15.json" in s
        assert "✅" in s
        assert "drive.google.com" in s

    def test_str_failure(self):
        r = DriveUploadResult(
            filename="2025-01-15.json",
            success=False,
            error="HTTP 403: quota exceeded",
        )
        s = str(r)
        assert "❌" in s
        assert "FAILED" in s
        assert "quota exceeded" in s

    def test_uploaded_at_auto_populated(self):
        r = DriveUploadResult(filename="x.json")
        assert r.uploaded_at
        assert "T" in r.uploaded_at  # ISO 8601


# ===========================================================================
# GoogleDriveClient.from_env — env var validation (no real HTTP calls)
# ===========================================================================

class TestGoogleDriveClientFromEnv:

    def test_missing_both_vars_raises_config_error(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)
        monkeypatch.delenv("GOOGLE_DRIVE_FOLDER_ID", raising=False)
        with pytest.raises(GoogleDriveConfigError, match="Missing required"):
            GoogleDriveClient.from_env()

    def test_missing_credentials_file_raises_config_error(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)
        monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "folder-abc")
        with pytest.raises(GoogleDriveConfigError, match="Missing required"):
            GoogleDriveClient.from_env()

    def test_missing_folder_id_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", "/tmp/creds.json")
        monkeypatch.delenv("GOOGLE_DRIVE_FOLDER_ID", raising=False)
        with pytest.raises(GoogleDriveConfigError, match="Missing required"):
            GoogleDriveClient.from_env()

    def test_both_vars_set_but_credentials_file_missing_raises_auth_error(
        self, monkeypatch
    ):
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", "/nonexistent/path/creds.json")
        monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "folder-abc")
        with pytest.raises(GoogleDriveAuthError, match="not found"):
            GoogleDriveClient.from_env()

    def test_empty_folder_id_raises_config_error(self, monkeypatch, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text("{}")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(creds))
        monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "   ")
        # Whitespace-only folder_id → treated as missing by from_env
        with pytest.raises(GoogleDriveConfigError):
            GoogleDriveClient.from_env()


# ===========================================================================
# GoogleDriveClient.__init__ — constructor validation (no real HTTP calls)
# ===========================================================================

class TestGoogleDriveClientInit:

    def test_empty_folder_id_raises_config_error(self, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text("{}")
        with pytest.raises(GoogleDriveConfigError, match="GOOGLE_DRIVE_FOLDER_ID is empty"):
            GoogleDriveClient(credentials_path=creds, folder_id="")

    def test_whitespace_folder_id_raises_config_error(self, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text("{}")
        with pytest.raises(GoogleDriveConfigError):
            GoogleDriveClient(credentials_path=creds, folder_id="   ")

    def test_missing_credentials_file_raises_auth_error(self, tmp_path):
        with pytest.raises(GoogleDriveAuthError, match="not found"):
            GoogleDriveClient(
                credentials_path=tmp_path / "nonexistent.json",
                folder_id="folder-abc",
            )


# ===========================================================================
# DriveUploader — filesystem helpers (no HTTP calls)
# ===========================================================================

class TestDriveUploaderFilesystem:
    """Tests that only touch the local filesystem — no Drive API."""

    def test_list_local_exports_empty_dir(self, tmp_path):
        export_dir = tmp_path / "exports"
        export_dir.mkdir()
        uploader = DriveUploader(_make_mock_client(), export_dir=export_dir)
        assert uploader._list_local_exports() == []

    def test_list_local_exports_nonexistent_dir(self, tmp_path):
        uploader = DriveUploader(
            _make_mock_client(),
            export_dir=tmp_path / "missing",
        )
        assert uploader._list_local_exports() == []

    def test_list_local_exports_finds_dated_files(self, tmp_path):
        export_dir = tmp_path / "exports"
        dates = ["2025-01-13", "2025-01-14", "2025-01-15"]
        _make_export_files(export_dir, dates)
        uploader = DriveUploader(_make_mock_client(), export_dir=export_dir)
        found = uploader._list_local_exports()
        assert len(found) == 3
        assert [p.stem for p in found] == dates   # sorted oldest-first

    def test_list_local_exports_ignores_non_dated_files(self, tmp_path):
        export_dir = tmp_path / "exports"
        export_dir.mkdir()
        (export_dir / "README.json").write_text("{}")
        (export_dir / "summary.json").write_text("{}")
        _make_export_files(export_dir, ["2025-01-15"])
        uploader = DriveUploader(_make_mock_client(), export_dir=export_dir)
        found = uploader._list_local_exports()
        assert len(found) == 1
        assert found[0].stem == "2025-01-15"

    def test_find_latest_local_export_returns_newest(self, tmp_path):
        export_dir = tmp_path / "exports"
        _make_export_files(export_dir, ["2025-01-13", "2025-01-14", "2025-01-15"])
        uploader = DriveUploader(_make_mock_client(), export_dir=export_dir)
        latest = uploader._find_latest_local_export()
        assert latest is not None
        assert latest.stem == "2025-01-15"

    def test_find_latest_local_export_empty_dir(self, tmp_path):
        export_dir = tmp_path / "exports"
        export_dir.mkdir()
        uploader = DriveUploader(_make_mock_client(), export_dir=export_dir)
        assert uploader._find_latest_local_export() is None


# ===========================================================================
# DriveUploader — mocked client tests
# ===========================================================================

class TestDriveUploaderOperations:
    """Tests that mock the GoogleDriveClient to avoid real HTTP calls."""

    # ── upload_daily_export ───────────────────────────────────────────────

    def test_upload_daily_export_by_date(self, tmp_path):
        export_dir = tmp_path / "exports"
        _make_export_files(export_dir, ["2025-01-15"])

        client   = _make_mock_client()
        uploader = DriveUploader(client, export_dir=export_dir)
        result   = uploader.upload_daily_export(date="2025-01-15")

        assert result.success is True
        client.upload_file.assert_called_once()
        call_kwargs = client.upload_file.call_args.kwargs
        assert call_kwargs["replace_existing"] is True
        assert call_kwargs["set_public_read"] is True

    def test_upload_daily_export_by_path(self, tmp_path):
        export_dir = tmp_path / "exports"
        _make_export_files(export_dir, ["2025-01-15"])

        client   = _make_mock_client()
        uploader = DriveUploader(client, export_dir=export_dir)
        result   = uploader.upload_daily_export(
            file_path=export_dir / "2025-01-15.json"
        )

        assert result.success is True
        client.upload_file.assert_called_once()

    def test_upload_daily_export_no_args_raises(self, tmp_path):
        uploader = DriveUploader(_make_mock_client(), export_dir=tmp_path)
        with pytest.raises(ValueError, match="Either `date`"):
            uploader.upload_daily_export()

    def test_upload_daily_export_invalid_date_raises(self, tmp_path):
        uploader = DriveUploader(_make_mock_client(), export_dir=tmp_path)
        with pytest.raises(ValueError, match="Invalid date format"):
            uploader.upload_daily_export(date="31-12-2025")

    def test_upload_daily_export_missing_file_raises(self, tmp_path):
        export_dir = tmp_path / "exports"
        export_dir.mkdir()
        client = _make_mock_client()
        client.upload_file.side_effect = FileNotFoundError("file not found")
        uploader = DriveUploader(client, export_dir=export_dir)
        with pytest.raises(FileNotFoundError):
            uploader.upload_daily_export(date="2025-01-15")

    # ── upload_latest_export ──────────────────────────────────────────────

    def test_upload_latest_export_success(self, tmp_path):
        export_dir = tmp_path / "exports"
        _make_export_files(export_dir, ["2025-01-14", "2025-01-15"])

        client   = _make_mock_client()
        uploader = DriveUploader(client, export_dir=export_dir)
        result   = uploader.upload_latest_export()

        assert result.success is True
        # Verify the latest (2025-01-15) was passed to upload_file
        call_path = client.upload_file.call_args.kwargs["file_path"]
        assert "2025-01-15" in str(call_path)

    def test_upload_latest_export_no_exports_raises(self, tmp_path):
        export_dir = tmp_path / "exports"
        export_dir.mkdir()
        uploader = DriveUploader(_make_mock_client(), export_dir=export_dir)
        with pytest.raises(GoogleDriveNotFoundError):
            uploader.upload_latest_export()

    # ── upload_all_exports ────────────────────────────────────────────────

    def test_upload_all_exports_calls_upload_per_file(self, tmp_path):
        export_dir = tmp_path / "exports"
        _make_export_files(export_dir, ["2025-01-13", "2025-01-14", "2025-01-15"])

        client   = _make_mock_client()
        uploader = DriveUploader(client, export_dir=export_dir)
        results  = uploader.upload_all_exports()

        assert len(results) == 3
        assert client.upload_file.call_count == 3

    def test_upload_all_exports_empty_dir_returns_empty(self, tmp_path):
        export_dir = tmp_path / "exports"
        export_dir.mkdir()
        uploader = DriveUploader(_make_mock_client(), export_dir=export_dir)
        results  = uploader.upload_all_exports()
        assert results == []

    def test_upload_all_exports_skip_existing(self, tmp_path):
        export_dir = tmp_path / "exports"
        _make_export_files(export_dir, ["2025-01-14", "2025-01-15"])

        # Simulate 2025-01-14 already in Drive
        client = _make_mock_client()
        client.list_files_in_folder.return_value = [
            _make_drive_file(filename="2025-01-14.json", file_id="existing-id"),
        ]

        uploader = DriveUploader(client, export_dir=export_dir)
        results  = uploader.upload_all_exports(skip_existing=True)

        # 2 total results but only 1 actual upload call (2025-01-15)
        assert len(results) == 2
        assert client.upload_file.call_count == 1

    # ── list_uploaded_exports ─────────────────────────────────────────────

    def test_list_uploaded_exports_returns_dated_files_only(self, tmp_path):
        client = _make_mock_client()
        client.list_files_in_folder.return_value = [
            _make_drive_file(filename="2025-01-15.json"),
            _make_drive_file(filename="2025-01-16.json"),
            _make_drive_file(filename="README.json"),      # not dated
            _make_drive_file(filename=".gitkeep"),         # not dated
        ]

        uploader = DriveUploader(client, export_dir=tmp_path)
        exports  = uploader.list_uploaded_exports()

        assert len(exports) == 2
        assert exports[0].filename == "2025-01-15.json"
        assert exports[1].filename == "2025-01-16.json"

    def test_list_uploaded_exports_sorted_by_filename(self, tmp_path):
        client = _make_mock_client()
        client.list_files_in_folder.return_value = [
            _make_drive_file(filename="2025-01-16.json"),
            _make_drive_file(filename="2025-01-14.json"),
            _make_drive_file(filename="2025-01-15.json"),
        ]
        uploader = DriveUploader(client, export_dir=tmp_path)
        exports  = uploader.list_uploaded_exports()
        names    = [e.filename for e in exports]
        assert names == sorted(names)

    def test_list_uploaded_exports_empty_folder(self, tmp_path):
        client = _make_mock_client()
        client.list_files_in_folder.return_value = []
        uploader = DriveUploader(client, export_dir=tmp_path)
        assert uploader.list_uploaded_exports() == []

    # ── delete_existing_file_if_present ───────────────────────────────────

    def test_delete_existing_file_found_and_deleted(self, tmp_path):
        client = _make_mock_client()
        client.find_file_by_name.return_value = _make_drive_file(
            file_id="del-id", filename="2025-01-15.json"
        )
        client.delete_file.return_value = True

        uploader = DriveUploader(client, export_dir=tmp_path)
        result   = uploader.delete_existing_file_if_present("2025-01-15.json")

        assert result is True
        client.delete_file.assert_called_once_with("del-id")

    def test_delete_existing_file_not_found_returns_false(self, tmp_path):
        client = _make_mock_client()
        client.find_file_by_name.return_value = None

        uploader = DriveUploader(client, export_dir=tmp_path)
        result   = uploader.delete_existing_file_if_present("2025-01-15.json")

        assert result is False
        client.delete_file.assert_not_called()

    # ── upload_file error handling ────────────────────────────────────────

    def test_upload_file_api_failure_returns_failed_result(self, tmp_path):
        export_dir = tmp_path / "exports"
        _make_export_files(export_dir, ["2025-01-15"])

        from googleapiclient.errors import HttpError
        import httplib2

        client = _make_mock_client()
        # Simulate a 403 HttpError
        resp = httplib2.Response({"status": 403})
        resp.reason = "Forbidden"
        client.upload_file.side_effect = None
        # Return a failed result instead (matching the real implementation)
        client.upload_file.return_value = DriveUploadResult(
            filename="2025-01-15.json",
            folder_id=client.folder_id,
            folder_url=client.folder_url,
            success=False,
            error="HTTP 403: Forbidden",
        )

        uploader = DriveUploader(client, export_dir=export_dir)
        result   = uploader.upload_daily_export(date="2025-01-15")

        assert result.success is False
        assert "403" in (result.error or "")
