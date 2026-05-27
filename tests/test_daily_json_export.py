"""
tests/test_daily_json_export.py
================================
Unit and integration tests for normalizers/daily_json_export.py

Coverage:
  - MessageItem  (Pydantic model + field mapping)
  - _message_date_utc  (timestamp extraction)
  - _lead_key          (lead ID bucketing)
  - _ts_to_iso         (timestamp formatting)
  - _validate_date_string
  - DailyExportGenerator.list_available_dates
  - DailyExportGenerator.export_for_date
  - DailyExportGenerator.export_latest_day
  - DailyExportGenerator.generate_all
  - ExportResult (dataclass)
  - generate_daily_export (convenience function)
  - Error cases: missing input, empty file, bad date, no messages for date

Run with:
    source .venv/bin/activate
    pytest tests/test_daily_json_export.py -v
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from normalizers.daily_json_export import (
    DailyExportDateError,
    DailyExportGenerator,
    DailyExportInputError,
    ExportResult,
    MessageItem,
    _lead_key,
    _message_date_utc,
    _ts_to_iso,
    _validate_date_string,
    generate_daily_export,
)


# ===========================================================================
# Fixtures — shared test data
# ===========================================================================

def _make_flat_message(
    message_id: str = "msg-1",
    lead_id: int | None = 42,
    direction: str = "in",
    timestamp: int = 1736935920,   # 2025-01-15T09:12:00Z
    text: str = "Hello",
    channel: str = "WhatsApp",
    author: str | dict | None = "John",
    author_type: str | None = "contact",
    lead_name: str | None = "Lead A",
    contact_name: str | None = "John Smith",
) -> dict:
    """Build a minimal flat message dict matching messages_flat.json schema."""
    return {
        "message_id":   message_id,
        "lead_id":      lead_id,
        "lead_name":    lead_name,
        "contact_name": contact_name,
        "channel":      channel,
        "direction":    direction,
        "author":       author,
        "author_type":  author_type,
        "message_text": text,
        "timestamp":    timestamp,
        "timestamp_iso": _ts_to_iso(timestamp),
        "media_url":    None,
        "channel_raw":  "whatsapp",
    }


def _make_messages_file(tmp_path: Path, messages: list[dict]) -> Path:
    """Write a messages_flat.json file to tmp_path and return its path."""
    f = tmp_path / "messages_flat.json"
    payload = {
        "_meta": {
            "entity": "messages",
            "count": len(messages),
            "extracted_at": datetime.now(tz=timezone.utc).isoformat(),
        },
        "messages": messages,
    }
    f.write_text(json.dumps(payload), encoding="utf-8")
    return f


# ===========================================================================
# _ts_to_iso
# ===========================================================================

class TestTsToIso:

    def test_known_timestamp(self):
        result = _ts_to_iso(1736935920)
        assert "2025-01-15" in result
        assert "T" in result

    def test_is_utc(self):
        result = _ts_to_iso(0)
        assert "1970-01-01" in result

    def test_returns_string(self):
        assert isinstance(_ts_to_iso(1700000000), str)


# ===========================================================================
# _validate_date_string
# ===========================================================================

class TestValidateDateString:

    def test_valid_date(self):
        assert _validate_date_string("2025-01-15") == "2025-01-15"

    def test_strips_whitespace(self):
        assert _validate_date_string("  2025-01-15  ") == "2025-01-15"

    def test_invalid_format_raises(self):
        with pytest.raises(DailyExportDateError):
            _validate_date_string("15-01-2025")

    def test_not_a_date_raises(self):
        with pytest.raises(DailyExportDateError):
            _validate_date_string("banana")

    def test_invalid_month_raises(self):
        with pytest.raises(DailyExportDateError):
            _validate_date_string("2025-13-01")

    def test_empty_string_raises(self):
        with pytest.raises(DailyExportDateError):
            _validate_date_string("")


# ===========================================================================
# _message_date_utc
# ===========================================================================

class TestMessageDateUtc:

    def test_unix_timestamp(self):
        msg = {"timestamp": 1736935920}   # 2025-01-15T09:12:00Z
        assert _message_date_utc(msg) == "2025-01-15"

    def test_created_at_fallback(self):
        msg = {"created_at": 1736935920}
        assert _message_date_utc(msg) == "2025-01-15"

    def test_iso_string_fallback(self):
        msg = {"timestamp_iso": "2025-01-15T09:12:00+00:00"}
        assert _message_date_utc(msg) == "2025-01-15"

    def test_iso_z_suffix(self):
        msg = {"timestamp_iso": "2025-01-15T09:12:00Z"}
        assert _message_date_utc(msg) == "2025-01-15"

    def test_no_timestamp_returns_none(self):
        assert _message_date_utc({}) is None

    def test_none_timestamp_returns_none(self):
        assert _message_date_utc({"timestamp": None}) is None

    def test_zero_timestamp_returns_none(self):
        # Zero is treated as "no timestamp" (would be 1970-01-01 which is wrong)
        assert _message_date_utc({"timestamp": 0}) is None

    def test_float_timestamp(self):
        # Float timestamps are also valid
        msg = {"timestamp": 1736935920.5}
        assert _message_date_utc(msg) == "2025-01-15"

    def test_unix_takes_priority_over_iso(self):
        # When both are present, Unix timestamp wins
        msg = {
            "timestamp": 1736935920,           # 2025-01-15
            "timestamp_iso": "2025-03-01T00:00:00+00:00",  # different date
        }
        assert _message_date_utc(msg) == "2025-01-15"


# ===========================================================================
# _lead_key
# ===========================================================================

class TestLeadKey:

    def test_lead_id_int(self):
        assert _lead_key({"lead_id": 42}) == 42

    def test_lead_id_string_coerced_to_int(self):
        assert _lead_key({"lead_id": "42"}) == 42

    def test_entity_id_fallback(self):
        assert _lead_key({"entity_id": 99}) == 99

    def test_no_lead_id_returns_sentinel(self):
        assert _lead_key({}) == "_no_lead"

    def test_none_lead_id_returns_sentinel(self):
        assert _lead_key({"lead_id": None}) == "_no_lead"

    def test_zero_lead_id_returns_sentinel(self):
        # 0 is falsy — treated as no lead
        assert _lead_key({"lead_id": 0}) == "_no_lead"


# ===========================================================================
# MessageItem
# ===========================================================================

class TestMessageItem:

    def test_valid_flat_message(self):
        raw = _make_flat_message()
        item = MessageItem.model_validate(raw)
        assert item.message_id == "msg-1"
        assert item.direction == "in"
        assert item.message_text == "Hello"
        assert item.timestamp == 1736935920

    def test_body_mapped_to_message_text(self):
        """'body' field (from raw API) is mapped to message_text."""
        raw = {"id": "x", "body": "test body", "chat_id": "c1"}
        item = MessageItem.model_validate(raw)
        assert item.message_text == "test body"

    def test_id_mapped_to_message_id(self):
        """'id' field is mapped to message_id."""
        raw = {"id": "abc-123", "chat_id": "c1"}
        item = MessageItem.model_validate(raw)
        assert item.message_id == "abc-123"

    def test_author_dict_flattened(self):
        """author as a dict is expanded to author (name), author_type, author_id."""
        raw = {
            "message_id": "m1",
            "author": {"id": 10, "type": "user", "name": "Alice"},
        }
        item = MessageItem.model_validate(raw)
        assert item.author == "Alice"
        assert item.author_type == "user"
        assert item.author_id == 10

    def test_author_string_preserved(self):
        raw = {"message_id": "m1", "author": "Bob"}
        item = MessageItem.model_validate(raw)
        assert item.author == "Bob"

    def test_extra_fields_ignored(self):
        """Unknown fields are silently dropped (extra='ignore')."""
        raw = _make_flat_message()
        raw["some_unknown_field"] = "irrelevant"
        item = MessageItem.model_validate(raw)
        assert not hasattr(item, "some_unknown_field")

    def test_none_fields_default_to_none(self):
        item = MessageItem.model_validate({"message_id": "x"})
        assert item.direction is None
        assert item.message_text is None
        assert item.timestamp is None

    def test_missing_timestamp_is_none(self):
        item = MessageItem.model_validate({})
        assert item.timestamp is None


# ===========================================================================
# ExportResult
# ===========================================================================

class TestExportResult:

    def test_success_status_icon(self):
        r = ExportResult(date="2025-01-15", success=True)
        assert r.status_icon == "✅"

    def test_failure_status_icon(self):
        r = ExportResult(date="2025-01-15", success=False, error="oh no")
        assert r.status_icon == "❌"

    def test_str_success(self):
        r = ExportResult(
            date="2025-01-15",
            success=True,
            total_messages=10,
            total_leads=3,
            duration_s=1.5,
        )
        s = str(r)
        assert "2025-01-15" in s
        assert "10" in s

    def test_str_failure(self):
        r = ExportResult(date="2025-01-15", success=False, error="disk full")
        assert "FAILED" in str(r)
        assert "disk full" in str(r)


# ===========================================================================
# DailyExportGenerator — integration tests using tmp_path
# ===========================================================================

class TestDailyExportGenerator:

    # ── Fixtures ──────────────────────────────────────────────────────────

    @pytest.fixture
    def two_day_messages(self) -> list[dict]:
        """10 messages across 2 days, 2 leads."""
        day1 = 1736935920   # 2025-01-15T09:12:00Z
        day2 = 1737022320   # 2025-01-16T09:12:00Z
        msgs = []
        for i in range(5):
            msgs.append(_make_flat_message(
                message_id=f"d1-{i}", lead_id=42,
                timestamp=day1 + i * 60, text=f"Day1 msg {i}",
            ))
        for i in range(5):
            msgs.append(_make_flat_message(
                message_id=f"d2-{i}", lead_id=99,
                timestamp=day2 + i * 60, text=f"Day2 msg {i}",
            ))
        return msgs

    @pytest.fixture
    def multi_lead_messages(self) -> list[dict]:
        """6 messages on same day, 3 leads."""
        base_ts = 1736935920   # 2025-01-15
        return [
            _make_flat_message("m1", lead_id=10, timestamp=base_ts),
            _make_flat_message("m2", lead_id=10, timestamp=base_ts + 60, direction="out"),
            _make_flat_message("m3", lead_id=20, timestamp=base_ts + 120),
            _make_flat_message("m4", lead_id=30, timestamp=base_ts + 180),
            _make_flat_message("m5", lead_id=30, timestamp=base_ts + 240, direction="out"),
            _make_flat_message("m6", lead_id=30, timestamp=base_ts + 300),
        ]

    # ── Input error cases ──────────────────────────────────────────────────

    def test_missing_input_file_raises(self, tmp_path):
        gen = DailyExportGenerator(
            input_file=tmp_path / "nonexistent.json",
            export_dir=tmp_path / "exports",
        )
        with pytest.raises(DailyExportInputError, match="not found"):
            gen.generate_all()

    def test_empty_input_file_raises(self, tmp_path):
        f = tmp_path / "messages_flat.json"
        f.write_text("")
        gen = DailyExportGenerator(input_file=f, export_dir=tmp_path / "exports")
        with pytest.raises(DailyExportInputError, match="empty"):
            gen.generate_all()

    def test_invalid_json_raises(self, tmp_path):
        f = tmp_path / "messages_flat.json"
        f.write_text("not json {{{")
        gen = DailyExportGenerator(input_file=f, export_dir=tmp_path / "exports")
        with pytest.raises(DailyExportInputError, match="not valid JSON"):
            gen.generate_all()

    def test_missing_messages_key_raises(self, tmp_path):
        f = tmp_path / "messages_flat.json"
        f.write_text(json.dumps({"_meta": {}, "items": []}))
        gen = DailyExportGenerator(input_file=f, export_dir=tmp_path / "exports")
        with pytest.raises(DailyExportInputError, match="no messages list"):
            gen.generate_all()

    # ── generate_all ──────────────────────────────────────────────────────

    def test_generate_all_creates_one_file_per_date(self, tmp_path, two_day_messages):
        f = _make_messages_file(tmp_path, two_day_messages)
        export_dir = tmp_path / "exports"
        gen = DailyExportGenerator(input_file=f, export_dir=export_dir)
        results = gen.generate_all()

        assert len(results) == 2
        assert all(r.success for r in results)
        dates = {r.date for r in results}
        assert "2025-01-15" in dates
        assert "2025-01-16" in dates

    def test_generate_all_files_exist(self, tmp_path, two_day_messages):
        f = _make_messages_file(tmp_path, two_day_messages)
        export_dir = tmp_path / "exports"
        gen = DailyExportGenerator(input_file=f, export_dir=export_dir)
        results = gen.generate_all()

        for r in results:
            assert r.output_path is not None
            assert r.output_path.exists(), f"File missing: {r.output_path}"

    def test_generate_all_message_counts(self, tmp_path, two_day_messages):
        f = _make_messages_file(tmp_path, two_day_messages)
        export_dir = tmp_path / "exports"
        gen = DailyExportGenerator(input_file=f, export_dir=export_dir)
        results = gen.generate_all()

        total = sum(r.total_messages for r in results)
        assert total == 10   # 5 per day

    def test_generate_all_output_is_valid_json(self, tmp_path, two_day_messages):
        f = _make_messages_file(tmp_path, two_day_messages)
        export_dir = tmp_path / "exports"
        gen = DailyExportGenerator(input_file=f, export_dir=export_dir)
        results = gen.generate_all()

        for r in results:
            content = json.loads(r.output_path.read_text(encoding="utf-8"))
            assert "_meta" in content
            assert "leads" in content
            assert isinstance(content["leads"], list)

    def test_generate_all_meta_fields(self, tmp_path, two_day_messages):
        f = _make_messages_file(tmp_path, two_day_messages)
        gen = DailyExportGenerator(input_file=f, export_dir=tmp_path / "exports")
        results = gen.generate_all()

        for r in results:
            content = json.loads(r.output_path.read_text(encoding="utf-8"))
            meta = content["_meta"]
            assert meta["date"] in ("2025-01-15", "2025-01-16")
            assert "generated_at" in meta
            assert "pipeline_version" in meta
            assert meta["total_messages"] > 0
            assert meta["total_leads"] > 0
            assert "date_range" in meta

    def test_generate_all_messages_sorted_chronologically(self, tmp_path):
        """Messages within a lead must be sorted oldest-first."""
        base = 1736935920   # 2025-01-15
        # Add messages out-of-order
        messages = [
            _make_flat_message("m3", lead_id=1, timestamp=base + 200),
            _make_flat_message("m1", lead_id=1, timestamp=base + 0),
            _make_flat_message("m2", lead_id=1, timestamp=base + 100),
        ]
        f = _make_messages_file(tmp_path, messages)
        gen = DailyExportGenerator(input_file=f, export_dir=tmp_path / "exports")
        gen.generate_all()

        export_file = tmp_path / "exports" / "2025-01-15.json"
        content = json.loads(export_file.read_text(encoding="utf-8"))
        lead_msgs = content["leads"][0]["messages"]

        timestamps = [m["timestamp"] for m in lead_msgs]
        assert timestamps == sorted(timestamps), "Messages not sorted chronologically"

    def test_generate_all_multiple_leads_per_day(self, tmp_path, multi_lead_messages):
        f = _make_messages_file(tmp_path, multi_lead_messages)
        gen = DailyExportGenerator(input_file=f, export_dir=tmp_path / "exports")
        results = gen.generate_all()

        assert len(results) == 1
        content = json.loads(results[0].output_path.read_text(encoding="utf-8"))
        assert len(content["leads"]) == 3   # leads 10, 20, 30

    def test_generate_all_per_lead_stats(self, tmp_path, multi_lead_messages):
        f = _make_messages_file(tmp_path, multi_lead_messages)
        gen = DailyExportGenerator(input_file=f, export_dir=tmp_path / "exports")
        results = gen.generate_all()

        content = json.loads(results[0].output_path.read_text(encoding="utf-8"))
        # Lead 10: 2 messages (1 inbound, 1 outbound)
        lead_10 = next(c for c in content["leads"] if c["lead_id"] == 10)
        assert lead_10["stats"]["total_messages"] == 2
        assert lead_10["stats"]["inbound"] == 1
        assert lead_10["stats"]["outbound"] == 1

    def test_no_lead_messages_grouped_under_no_lead(self, tmp_path):
        """Messages without lead_id are grouped under _no_lead bucket."""
        messages = [
            _make_flat_message("m1", lead_id=None, timestamp=1736935920),
            _make_flat_message("m2", lead_id=None, timestamp=1736935980),
        ]
        f = _make_messages_file(tmp_path, messages)
        gen = DailyExportGenerator(input_file=f, export_dir=tmp_path / "exports")
        results = gen.generate_all()

        content = json.loads(results[0].output_path.read_text(encoding="utf-8"))
        assert len(content["leads"]) == 1
        assert content["leads"][0]["lead_id"] is None

    def test_empty_messages_list(self, tmp_path):
        """An input file with an empty messages list produces no output files."""
        f = _make_messages_file(tmp_path, [])
        gen = DailyExportGenerator(input_file=f, export_dir=tmp_path / "exports")
        results = gen.generate_all()
        assert results == []

    def test_messages_with_no_timestamp_are_skipped(self, tmp_path):
        messages = [
            {"message_id": "x", "lead_id": 1, "direction": "in"},   # no timestamp
            _make_flat_message("m1", lead_id=1, timestamp=1736935920),
        ]
        f = _make_messages_file(tmp_path, messages)
        gen = DailyExportGenerator(input_file=f, export_dir=tmp_path / "exports")
        results = gen.generate_all()

        # Only 1 message is exportable (the one with timestamp)
        assert results[0].total_messages == 1

    def test_supports_data_key_instead_of_messages(self, tmp_path):
        """Should also work when the top-level key is 'data' not 'messages'."""
        f = tmp_path / "messages_flat.json"
        payload = {
            "_meta": {"count": 1},
            "data": [_make_flat_message("m1", lead_id=1, timestamp=1736935920)],
        }
        f.write_text(json.dumps(payload), encoding="utf-8")
        gen = DailyExportGenerator(input_file=f, export_dir=tmp_path / "exports")
        results = gen.generate_all()
        assert len(results) == 1
        assert results[0].total_messages == 1

    # ── export_for_date ───────────────────────────────────────────────────

    def test_export_for_date_success(self, tmp_path, two_day_messages):
        f = _make_messages_file(tmp_path, two_day_messages)
        gen = DailyExportGenerator(input_file=f, export_dir=tmp_path / "exports")
        result = gen.export_for_date("2025-01-15")

        assert result.success is True
        assert result.date == "2025-01-15"
        assert result.total_messages == 5

    def test_export_for_date_writes_correct_file(self, tmp_path, two_day_messages):
        f = _make_messages_file(tmp_path, two_day_messages)
        gen = DailyExportGenerator(input_file=f, export_dir=tmp_path / "exports")
        result = gen.export_for_date("2025-01-16")

        assert result.output_path.exists()
        assert result.output_path.name == "2025-01-16.json"

    def test_export_for_date_only_writes_requested_date(self, tmp_path, two_day_messages):
        f = _make_messages_file(tmp_path, two_day_messages)
        export_dir = tmp_path / "exports"
        gen = DailyExportGenerator(input_file=f, export_dir=export_dir)
        gen.export_for_date("2025-01-15")

        # Only one file should exist
        files = list(export_dir.glob("*.json"))
        assert len(files) == 1
        assert files[0].name == "2025-01-15.json"

    def test_export_for_nonexistent_date_returns_failure(self, tmp_path, two_day_messages):
        f = _make_messages_file(tmp_path, two_day_messages)
        gen = DailyExportGenerator(input_file=f, export_dir=tmp_path / "exports")
        result = gen.export_for_date("2099-12-31")

        assert result.success is False
        assert "No messages found" in (result.error or "")

    def test_export_for_date_invalid_format_raises(self, tmp_path, two_day_messages):
        f = _make_messages_file(tmp_path, two_day_messages)
        gen = DailyExportGenerator(input_file=f, export_dir=tmp_path / "exports")
        with pytest.raises(DailyExportDateError):
            gen.export_for_date("15/01/2025")

    # ── export_latest_day ─────────────────────────────────────────────────

    def test_export_latest_day_returns_most_recent(self, tmp_path, two_day_messages):
        f = _make_messages_file(tmp_path, two_day_messages)
        gen = DailyExportGenerator(input_file=f, export_dir=tmp_path / "exports")
        result = gen.export_latest_day()

        assert result.success is True
        assert result.date == "2025-01-16"   # Latest of the two days

    def test_export_latest_day_no_messages_raises(self, tmp_path):
        f = _make_messages_file(tmp_path, [])
        gen = DailyExportGenerator(input_file=f, export_dir=tmp_path / "exports")
        with pytest.raises(DailyExportInputError):
            gen.export_latest_day()

    # ── list_available_dates ──────────────────────────────────────────────

    def test_list_available_dates(self, tmp_path, two_day_messages):
        f = _make_messages_file(tmp_path, two_day_messages)
        gen = DailyExportGenerator(input_file=f, export_dir=tmp_path / "exports")
        dates = gen.list_available_dates()

        assert dates == ["2025-01-15", "2025-01-16"]

    def test_list_available_dates_empty(self, tmp_path):
        f = _make_messages_file(tmp_path, [])
        gen = DailyExportGenerator(input_file=f, export_dir=tmp_path / "exports")
        assert gen.list_available_dates() == []

    def test_list_available_dates_sorted(self, tmp_path):
        """Dates must be returned oldest-first."""
        messages = [
            _make_flat_message("m1", timestamp=1737022320),   # 2025-01-16
            _make_flat_message("m2", timestamp=1736935920),   # 2025-01-15
        ]
        f = _make_messages_file(tmp_path, messages)
        gen = DailyExportGenerator(input_file=f, export_dir=tmp_path / "exports")
        dates = gen.list_available_dates()

        assert dates == sorted(dates)

    # ── Atomic write safety ───────────────────────────────────────────────

    def test_no_tmp_files_left_after_write(self, tmp_path, two_day_messages):
        f = _make_messages_file(tmp_path, two_day_messages)
        export_dir = tmp_path / "exports"
        gen = DailyExportGenerator(input_file=f, export_dir=export_dir)
        gen.generate_all()

        tmp_files = list(export_dir.glob("*.tmp"))
        assert tmp_files == [], f"Leftover .tmp files: {tmp_files}"

    def test_output_is_utf8(self, tmp_path):
        """Unicode characters in message text must be preserved as-is."""
        messages = [
            _make_flat_message("m1", timestamp=1736935920, text="Hola ¿cómo estás? 🏥")
        ]
        f = _make_messages_file(tmp_path, messages)
        gen = DailyExportGenerator(input_file=f, export_dir=tmp_path / "exports")
        gen.generate_all()

        content = (tmp_path / "exports" / "2025-01-15.json").read_text(encoding="utf-8")
        assert "¿cómo estás?" in content
        assert "🏥" in content


# ===========================================================================
# generate_daily_export (convenience function)
# ===========================================================================

class TestGenerateDailyExport:

    def test_with_specific_date(self, tmp_path):
        msgs = [_make_flat_message("m1", timestamp=1736935920)]
        f = _make_messages_file(tmp_path, msgs)
        results = generate_daily_export(
            date="2025-01-15",
            input_file=f,
            export_dir=tmp_path / "exports",
        )
        assert len(results) == 1
        assert results[0].date == "2025-01-15"
        assert results[0].success is True

    def test_without_date_exports_latest(self, tmp_path):
        msgs = [
            _make_flat_message("m1", timestamp=1736935920),   # 2025-01-15
            _make_flat_message("m2", timestamp=1737022320),   # 2025-01-16
        ]
        f = _make_messages_file(tmp_path, msgs)
        results = generate_daily_export(
            date=None,
            input_file=f,
            export_dir=tmp_path / "exports",
        )
        assert len(results) == 1
        assert results[0].date == "2025-01-16"

    def test_returns_list(self, tmp_path):
        msgs = [_make_flat_message("m1", timestamp=1736935920)]
        f = _make_messages_file(tmp_path, msgs)
        results = generate_daily_export(input_file=f, export_dir=tmp_path / "exports")
        assert isinstance(results, list)
