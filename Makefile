# Kommo CRM Integration — Makefile
# Usage: make <target>

.PHONY: help setup auth extract extract-incremental extract-slim \
        leads pipelines tasks tasks-slim contacts chats messages \
        daily-export daily-export-all drive-upload drive-upload-all sheets-sync \
        pipeline pipeline-full pipeline-extraction-only \
        test test-verbose test-coverage \
        clean clean-outputs clean-state clean-exports clean-daily state reset-state

# ---------------------------------------------------------------------------
# Default
# ---------------------------------------------------------------------------
help:
	@echo ""
	@echo "  Kommo CRM Integration — Available Commands"
	@echo "  ─────────────────────────────────────────"
	@echo "  make setup               One-command project setup"
	@echo "  make auth                Run OAuth authorization (one-time)"
	@echo ""
	@echo "  ── Extraction ──────────────────────────────────────"
	@echo "  make extract             Full extraction (all entities)"
	@echo "  make extract-incremental Auto-incremental (reads sync state)"
	@echo "  make extract-slim        Full extraction + slim tasks JSON"
	@echo ""
	@echo "  ── Individual Entities ──────────────────────────────"
	@echo "  make leads               Extract leads only"
	@echo "  make pipelines           Extract pipelines only"
	@echo "  make tasks               Extract tasks only"
	@echo "  make tasks-slim          Extract tasks (full + 6-field slim)"
	@echo "  make contacts            Extract contacts + linked lead IDs"
	@echo "  make contacts-incremental Incremental contacts (last 24h)"
	@echo "  make chats               Extract all chats + messages (flat)"
	@echo "  make chats-incremental   Incremental chats (since last cursor)"
	@echo ""
	@echo "  ── Tests ───────────────────────────────────────────"
	@echo "  make test                Run all tests"
	@echo "  make test-verbose        Run tests with verbose output"
	@echo "  make test-coverage       Tests + HTML coverage report"
	@echo ""
	@echo "  ── Full Pipeline (M1 + M2) ──────────────────────────"
	@echo "  make pipeline             Full M1+M2 pipeline (incremental)"
	@echo "  make pipeline-full        Full M1+M2 pipeline (full re-extraction)"
	@echo "  make pipeline-extraction-only  Extraction only (skip M2 phases)"
	@echo ""
	@echo "  ── Milestone 2 (Google integrations) ───────────────"
	@echo "  make daily-export         Generate today's AI-ready JSON export"
	@echo "  make daily-export-all     Generate exports for ALL available dates"
	@echo "  make drive-upload         Upload latest export to Google Drive"
	@echo "  make drive-upload-all     Upload all local exports to Drive"
	@echo "  make sheets-sync          Sync all worksheets to Google Sheets"
	@echo ""
	@echo "  ── State & Cleanup ─────────────────────────────────"
	@echo "  make state               Print current sync state"
	@echo "  make reset-state         Force full re-extraction next run"
	@echo "  make clean               Remove outputs + state + cache"
	@echo "  make clean-outputs       Remove output JSON files only"
	@echo "  make clean-exports       Remove daily_exports/ files only"
	@echo "  make clean-daily         Remove outputs + exports (keep state)"
	@echo ""

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
setup:
	@chmod +x setup.sh && ./setup.sh

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
auth:
	@source .venv/bin/activate && python run_auth.py

# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
extract:
	@source .venv/bin/activate && python main.py

extract-incremental:
	@source .venv/bin/activate && python main.py --auto-incremental

extract-slim:
	@source .venv/bin/activate && python main.py --slim-tasks

leads:
	@source .venv/bin/activate && python run_leads.py

pipelines:
	@source .venv/bin/activate && python run_pipelines.py

tasks:
	@source .venv/bin/activate && python run_tasks.py

tasks-slim:
	@source .venv/bin/activate && python run_tasks.py --slim

# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------
contacts:
	@source .venv/bin/activate && python run_contacts.py

contacts-incremental:
	@source .venv/bin/activate && python run_contacts.py --auto-incremental

# ---------------------------------------------------------------------------
# Chats & Messages
# ---------------------------------------------------------------------------
chats:
	@source .venv/bin/activate && python run_chats.py

chats-incremental:
	@source .venv/bin/activate && python run_chats.py --auto-incremental

messages: chats
	@echo "Flat messages written to outputs/messages_flat.json"

# ---------------------------------------------------------------------------
# Full Pipeline (M1 + M2)
# ---------------------------------------------------------------------------
pipeline:
	@source .venv/bin/activate && python main.py --auto-incremental

pipeline-full:
	@source .venv/bin/activate && python main.py

pipeline-extraction-only:
	@source .venv/bin/activate && python main.py --extraction-only

# ---------------------------------------------------------------------------
# Milestone 2 — Daily AI Export
# ---------------------------------------------------------------------------
daily-export:
	@source .venv/bin/activate && python run_daily_export.py

daily-export-all:
	@source .venv/bin/activate && python run_daily_export.py --all

# ---------------------------------------------------------------------------
# Milestone 2 — Google Drive Upload
# ---------------------------------------------------------------------------
drive-upload:
	@source .venv/bin/activate && python run_drive_upload.py

drive-upload-all:
	@source .venv/bin/activate && python run_drive_upload.py --all

# ---------------------------------------------------------------------------
# Milestone 2 — Google Sheets Sync
# ---------------------------------------------------------------------------
sheets-sync:
	@source .venv/bin/activate && python run_sheets_sync.py

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------
test:
	@source .venv/bin/activate && pytest

test-verbose:
	@source .venv/bin/activate && pytest -v --tb=long

test-coverage:
	@source .venv/bin/activate && pytest --cov=. --cov-report=term-missing --cov-report=html

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
state:
	@source .venv/bin/activate && python -c "\
from utils.state_manager import StateManager; \
sm = StateManager(); \
print(sm.summary())"

reset-state:
	@source .venv/bin/activate && python -c "\
from utils.state_manager import StateManager; \
sm = StateManager(); \
sm.reset_all(); \
print('Sync state reset — next run will be a full extraction')"

# ---------------------------------------------------------------------------
# Clean
# ---------------------------------------------------------------------------
clean:
	@echo "Removing output files..."
	@rm -f outputs/*.json outputs/errors/*.json outputs/data/*.json
	@echo "Removing sync state..."
	@rm -f state/sync_state.json
	@echo "Removing Python cache..."
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -name "*.pyc" -delete 2>/dev/null || true
	@echo "Done."

clean-outputs:
	@rm -f outputs/*.json outputs/errors/*.json
	@echo "Output files removed."

clean-exports:
	@rm -f daily_exports/*.json
	@echo "Daily export files removed."

clean-daily:
	@rm -f outputs/*.json outputs/errors/*.json daily_exports/*.json
	@echo "Outputs and daily exports removed."

clean-state:
	@rm -f state/sync_state.json
	@echo "Sync state removed."
