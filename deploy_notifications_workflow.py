#!/usr/bin/env python3
"""
deploy_notifications_workflow.py
=================================
Builds and deploys the Kommo CRM Notifications Hub workflow to n8n.

WORKFLOW RESPONSIBILITIES
─────────────────────────
  This is the CENTRALISED alert hub for the entire Kommo pipeline.
  All other workflows call this webhook instead of hitting Slack/Telegram/
  Email directly — one change here updates all pipelines.

  INBOUND: Webhook POST from other workflows OR cron-based daily summary.
  ROUTING: By alert_type → severity → channel fan-out.

  Alert types handled:
    extraction_failure  — Kommo API / extraction engine errors
    google_failure      — Sheets / Drive API errors
    claude_failure      — Anthropic API errors or parse failures
    missing_export      — daily_exports/ has no file for today
    validation_error    — schema / data validation failures
    daily_summary       — morning operational roll-up (cron)
    urgent_lead         — lead needs follow-up within <6h (from AI workflow)
    operational_alert   — generic pipeline warning / info

  Deduplication: same alert_fingerprint within 60 minutes → suppressed.
  Escalation: CRITICAL sends to all 3 channels; WARNING to Slack + Telegram;
              INFO to Slack only.

  Channels:
    Slack    — all severity levels, structured blocks
    Telegram — CRITICAL + WARNING + urgent_lead
    Email    — CRITICAL only (Gmail)

PAYLOAD SCHEMA (POST to webhook)
─────────────────────────────────
  {
    "alert_type":    "extraction_failure",   // required
    "severity":      "critical",             // critical|warning|info
    "title":         "Extraction failed",    // required
    "message":       "Full error text",      // required
    "source":        "Workflow 1",           // optional
    "lead_id":       "12345",               // optional
    "lead_name":     "John Doe",            // optional
    "details":       { ...any... },         // optional
    "triggered_by":  "schedule"             // optional
  }

USAGE
─────
    python3 deploy_notifications_workflow.py
    python3 deploy_notifications_workflow.py --activate
    python3 deploy_notifications_workflow.py --export-only
    python3 deploy_notifications_workflow.py --force-new
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "-q"])
    import requests

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

N8N_API_URL  = "http://localhost:5678/api/v1"
N8N_API_KEY  = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIzZjc1YWZkZC0wZjE3LTQ5YTktODljMS0xMmM1YTM4NGIwMjUiLCJpc3MiOiJuOG4iLCJhdWQiOiJwdWJsaWMtYXBpIiwianRpIjoiYjE1Y2QwNmItMDc1Yy00NDE4LTgxNTktMzAwZGI4NTI3MzQ5IiwiaWF0IjoxNzc5OTkxNTQ4fQ.wX3Yv9o0lEtoD37Xrkm05y7H5UTiJP6XUdje1I1dreA"
PROJECT_DIR = "/opt/kommo-platform/app"

HEADERS = {"X-N8N-API-KEY": N8N_API_KEY, "Content-Type": "application/json"}
_READ_ONLY = {"active", "tags", "id", "createdAt", "updatedAt", "versionId"}

def _clean(wf: dict, keep_id: str | None = None) -> dict:
    p = {k: v for k, v in wf.items() if k not in _READ_ONLY}
