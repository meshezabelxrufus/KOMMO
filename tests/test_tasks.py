"""
tests/test_tasks.py
===================
Unit tests for api/tasks.py — TaskRecord, SlimTaskRecord, TasksExtractor.
All API calls are mocked — no live Kommo account needed.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _raw_task(overrides: dict | None = None) -> dict[str, Any]:
    """Return a valid raw Kommo task dict."""
    base = {
        "id": 5001,
        "created_by": 7712,
        "updated_by": 7712,
        "responsible_user_id": 7712,
        "group_id": 142,
        "entity_id": 10482301,
        "entity_type": "leads",
        "is_completed": False,
        "task_type_id": 1,
        "text": "Follow up call with Acme",
        "duration": 900,
        "complete_till": int(time.time()) + 86400,
        "created_at": int(time.time()) - 3600,
        "updated_at": int(time.time()) - 1800,
        "account_id": 30019,
    }
    if overrides:
        base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# TaskRecord — model validation
# ---------------------------------------------------------------------------

class TestTaskRecord:
    def test_valid_record_parses(self):
        from api.tasks import TaskRecord
        record = TaskRecord.model_validate(_raw_task())
        assert record.id == 5001
        assert record.text == "Follow up call with Acme"
        assert record.entity_id == 10482301
        assert record.is_completed is False
        assert record.responsible_user_id == 7712

    def test_missing_id_raises(self):
        from pydantic import ValidationError
        from api.tasks import TaskRecord
        raw = _raw_task()
        del raw["id"]
        with pytest.raises(ValidationError):
            TaskRecord.model_validate(raw)

    def test_task_type_label_call(self):
        from api.tasks import TaskRecord
        record = TaskRecord.model_validate(_raw_task({"task_type_id": 1}))
        assert record.task_type_label == "call"

    def test_task_type_label_meeting(self):
        from api.tasks import TaskRecord
        record = TaskRecord.model_validate(_raw_task({"task_type_id": 2}))
        assert record.task_type_label == "meeting"

    def test_task_type_label_email(self):
        from api.tasks import TaskRecord
        record = TaskRecord.model_validate(_raw_task({"task_type_id": 3}))
        assert record.task_type_label == "email"

    def test_task_type_label_custom(self):
        from api.tasks import TaskRecord
        record = TaskRecord.model_validate(_raw_task({"task_type_id": 99}))
        assert record.task_type_label == "custom_99"

    def test_complete_till_iso_is_set(self):
        from api.tasks import TaskRecord
        future_ts = int(time.time()) + 86400
        record = TaskRecord.model_validate(_raw_task({"complete_till": future_ts}))
        assert record.complete_till_iso is not None
        assert "T" in record.complete_till_iso   # ISO 8601 format

    def test_is_overdue_false_for_future_deadline(self):
        from api.tasks import TaskRecord
        future_ts = int(time.time()) + 86400
        record = TaskRecord.model_validate(_raw_task({"complete_till": future_ts, "is_completed": False}))
        assert record.is_overdue is False

    def test_is_overdue_true_for_past_deadline(self):
        from api.tasks import TaskRecord
        past_ts = int(time.time()) - 3600
        record = TaskRecord.model_validate(_raw_task({"complete_till": past_ts, "is_completed": False}))
        assert record.is_overdue is True

    def test_completed_task_not_overdue(self):
        from api.tasks import TaskRecord
        past_ts = int(time.time()) - 3600
        record = TaskRecord.model_validate(_raw_task({"complete_till": past_ts, "is_completed": True}))
        assert record.is_overdue is False

    def test_extra_fields_ignored(self):
        from api.tasks import TaskRecord
        raw = _raw_task({"unknown_field_xyz": "should_be_ignored"})
        record = TaskRecord.model_validate(raw)
        assert not hasattr(record, "unknown_field_xyz")

    def test_created_at_iso_populated(self):
        from api.tasks import TaskRecord
        record = TaskRecord.model_validate(_raw_task())
        assert record.created_at_iso is not None


# ---------------------------------------------------------------------------
# SlimTaskRecord
# ---------------------------------------------------------------------------

class TestSlimTaskRecord:
    def test_slim_contains_exactly_6_fields(self):
        from api.tasks import SlimTaskRecord, TaskRecord
        full = TaskRecord.model_validate(_raw_task())
        slim = SlimTaskRecord.from_full(full)
        dumped = slim.model_dump()
        expected_keys = {"task_id", "text", "entity_id", "due_date",
                         "due_date_unix", "is_completed", "responsible_user_id"}
        assert set(dumped.keys()) == expected_keys

    def test_slim_maps_task_id(self):
        from api.tasks import SlimTaskRecord, TaskRecord
        full = TaskRecord.model_validate(_raw_task({"id": 9999}))
        slim = SlimTaskRecord.from_full(full)
        assert slim.task_id == 9999

    def test_slim_due_date_is_iso_string(self):
        from api.tasks import SlimTaskRecord, TaskRecord
        full = TaskRecord.model_validate(_raw_task())
        slim = SlimTaskRecord.from_full(full)
        assert slim.due_date is not None
        assert "T" in slim.due_date

    def test_slim_due_date_none_when_no_deadline(self):
        from api.tasks import SlimTaskRecord, TaskRecord
        full = TaskRecord.model_validate(_raw_task({"complete_till": None}))
        slim = SlimTaskRecord.from_full(full)
        assert slim.due_date is None

    def test_slim_validate_from_raw(self):
        from api.tasks import SlimTaskRecord
        raw = _raw_task()
        slim = SlimTaskRecord.model_validate(raw)
        assert slim.task_id == raw["id"]
        assert slim.is_completed == raw["is_completed"]

    def test_slim_direct_validation_maps_complete_till(self):
        from api.tasks import SlimTaskRecord
        ts = int(time.time()) + 3600
        raw = _raw_task({"complete_till": ts})
        slim = SlimTaskRecord.model_validate(raw)
        assert slim.due_date_unix == ts
        assert slim.due_date is not None


# ---------------------------------------------------------------------------
# TasksExtractor — validation logic
# ---------------------------------------------------------------------------

class TestTasksExtractorValidation:
    def _make_extractor(self, tmp_path: Path):
        from api.tasks import TasksExtractor
        mock_client = MagicMock()
        return TasksExtractor(client=mock_client, output_dir=tmp_path)

    def test_validate_page_valid_records(self, tmp_path):
        extractor = self._make_extractor(tmp_path)
        raw_page = [_raw_task({"id": i}) for i in range(1, 4)]
        valid, failed = extractor._validate_page(raw_page, page_num=1)
        assert len(valid) == 3
        assert len(failed) == 0

    def test_validate_page_invalid_record_goes_to_dead_letter(self, tmp_path):
        extractor = self._make_extractor(tmp_path)
        bad_record = {"id": None, "text": "broken"}    # id=None fails int validation
        valid, failed = extractor._validate_page([bad_record], page_num=1)
        assert len(valid) == 0
        assert len(failed) == 1
        assert "_raw" in failed[0]
        assert "_validation_errors" in failed[0]

    def test_validate_page_mixed_records(self, tmp_path):
        extractor = self._make_extractor(tmp_path)
        records = [
            _raw_task({"id": 1}),             # valid
            {"id": None, "text": "bad"},       # invalid
            _raw_task({"id": 2}),             # valid
        ]
        valid, failed = extractor._validate_page(records, page_num=1)
        assert len(valid) == 2
        assert len(failed) == 1

    def test_validate_page_preserves_page_num_in_failed(self, tmp_path):
        extractor = self._make_extractor(tmp_path)
        _, failed = extractor._validate_page([{"id": None}], page_num=7)
        assert failed[0]["_page"] == 7


# ---------------------------------------------------------------------------
# TasksExtractor — file I/O
# ---------------------------------------------------------------------------

class TestTasksExtractorIO:
    def test_write_json_creates_file(self, tmp_path):
        from api.tasks import TasksExtractor
        extractor = TasksExtractor(client=MagicMock(), output_dir=tmp_path)
        records = [_raw_task({"id": i}) for i in range(1, 4)]
        path = extractor._write_json("tasks.json", records)
        assert path.exists()

    def test_write_json_envelope_structure(self, tmp_path):
        import json as _json
        from api.tasks import TasksExtractor
        extractor = TasksExtractor(client=MagicMock(), output_dir=tmp_path)
        records = [{"id": 1, "is_completed": True, "is_overdue": False}]
        path = extractor._write_json("tasks.json", records)
        content = _json.loads(path.read_text())
        assert "_meta" in content
        assert "data"  in content
        assert content["_meta"]["count"] == 1
        assert content["_meta"]["entity"] == "tasks"
        assert content["_meta"]["completed_count"] == 1

    def test_write_dead_letter_creates_file(self, tmp_path):
        from api.tasks import TasksExtractor
        extractor = TasksExtractor(client=MagicMock(), output_dir=tmp_path)
        failed = [{"_raw": {"id": None}, "_validation_errors": [], "_page": 1}]
        path = extractor._write_dead_letter(failed)
        assert path.exists()
        assert "tasks_failed" in path.name

    def test_write_slim_json_correct_fields(self, tmp_path):
        import json as _json
        from api.tasks import TasksExtractor, SlimTaskRecord, TaskRecord
        extractor = TasksExtractor(client=MagicMock(), output_dir=tmp_path)

        full = TaskRecord.model_validate(_raw_task())
        slim = SlimTaskRecord.from_full(full)
        slim_records = [slim.model_dump(mode="json", by_alias=False)]

        path = extractor._write_slim_json("tasks_slim.json", slim_records)
        content = _json.loads(path.read_text())

        assert content["_meta"]["variant"] == "slim"
        assert "task_id" in content["data"][0]
        assert "due_date" in content["data"][0]
        assert "is_completed" in content["data"][0]


# ---------------------------------------------------------------------------
# TasksExtractor — summary statistics
# ---------------------------------------------------------------------------

class TestTaskExtractionResult:
    def test_completed_count_computed(self, tmp_path):
        from api.tasks import TasksExtractor, TaskExtractionResult
        extractor = TasksExtractor(client=MagicMock(), output_dir=tmp_path)

        tasks = [
            {**_raw_task({"id": 1}), "is_completed": True,  "is_overdue": False},
            {**_raw_task({"id": 2}), "is_completed": False, "is_overdue": True},
            {**_raw_task({"id": 3}), "is_completed": True,  "is_overdue": False},
        ]

        result = TaskExtractionResult()
        result.total_records   = len(tasks)
        result.completed_count = sum(1 for t in tasks if t["is_completed"])
        result.overdue_count   = sum(1 for t in tasks if t["is_overdue"])

        assert result.completed_count == 2
        assert result.overdue_count   == 1

    def test_as_dict_includes_all_keys(self):
        from api.tasks import TaskExtractionResult
        result = TaskExtractionResult()
        d = result.as_dict()
        for key in ["entity", "total_records", "failed_records", "completed_count",
                    "overdue_count", "duration_seconds", "started_at"]:
            assert key in d
