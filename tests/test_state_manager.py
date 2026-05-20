"""
tests/test_state_manager.py
============================
Unit tests for utils/state_manager.py.
All tests use tmp_path — no real state file touched.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from utils.state_manager import StateManager, load_state, save_state


def _make_sm(tmp_path: Path) -> StateManager:
    return StateManager(state_path=tmp_path / "sync_state.json")


# ---------------------------------------------------------------------------
# Init behaviour
# ---------------------------------------------------------------------------

class TestInit:
    def test_creates_empty_state_when_no_file(self, tmp_path):
        sm = _make_sm(tmp_path)
        assert sm._state["schema_version"] == 1
        assert sm._state["entities"] == {}

    def test_loads_existing_file(self, tmp_path):
        path = tmp_path / "sync_state.json"
        data = {
            "schema_version": 1,
            "updated_at": "2025-01-01T00:00:00+00:00",
            "entities": {"leads": {"last_run_at": 9999, "status": "success"}},
        }
        path.write_text(json.dumps(data))
        sm = StateManager(state_path=path)
        assert sm._state["entities"]["leads"]["last_run_at"] == 9999

    def test_recovers_from_corrupt_file(self, tmp_path):
        path = tmp_path / "sync_state.json"
        path.write_text("{{INVALID JSON}}")
        sm = StateManager(state_path=path)
        assert sm._state["entities"] == {}

    def test_repr_shows_entity_count(self, tmp_path):
        sm = _make_sm(tmp_path)
        sm.mark_success("leads", records=10)
        assert "entities=1" in repr(sm)


# ---------------------------------------------------------------------------
# has_been_synced
# ---------------------------------------------------------------------------

class TestHasBeenSynced:
    def test_returns_false_for_unseen_entity(self, tmp_path):
        sm = _make_sm(tmp_path)
        assert sm.has_been_synced("leads") is False

    def test_returns_true_after_mark_success(self, tmp_path):
        sm = _make_sm(tmp_path)
        sm.mark_success("leads", records=100)
        assert sm.has_been_synced("leads") is True

    def test_returns_false_after_mark_failed(self, tmp_path):
        sm = _make_sm(tmp_path)
        sm.mark_failed("leads", error="connection error")
        assert sm.has_been_synced("leads") is False  # last_run_at still None


# ---------------------------------------------------------------------------
# get_last_run_timestamp
# ---------------------------------------------------------------------------

class TestGetLastRunTimestamp:
    def test_returns_none_when_never_synced(self, tmp_path):
        sm = _make_sm(tmp_path)
        assert sm.get_last_run_timestamp("leads") is None

    def test_returns_timestamp_after_success(self, tmp_path):
        sm = _make_sm(tmp_path)
        ts = int(time.time())
        sm.mark_success("leads", run_timestamp=ts)
        assert sm.get_last_run_timestamp("leads") == ts

    def test_different_entities_independent(self, tmp_path):
        sm = _make_sm(tmp_path)
        sm.mark_success("leads",    run_timestamp=1000)
        sm.mark_success("tasks",    run_timestamp=2000)
        sm.mark_success("pipelines", run_timestamp=3000)
        assert sm.get_last_run_timestamp("leads")    == 1000
        assert sm.get_last_run_timestamp("tasks")    == 2000
        assert sm.get_last_run_timestamp("pipelines") == 3000


# ---------------------------------------------------------------------------
# mark_success
# ---------------------------------------------------------------------------

class TestMarkSuccess:
    def test_updates_last_run_at(self, tmp_path):
        sm = _make_sm(tmp_path)
        ts = int(time.time())
        sm.mark_success("leads", records=500, pages=2, run_timestamp=ts)
        state = sm.get_entity_state("leads")
        assert state["last_run_at"]       == ts
        assert state["records_extracted"] == 500
        assert state["pages_fetched"]     == 2
        assert state["status"]            == "success"
        assert state["error"]             is None

    def test_last_run_at_iso_populated(self, tmp_path):
        sm = _make_sm(tmp_path)
        sm.mark_success("leads", run_timestamp=1736847825)
        state = sm.get_entity_state("leads")
        assert "2025" in state["last_run_at_iso"]

    def test_persists_to_disk(self, tmp_path):
        path = tmp_path / "sync_state.json"
        sm = StateManager(state_path=path)
        sm.mark_success("tasks", records=100)
        saved = json.loads(path.read_text())
        assert saved["entities"]["tasks"]["status"] == "success"

    def test_clears_previous_error(self, tmp_path):
        sm = _make_sm(tmp_path)
        sm.mark_failed("leads", error="network error")
        sm.mark_success("leads", records=50)
        assert sm.get_entity_state("leads")["error"] is None


# ---------------------------------------------------------------------------
# mark_failed
# ---------------------------------------------------------------------------

class TestMarkFailed:
    def test_sets_status_failed(self, tmp_path):
        sm = _make_sm(tmp_path)
        sm.mark_failed("leads", error="timeout after 30s")
        state = sm.get_entity_state("leads")
        assert state["status"] == "failed"
        assert "timeout" in state["error"]

    def test_preserves_previous_last_run_at(self, tmp_path):
        sm = _make_sm(tmp_path)
        sm.mark_success("leads", run_timestamp=5000)
        sm.mark_failed("leads", error="API down")
        # last_run_at should still be 5000
        assert sm.get_last_run_timestamp("leads") == 5000

    def test_truncates_long_error_messages(self, tmp_path):
        sm = _make_sm(tmp_path)
        long_err = "x" * 1000
        sm.mark_failed("leads", error=long_err)
        saved_err = sm.get_entity_state("leads")["error"]
        assert len(saved_err) <= 500


# ---------------------------------------------------------------------------
# mark_partial
# ---------------------------------------------------------------------------

class TestMarkPartial:
    def test_sets_status_partial(self, tmp_path):
        sm = _make_sm(tmp_path)
        sm.mark_partial("leads", records=450, failed_records=50)
        state = sm.get_entity_state("leads")
        assert state["status"]            == "partial"
        assert state["records_extracted"] == 450

    def test_updates_last_run_at(self, tmp_path):
        sm = _make_sm(tmp_path)
        before = int(time.time())
        sm.mark_partial("leads", records=450, failed_records=50)
        assert sm.get_last_run_timestamp("leads") >= before


# ---------------------------------------------------------------------------
# Chat / message timestamp
# ---------------------------------------------------------------------------

class TestLastMessageTimestamp:
    def test_returns_none_when_not_set(self, tmp_path):
        sm = _make_sm(tmp_path)
        assert sm.get_last_message_timestamp("chats") is None

    def test_set_and_get_round_trip(self, tmp_path):
        sm = _make_sm(tmp_path)
        ts = 1736847825
        sm.set_last_message_timestamp("chats", timestamp=ts)
        assert sm.get_last_message_timestamp("chats") == ts

    def test_iso_string_populated(self, tmp_path):
        sm = _make_sm(tmp_path)
        sm.set_last_message_timestamp("chats", timestamp=1736847825)
        state = sm.get_entity_state("chats")
        assert "2025" in state["last_message_timestamp_iso"]

    def test_persisted_to_disk(self, tmp_path):
        path = tmp_path / "sync_state.json"
        sm = StateManager(state_path=path)
        sm.set_last_message_timestamp("chats", timestamp=9876543)
        saved = json.loads(path.read_text())
        assert saved["entities"]["chats"]["last_message_timestamp"] == 9876543


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------

class TestReset:
    def test_reset_entity_clears_state(self, tmp_path):
        sm = _make_sm(tmp_path)
        sm.mark_success("leads", records=100, run_timestamp=5000)
        sm.reset_entity("leads")
        assert sm.has_been_synced("leads") is False
        assert sm.get_last_run_timestamp("leads") is None

    def test_reset_all_clears_all_entities(self, tmp_path):
        sm = _make_sm(tmp_path)
        sm.mark_success("leads",    records=100)
        sm.mark_success("tasks",    records=50)
        sm.mark_success("pipelines", records=2)
        sm.reset_all()
        assert sm.get_all_entities() == {}


# ---------------------------------------------------------------------------
# Atomic write safety
# ---------------------------------------------------------------------------

class TestAtomicWrite:
    def test_no_tmp_file_left_on_success(self, tmp_path):
        sm = _make_sm(tmp_path)
        sm.mark_success("leads", records=10)
        tmp_file = sm._path.with_suffix(".tmp")
        assert not tmp_file.exists()

    def test_state_file_created_in_parent_dir(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c" / "sync_state.json"
        sm = StateManager(state_path=deep)
        sm.mark_success("tasks", records=5)
        assert deep.exists()


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------

class TestConvenienceFunctions:
    def test_load_state_returns_dict(self, tmp_path):
        path = tmp_path / "sync_state.json"
        sm = StateManager(state_path=path)
        sm.mark_success("leads", records=50)

        state = load_state(state_path=path)
        assert isinstance(state, dict)
        assert "entities" in state
        assert state["entities"]["leads"]["status"] == "success"

    def test_save_state_merges_updates(self, tmp_path):
        path = tmp_path / "sync_state.json"
        sm = StateManager(state_path=path)
        sm.mark_success("leads", records=50)

        save_state(
            {"entities": {"tasks": {"last_run_at": 9999, "status": "success"}}},
            state_path=path,
        )

        reloaded = load_state(state_path=path)
        assert reloaded["entities"]["leads"]["status"] == "success"   # preserved
        assert reloaded["entities"]["tasks"]["last_run_at"] == 9999   # merged


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

class TestSummary:
    def test_summary_contains_entity_names(self, tmp_path):
        sm = _make_sm(tmp_path)
        sm.mark_success("leads",  records=1523)
        sm.mark_failed("tasks",   error="timeout")
        sm.mark_success("pipelines", records=2)
        text = sm.summary()
        assert "leads"     in text
        assert "tasks"     in text
        assert "pipelines" in text

    def test_summary_shows_no_entities_message(self, tmp_path):
        sm = _make_sm(tmp_path)
        assert "No entities" in sm.summary()
