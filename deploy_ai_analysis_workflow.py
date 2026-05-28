#!/usr/bin/env python3
"""
deploy_ai_analysis_workflow.py
================================
Builds and deploys the Kommo CRM → Claude AI Analysis workflow to n8n.

WORKFLOW RESPONSIBILITIES
─────────────────────────
  1. Read daily_exports/YYYY-MM-DD.json (per-lead conversation blocks).
  2. Chunk large conversations to stay within Claude's context window.
  3. Send each lead's conversation to Claude API (Anthropic) for:
       • Sentiment analysis  (positive / neutral / negative + score)
       • Buying signal detection (strength: strong / moderate / weak / none)
       • Objection detection  (list of identified objections)
       • Urgent follow-up recommendations  (next action + urgency)
       • Agent performance insights  (response quality score + comments)
  4. Parse + validate Claude's structured JSON response.
  5. Save AI summaries to  outputs/ai_summaries/YYYY-MM-DD.json  on disk.
  6. Push each lead's summary to Google Sheets (AI_Summaries worksheet).
  7. Send a per-lead Telegram alert for HIGH-urgency follow-ups.
  8. Send a daily roll-up summary to Slack.
  9. Handle: API retries, rate-limiting back-off, malformed JSON responses,
     missing export files, and partial failures.

CHUNKING STRATEGY
─────────────────
  Conversations with >50 messages are split into chunks of 40 messages.
  Each chunk is analysed independently. Results are merged into one final
  per-lead summary. This ensures we never exceed Claude's context window
  regardless of conversation length.

PROMPT ENGINEERING
──────────────────
  Model   : claude-3-5-sonnet-20241022  (latest, 200k context)
  Temp    : 0.2  (deterministic, consistent scoring)
  Tokens  : 1024 per lead (structured JSON output is compact)
  Strategy: Role + task + schema enforcement + chain-of-thought suppression

USAGE
─────
    python3 deploy_ai_analysis_workflow.py
    python3 deploy_ai_analysis_workflow.py --activate
    python3 deploy_ai_analysis_workflow.py --export-only
    python3 deploy_ai_analysis_workflow.py --force-new
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
# n8n connection
# ─────────────────────────────────────────────────────────────────────────────

N8N_API_URL = "http://localhost:5678/api/v1"
N8N_API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIzZjc1YWZkZC0wZjE3LTQ5YTktODljMS0xMmM1YTM4NGIwMjUiLCJpc3MiOiJuOG4iLCJhdWQiOiJwdWJsaWMtYXBpIiwianRpIjoiYjE1Y2QwNmItMDc1Yy00NDE4LTgxNTktMzAwZGI4NTI3MzQ5IiwiaWF0IjoxNzc5OTkxNTQ4fQ.wX3Yv9o0lEtoD37Xrkm05y7H5UTiJP6XUdje1I1dreA"
PROJECT_DIR  = "/opt/kommo-platform/app"
EXPORT_DIR   = f"{PROJECT_DIR}/daily_exports"
SUMMARY_DIR  = f"{PROJECT_DIR}/outputs/ai_summaries"

HEADERS = {
    "X-N8N-API-KEY": N8N_API_KEY,
    "Content-Type":  "application/json",
}
_READ_ONLY = {"active", "tags", "id", "createdAt", "updatedAt", "versionId"}

def _clean(wf: dict, keep_id: str | None = None) -> dict:
    p = {k: v for k, v in wf.items() if k not in _READ_ONLY}
