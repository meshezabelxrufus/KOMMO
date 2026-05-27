"""
tests/test_google_sheets.py
===========================
Unit tests for the Milestone 2 Google Sheets integration layer.

Test coverage:
  - WorksheetConfig validation
  - _extract_field (dotted path extraction)
  - _coerce_cell (type coercion)
  - _records_to_rows (full 2-D table conversion)
  - SheetsSyncResult (Pydantic model)
  - GoogleSheetsConfigError (missing env vars)
  - SheetsWriter (mocked gspread client)

Run with:
    source .venv/bin/activate
    pytest tests/test_google_sheets.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from integrations.google_sheets import (
    GoogleSheetsAuthError,
    GoogleSheetsClient,
    GoogleSheetsConfigError,
    GoogleSheetsError,
    GoogleSheetsWriteError,
    LEADS_WORKSHEET_CONFIG,
    MESSAGES_WORKSHEET_CONFIG,
    DAILY_SUMMARY_WORKSHEET_CONFIG,
    SheetsSyncResult,
    SheetsWriter,
    WorksheetConfig,
    _coerce_cell,
    _extract_field,
    _records_to_rows,
    load_json_output,
)


# =============================================================================
# WorksheetConfig
# =============================================================================

class TestWorksheetConfig:

    def test_valid_config(self):
        cfg = WorksheetConfig(
            name="TestSheet",
            headers=["ID", "Name"],
            fields=["id", "name"],
        )
        assert cfg.name == "TestSheet"
        assert len(cfg.headers) == len(cfg.fields) == 2

    def test_name_stripped(self):
        cfg = WorksheetConfig(name="  Leads  ", headers=["x"], fields=["x"])
        assert cfg.name == "Leads"

    def test_mismatched_headers_fields_raises(self):
        with pytest.raises(ValueError, match="same length"):
            WorksheetConfig(
                name="Bad",
                headers=["A", "B"],
                fields=["a"],
            )

    def test_empty_headers_raises(self):
        with pytest.raises(Exception):
            WorksheetConfig(name="X", headers=[], fields=[])

    def test_predefined_leads_config_valid(self):
        # The pre-built config must be self-consistent
        assert len(LEADS_WORKSHEET_CONFIG.headers) == len(LEADS_WORKSHEET_CONFIG.fields)
        assert LEADS_WORKSHEET_CONFIG.name == "Leads"

    def test_predefined_messages_config_valid(self):
        assert len(MESSAGES_WORKSHEET_CONFIG.headers) == len(MESSAGES_WORKSHEET_CONFIG.fields)
        assert MESSAGES_WORKSHEET_CONFIG.name == "Messages"

    def test_predefined_daily_summary_config_valid(self):
        assert len(DAILY_SUMMARY_WORKSHEET_CONFIG.headers) == len(DAILY_SUMMARY_WORKSHEET_CONFIG.fields)
        assert DAILY_SUMMARY_WORKSHEET_CONFIG.name == "Daily_Summary"


# =============================================================================
# _extract_field
# =============================================================================

class TestExtractField:

    def test_top_level_key(self):
        assert _extract_field({"id": 42}, "id") == 42

    def test_dotted_path(self):
        assert _extract_field({"result": {"text": "ok"}}, "result.text") == "ok"

    def test_missing_top_level_returns_none(self):
        assert _extract_field({}, "nonexistent") is None

    def test_missing_nested_key_returns_none(self):
        assert _extract_field({"a": {}}, "a.b") is None

    def test_none_mid_path_returns_none(self):
        assert _extract_field({"a": None}, "a.b") is None

    def test_deeply_nested(self):
        data = {"a": {"b": {"c": "deep"}}}
        assert _extract_field(data, "a.b.c") == "deep"

    def test_none_value_returned(self):
        # Explicit None value (not missing key)
        assert _extract_field({"x": None}, "x") is None

    def test_int_value(self):
        assert _extract_field({"n": 100}, "n") == 100

    def test_bool_value(self):
        assert _extract_field({"flag": True}, "flag") is True

    def test_non_dict_mid_path_returns_none(self):
        # "a" is a string, not a dict
        assert _extract_field({"a": "hello"}, "a.b") is None


# =============================================================================
# _coerce_cell
# =============================================================================

class TestCoecrceCell:

    def test_none_becomes_empty_string(self):
        assert _coerce_cell(None) == ""

    def test_string_stripped(self):
        assert _coerce_cell("  hello  ") == "hello"

    def test_empty_string(self):
        assert _coerce_cell("") == ""

    def test_int_preserved(self):
        assert _coerce_cell(42) == 42

    def test_float_preserved(self):
        assert _coerce_cell(3.14) == 3.14

    def test_bool_true_preserved(self):
        assert _coerce_cell(True) is True

    def test_bool_false_preserved(self):
        assert _coerce_cell(False) is False

    def test_dict_json_encoded(self):
        result = _coerce_cell({"key": "val"})
        assert isinstance(result, str)
        assert '"key"' in result
        assert '"val"' in result

    def test_list_json_encoded(self):
        result = _coerce_cell([1, 2, 3])
        assert isinstance(result, str)
        assert "1" in result

    def test_datetime_iso_string(self):
        dt = datetime(2025, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        result = _coerce_cell(dt)
        assert "2025-01-15" in result

    def test_zero_preserved_not_empty(self):
        assert _coerce_cell(0) == 0

    def test_false_not_treated_as_none(self):
        # bool is a subclass of int — False should not become ""
        assert _coerce_cell(False) is False


# =============================================================================
# _records_to_rows
# =============================================================================

class TestRecordsToRows:

    _config = WorksheetConfig(
        name="Test",
        headers=["ID", "Name", "Status"],
        fields=["id", "name", "status_id"],
    )

    def test_empty_records_returns_header_only(self):
        rows = _records_to_rows([], self._config)
        assert rows == [["ID", "Name", "Status"]]

    def test_single_record(self):
        records = [{"id": 1, "name": "Lead A", "status_id": 10}]
        rows = _records_to_rows(records, self._config)
        assert len(rows) == 2
        assert rows[0] == ["ID", "Name", "Status"]
        assert rows[1] == [1, "Lead A", 10]

    def test_missing_field_becomes_empty_string(self):
        records = [{"id": 99}]   # name and status_id missing
        rows = _records_to_rows(records, self._config)
        assert rows[1] == [99, "", ""]

    def test_multiple_records(self):
        records = [
            {"id": 1, "name": "A", "status_id": 100},
            {"id": 2, "name": "B", "status_id": 200},
        ]
        rows = _records_to_rows(records, self._config)
        assert len(rows) == 3  # header + 2 data rows

    def test_dotted_field_extraction(self):
        cfg = WorksheetConfig(
            name="Deep",
            headers=["Result Text"],
            fields=["result.text"],
        )
        records = [{"result": {"text": "NC"}}]
        rows = _records_to_rows(records, cfg)
        assert rows[1] == ["NC"]

    def test_header_is_always_first_row(self):
        records = [{"id": 1, "name": "X", "status_id": 0}]
        rows = _records_to_rows(records, self._config)
        assert rows[0] == list(self._config.headers)

    def test_leads_config_produces_correct_column_count(self):
        """Integration: the pre-built Leads config maps to 16 columns."""
        records = [{
            "id": 1, "name": "Test Lead", "pipeline_id": 7, "status_id": 1,
            "responsible_user_id": 10, "group_id": 0, "price": 500,
            "loss_reason_id": None, "is_deleted": False, "score": None,
            "account_id": 31959059, "created_at_iso": "2025-01-15T10:00:00+00:00",
            "updated_at_iso": "2025-01-15T11:00:00+00:00", "closed_at_iso": None,
            "tags": None, "custom_fields_values": [{"field_id": 1, "value": "x"}],
        }]
        rows = _records_to_rows(records, LEADS_WORKSHEET_CONFIG)
        assert len(rows[0]) == 16   # header columns
        assert len(rows[1]) == 16   # data row columns


# =============================================================================
# SheetsSyncResult
# =============================================================================

class TestSheetsSyncResult:

    def test_successful_result(self):
        r = SheetsSyncResult(
            worksheet_name="Leads",
            rows_written=100,
            columns_written=16,
            duration_s=5.2,
            spreadsheet_url="https://docs.google.com/spreadsheets/d/abc",
            success=True,
        )
        assert r.success is True
        assert r.status_icon == "✅"
        assert r.error is None
        assert "100" in str(r)

    def test_failed_result(self):
        r = SheetsSyncResult(
            worksheet_name="Messages",
            success=False,
            error="quota exceeded",
        )
        assert r.success is False
        assert r.status_icon == "❌"
        assert "FAILED" in str(r)
        assert "quota exceeded" in str(r)

    def test_written_at_auto_populated(self):
        r = SheetsSyncResult(worksheet_name="X")
        assert r.written_at is not None
        assert "T" in r.written_at   # ISO format


# =============================================================================
# GoogleSheetsClient — env var validation (no real HTTP calls)
# =============================================================================

class TestGoogleSheetsClientFromEnv:

    def test_missing_both_vars_raises_config_error(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)
        monkeypatch.delenv("GOOGLE_SHEETS_SPREADSHEET_ID", raising=False)
        with pytest.raises(GoogleSheetsConfigError, match="Missing required environment variable"):
            GoogleSheetsClient.from_env()

    def test_missing_credentials_file_raises_config_error(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)
        monkeypatch.setenv("GOOGLE_SHEETS_SPREADSHEET_ID", "abc123")
        with pytest.raises(GoogleSheetsConfigError):
            GoogleSheetsClient.from_env()

    def test_missing_spreadsheet_id_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", "/tmp/creds.json")
        monkeypatch.delenv("GOOGLE_SHEETS_SPREADSHEET_ID", raising=False)
        with pytest.raises(GoogleSheetsConfigError):
            GoogleSheetsClient.from_env()

    def test_credentials_file_not_found_raises_auth_error(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", "/nonexistent/path/creds.json")
        monkeypatch.setenv("GOOGLE_SHEETS_SPREADSHEET_ID", "abc123")
        with pytest.raises(GoogleSheetsAuthError, match="not found"):
            GoogleSheetsClient.from_env()

    def test_empty_spreadsheet_id_raises_config_error(self, monkeypatch, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text("{}")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(creds))
        monkeypatch.setenv("GOOGLE_SHEETS_SPREADSHEET_ID", "   ")
        with pytest.raises(GoogleSheetsConfigError, match="Missing required"):
            GoogleSheetsClient.from_env()


# =============================================================================
# load_json_output
# =============================================================================

class TestLoadJsonOutput:

    def test_loads_valid_json(self, tmp_path):
        f = tmp_path / "leads.json"
        payload = {"_meta": {"count": 2}, "data": [{"id": 1}, {"id": 2}]}
        f.write_text(json.dumps(payload))
        result = load_json_output(f)
        assert result["_meta"]["count"] == 2
        assert len(result["data"]) == 2

    def test_file_not_found_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_json_output(tmp_path / "nonexistent.json")

    def test_empty_file_raises(self, tmp_path):
        f = tmp_path / "empty.json"
        f.write_text("")
        with pytest.raises(ValueError, match="empty"):
            load_json_output(f)

    def test_invalid_json_raises(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("not json {{{")
        with pytest.raises(json.JSONDecodeError):
            load_json_output(f)


# =============================================================================
# SheetsWriter — unit tests with mocked GoogleSheetsClient
# =============================================================================

class TestSheetsWriter:
    """Unit tests for SheetsWriter using a fully mocked GoogleSheetsClient."""

    def _make_mock_client(self) -> MagicMock:
        """Build a mock GoogleSheetsClient with the minimal interface."""
        client = MagicMock(spec=GoogleSheetsClient)
        client.spreadsheet_id  = "mock-spreadsheet-id"
        client.spreadsheet_url = "https://docs.google.com/spreadsheets/d/mock-spreadsheet-id/edit"
        client.spreadsheet_title = "Mock Spreadsheet"
        ws = MagicMock()
        ws.title = "Leads"
        client.get_or_create_worksheet.return_value = ws
        client.batch_write.return_value = None
        client.format_header_row.return_value = None
        return client

    def test_write_leads_success(self):
        client = self._make_mock_client()
        writer = SheetsWriter(client)
        records = [
            {"id": 1, "name": "Lead A", "pipeline_id": 7, "status_id": 1,
             "responsible_user_id": 10, "group_id": 0, "price": 0,
             "loss_reason_id": None, "is_deleted": False, "score": None,
             "account_id": 31959059, "created_at_iso": "2025-01-01T00:00:00+00:00",
             "updated_at_iso": "2025-01-01T00:00:00+00:00", "closed_at_iso": None,
             "tags": None, "custom_fields_values": None},
        ]
        result = writer.write_leads(records)

        assert result.success is True
        assert result.worksheet_name == "Leads"
        assert result.rows_written == 1
        assert result.columns_written == 16
        client.batch_write.assert_called_once()

    def test_write_leads_empty_records(self):
        """Empty records should write header row only — not fail."""
        client = self._make_mock_client()
        writer = SheetsWriter(client)
        result = writer.write_leads([])

        assert result.success is True
        assert result.rows_written == 0   # 0 data rows (header not counted)

    def test_write_messages_success(self):
        client = self._make_mock_client()
        ws = client.get_or_create_worksheet.return_value
        ws.title = "Messages"
        writer = SheetsWriter(client)

        records = [
            {
                "id": "msg-1", "chat_id": "chat-1", "lead_id": 42,
                "direction": "inbound", "type": "text",
                "author": {"id": "user-1", "type": "user"},
                "text": "Hello", "timestamp_iso": "2025-01-01T12:00:00+00:00",
                "created_at": 1700000000, "media_url": None,
                "origin": "whatsapp", "chat_created_at_iso": "2025-01-01T00:00:00+00:00",
                "contact_id": 55, "responsible_user_id": 10,
            }
        ]
        result = writer.write_messages(records)

        assert result.success is True
        assert result.rows_written == 1
        assert result.columns_written == 15

    def test_write_leads_api_error_returns_failed_result(self):
        """If batch_write raises, write_leads should return a failed result (not raise)."""
        import gspread.exceptions
        client = self._make_mock_client()
        client.batch_write.side_effect = GoogleSheetsWriteError(
            "API quota exceeded", worksheet="Leads"
        )
        writer = SheetsWriter(client)
        result = writer.write_leads([{"id": 1, "name": "x"}])

        assert result.success is False
        assert "quota" in (result.error or "").lower()

    def test_write_worksheet_calls_format_header(self):
        """format_header_row must be called after every successful write."""
        client = self._make_mock_client()
        writer = SheetsWriter(client)
        writer.write_leads([])
        client.format_header_row.assert_called_once()

    def test_write_worksheet_calls_get_or_create(self):
        """get_or_create_worksheet must be called with the correct tab name."""
        client = self._make_mock_client()
        writer = SheetsWriter(client)
        writer.write_leads([])
        client.get_or_create_worksheet.assert_called_once_with("Leads")
