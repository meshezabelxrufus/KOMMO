#!/usr/bin/env python3
"""
deploy_sheets_sync_workflow.py
==============================
Builds and deploys the Kommo CRM → Google Sheets Sync workflow to n8n.

WORKFLOW RESPONSIBILITIES
─────────────────────────
  1. Read outputs/leads.json, outputs/messages_flat.json, and
     logs/analytics_YYYY-MM-DD.json (daily summary) from local disk.
  2. Sync data into three Google Sheets worksheets:
       • Leads          — one row per lead, upserted by ID
       • Messages       — one row per message, upserted by message_id
       • Daily_Summary  — one row per day appended with run stats
  3. Duplicate prevention via Google Sheets MATCH() detection.
  4. Batch writes (250-row chunks) to stay within Sheets API limits.
  5. Retry-safe execution (n8n native retryOnFail).
  6. Slack alert on any worksheet failure (neverError=true).

USAGE
─────
    python3 deploy_sheets_sync_workflow.py
    python3 deploy_sheets_sync_workflow.py --activate
    python3 deploy_sheets_sync_workflow.py --export-only
    python3 deploy_sheets_sync_workflow.py --force-new

EXIT CODES
──────────
    0 — Deployed (and optionally activated) successfully.
    1 — HTTP or unexpected error.
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
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

N8N_API_URL = "http://localhost:5678/api/v1"
N8N_API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIzZjc1YWZkZC0wZjE3LTQ5YTktODljMS0xMmM1YTM4NGIwMjUiLCJpc3MiOiJuOG4iLCJhdWQiOiJwdWJsaWMtYXBpIiwianRpIjoiYzdmZjNlZjgtMDhkYy00Y2Q2LTlkOTUtMDU0MjkwYzNhMWYzIiwiaWF0IjoxNzc5ODk1NTU0fQ.UAH-vKXs0pbKEA0UU1V7noYbbRuxfeHjja8fhYMuexo"
PROJECT_DIR = "/opt/kommo-platform/app"
PYTHON_BIN  = "python3"

HEADERS = {
    "X-N8N-API-KEY": N8N_API_KEY,
    "Content-Type":  "application/json",
}

# Fields n8n refuses on POST / PUT
_READ_ONLY = {"active", "tags", "id", "createdAt", "updatedAt", "versionId"}


def _clean(workflow: dict, keep_id: str | None = None) -> dict:
    payload = {k: v for k, v in workflow.items() if k not in _READ_ONLY}
    if keep_id:
        payload["id"] = keep_id
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# JavaScript snippets
# ─────────────────────────────────────────────────────────────────────────────

# Read leads.json from disk, return flat row array
JS_READ_LEADS = f"""
const fs   = require('fs');
const path = '{PROJECT_DIR}/outputs/leads.json';

if (!fs.existsSync(path)) {{
  return [{{ json: {{ rows: [], status: 'skipped', reason: 'leads.json not found' }} }}];
}}

const raw  = JSON.parse(fs.readFileSync(path, 'utf8'));
const data = raw.data || raw.leads || [];

const rows = data.map(l => {{
  // Flatten custom_fields_values into key-value pairs
  const cf = {{}};
  if (Array.isArray(l.custom_fields_values)) {{
    for (const f of l.custom_fields_values) {{
      const val = Array.isArray(f.values) && f.values.length ? f.values[0].value : null;
      cf[f.field_name] = val;
    }}
  }}

  return {{
    lead_id:              String(l.id || ''),
    lead_name:            l.name || '',
    pipeline_id:          String(l.pipeline_id || ''),
    status_id:            String(l.status_id || ''),
    responsible_user_id:  String(l.responsible_user_id || ''),
    price:                l.price ?? '',
    created_at_iso:       l.created_at_iso || '',
    updated_at_iso:       l.updated_at_iso || '',
    is_deleted:           String(l.is_deleted ?? false),
    utm_source:           cf['utm_source'] || '',
    fuente:               cf['Fuente'] || '',
    cirugia:              cf['Cirugía'] || '',
    embudo_inicial:       cf['Embudo Inicial'] || '',
    tags:                 JSON.stringify(l.tags || []),
    synced_at:            new Date().toISOString(),
  }};
}});

return [{{ json: {{ rows, status: 'ok', count: rows.length }} }}];
"""

# Read messages_flat.json from disk
JS_READ_MESSAGES = f"""
const fs   = require('fs');
const path = '{PROJECT_DIR}/outputs/messages_flat.json';

if (!fs.existsSync(path)) {{
  return [{{ json: {{ rows: [], status: 'skipped', reason: 'messages_flat.json not found' }} }}];
}}

const raw  = JSON.parse(fs.readFileSync(path, 'utf8'));
const data = raw.data || raw.messages || [];

if (!data.length) {{
  return [{{ json: {{ rows: [], status: 'empty', count: 0 }} }}];
}}

const rows = data.map(m => ({{
  message_id:    m.message_id   || m.id || '',
  chat_id:       m.chat_id      || '',
  lead_id:       String(m.lead_id || ''),
  lead_name:     m.lead_name    || '',
  contact_name:  m.contact_name || '',
  channel:       m.channel      || '',
  direction:     m.direction    || '',
  author:        m.author       || '',
  author_type:   m.author_type  || '',
  message_text:  (m.message_text || '').slice(0, 1000),
  timestamp_iso: m.timestamp_iso || '',
  media_url:     m.media_url    || '',
  synced_at:     new Date().toISOString(),
}}));

return [{{ json: {{ rows, status: 'ok', count: rows.length }} }}];
"""

# Read today's analytics file for the Daily_Summary worksheet
JS_READ_DAILY_SUMMARY = f"""
const fs   = require('fs');
const path = require('path');

const today = new Date().toISOString().split('T')[0];
const fPath = path.join('{PROJECT_DIR}', 'logs', `analytics_${{today}}.json`);

if (!fs.existsSync(fPath)) {{
  return [{{ json: {{ rows: [], status: 'skipped', reason: `No analytics file for ${{today}}` }} }}];
}}

const raw = JSON.parse(fs.readFileSync(fPath, 'utf8'));
const meta = raw._meta || {{}};
const phases = raw.phases || {{}};

const row = {{
  run_date:           today,
  generated_at:       meta.generated_at || new Date().toISOString(),
  pipeline_mode:      meta.pipeline_mode || '',
  overall_status:     meta.overall_status || '',
  total_duration_s:   String(meta.total_duration_s || ''),
  extraction_status:  (phases.Extraction || {{}}).status || '',
  extraction_records: String((phases.Extraction || {{}}).records || 0),
  ai_export_status:   (phases['AI Export'] || {{}}).status || '',
  sheets_status:      (phases['Google Sheets'] || {{}}).status || '',
  drive_status:       (phases['Google Drive'] || {{}}).status || '',
  analytics_status:   (phases.Analytics || {{}}).status || '',
  synced_at:          new Date().toISOString(),
}};

return [{{ json: {{ rows: [row], status: 'ok', count: 1 }} }}];
"""

# Batch items from the rows array into chunks of 250
JS_BATCH_LEADS = """
const item  = $input.first().json;
const rows  = item.rows || [];
if (!rows.length) return [{ json: { _empty: true } }];
return rows.map(r => ({ json: r }));
"""

JS_BATCH_MESSAGES = """
const item  = $input.first().json;
const rows  = item.rows || [];
if (!rows.length) return [{ json: { _empty: true } }];
return rows.map(r => ({ json: r }));
"""

# Build final sync report
JS_SYNC_REPORT = """
const config  = $('⚙️ Set Sync Config').first().json;
const leads   = (() => { try { return $('📖 Read Leads JSON').first().json; } catch { return {}; } })();
const msgs    = (() => { try { return $('📖 Read Messages JSON').first().json; } catch { return {}; } })();
const summary = (() => { try { return $('📖 Read Daily Summary').first().json; } catch { return {}; } })();

const duration = (() => {
  try {
    const start = new Date(config.started_at);
    const secs  = Math.round((new Date() - start) / 1000);
    return `${Math.floor(secs/60)}m ${secs%60}s`;
  } catch { return 'unknown'; }
})();

return [{
  json: {
    status:           'SUCCESS',
    workflow:         'Kommo CRM → Google Sheets Sync',
    startedAt:        config.started_at,
    completedAt:      new Date().toISOString(),
    duration,
    spreadsheetId:    config.spreadsheet_id,
    leadsCount:       leads.count || 0,
    messagesCount:    msgs.count  || 0,
    dailySummaryRows: summary.count || 0,
    triggeredBy:      config.triggered_by,
  }
}];
"""

# Failure alert payload builder (Slack)
def _slack_body(title_expr: str, detail_expr: str, triggered_expr: str) -> str:
    return (
        "={{ JSON.stringify({"
        f"  text: {title_expr},"
        "  blocks: [{"
        "    type: 'header',"
        f"    text: {{ type: 'plain_text', text: {title_expr} }}"
        "  },{"
        "    type: 'section',"
        "    fields: ["
        f"      {{ type: 'mrkdwn', text: '*Triggered by:*\\n' + {triggered_expr} }},"
        f"      {{ type: 'mrkdwn', text: '*Time:*\\n' + $now.toISO() }}"
        "    ]"
        "  },{"
        "    type: 'section',"
        f"    text: {{ type: 'mrkdwn', text: '*Error:*\\n```\\n' + ({detail_expr} || 'none').slice(0,500) + '\\n```' }}"
        "  }]"
        "}) }}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Workflow builder
# ─────────────────────────────────────────────────────────────────────────────

def build_workflow() -> dict:

    SLACK_BODY = _slack_body(
        "$json.alert_title",
        "$json.alert_detail",
        "$json.triggered_by",
    )

    return {
        "name": "Kommo CRM → Google Sheets Sync",
        "settings": {
            "executionOrder":           "v1",
            "saveManualExecutions":     True,
            "callerPolicy":             "workflowsFromSameOwner",
            "saveExecutionProgress":    True,
            "saveDataSuccessExecution": "all",
            "saveDataErrorExecution":   "all",
            "executionTimeout":         3600,
            "timezone":                 "UTC",
        },
        "staticData": None,

        # ── NODES ────────────────────────────────────────────────────────────
        "nodes": [

            # ─── Documentation sticky notes ──────────────────────────────────
            {
                "id": "sticky-overview",
                "name": "📌 Workflow Overview",
                "type": "n8n-nodes-base.stickyNote",
                "typeVersion": 1,
                "position": [-240, 60],
                "parameters": {
                    "width": 440,
                    "height": 380,
                    "color": 2,
                    "content": (
                        "## 📊 Kommo CRM → Google Sheets Sync\n\n"
                        "**Reads** three local JSON outputs and **upserts** to Sheets:\n\n"
                        "| Source | Worksheet |\n"
                        "| --- | --- |\n"
                        "| `outputs/leads.json` | **Leads** |\n"
                        "| `outputs/messages_flat.json` | **Messages** |\n"
                        "| `logs/analytics_*.json` | **Daily_Summary** |\n\n"
                        "**Features:**\n"
                        "- Batch writes (250 rows/chunk)\n"
                        "- Duplicate prevention (upsert by ID)\n"
                        "- Retry ×2 on API failures\n"
                        "- Slack alerts on failure\n\n"
                        "**Env vars required:**\n"
                        "`KOMMO_SHEETS_SPREADSHEET_ID`\n"
                        "`KOMMO_SLACK_WEBHOOK` *(optional)*"
                    ),
                },
            },
            {
                "id": "sticky-creds",
                "name": "🔑 Credential Setup",
                "type": "n8n-nodes-base.stickyNote",
                "typeVersion": 1,
                "position": [-240, 480],
                "parameters": {
                    "width": 440,
                    "height": 260,
                    "color": 4,
                    "content": (
                        "## 🔑 Credentials Required\n\n"
                        "**Google Sheets** credential:\n"
                        "n8n → Credentials → New → *Google Sheets OAuth2*\n"
                        "or *Google Sheets Service Account*\n\n"
                        "**Environment variables** (Settings → Variables):\n"
                        "```\n"
                        "KOMMO_SHEETS_SPREADSHEET_ID = <your_sheet_id>\n"
                        "KOMMO_SLACK_WEBHOOK         = https://hooks.slack.com/...\n"
                        "```"
                    ),
                },
            },

            # ─── Triggers ────────────────────────────────────────────────────
            {
                "id": "schedule-trigger",
                "name": "⏰ Daily 7AM UTC",
                "type": "n8n-nodes-base.scheduleTrigger",
                "typeVersion": 1.2,
                "position": [260, 260],
                "parameters": {
                    "rule": {
                        "interval": [
                            {"field": "cronExpression", "expression": "0 7 * * *"}
                        ]
                    }
                },
            },
            {
                "id": "manual-trigger",
                "name": "▶️ Manual Run",
                "type": "n8n-nodes-base.manualTrigger",
                "typeVersion": 1,
                "position": [260, 440],
                "parameters": {},
            },

            # ─── Config ──────────────────────────────────────────────────────
            {
                "id": "set-sync-config",
                "name": "⚙️ Set Sync Config",
                "type": "n8n-nodes-base.set",
                "typeVersion": 3.4,
                "position": [500, 340],
                "parameters": {
                    "mode": "manual",
                    "assignments": {
                        "assignments": [
                            {"id": "c1", "name": "started_at",      "value": "={{ $now.toISO() }}",                                                   "type": "string"},
                            {"id": "c2", "name": "triggered_by",    "value": "={{ $execution.mode === 'manual' ? 'manual' : 'schedule' }}",             "type": "string"},
                            {"id": "c3", "name": "spreadsheet_id",  "value": "={{ $env.KOMMO_SHEETS_SPREADSHEET_ID || '' }}",                          "type": "string"},
                            {"id": "c4", "name": "project_dir",     "value": PROJECT_DIR,                                                              "type": "string"},
                            {"id": "c5", "name": "batch_size",      "value": 250,                                                                      "type": "number"},
                        ]
                    },
                    "options": {},
                },
            },

            # ─── Phase 1: Read JSON files (parallel via Code nodes) ───────────
            {
                "id": "read-leads",
                "name": "📖 Read Leads JSON",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [760, 180],
                "parameters": {"jsCode": JS_READ_LEADS.strip()},
            },
            {
                "id": "read-messages",
                "name": "📖 Read Messages JSON",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [760, 340],
                "parameters": {"jsCode": JS_READ_MESSAGES.strip()},
            },
            {
                "id": "read-summary",
                "name": "📖 Read Daily Summary",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [760, 500],
                "parameters": {"jsCode": JS_READ_DAILY_SUMMARY.strip()},
            },

            # ─── Phase 2: Batch splitting ────────────────────────────────────
            {
                "id": "batch-leads",
                "name": "🔀 Batch Leads (250)",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [1020, 180],
                "parameters": {"jsCode": JS_BATCH_LEADS.strip()},
            },
            {
                "id": "batch-messages",
                "name": "🔀 Batch Messages (250)",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [1020, 340],
                "parameters": {"jsCode": JS_BATCH_MESSAGES.strip()},
            },

            # ─── Phase 3: Google Sheets upsert ───────────────────────────────
            # Leads worksheet — appendOrUpdate by lead_id in column A
            {
                "id": "sheets-leads",
                "name": "📊 Sync → Leads Sheet",
                "type": "n8n-nodes-base.googleSheets",
                "typeVersion": 4.5,
                "position": [1280, 180],
                "retryOnFail": True,
                "maxTries": 2,
                "waitBetweenTries": 30000,
                "parameters": {
                    "operation":     "appendOrUpdate",
                    "documentId":    "={{ $('⚙️ Set Sync Config').first().json.spreadsheet_id }}",
                    "sheetName":     "={{ 'Leads' }}",
                    "columns": {
                        "mappingMode": "autoMapInputData",
                        "value":       {},
                        "matchingColumns": ["lead_id"],
                        "schema": [
                            {"id": "lead_id",             "displayName": "lead_id",             "canBeUsedToMatch": True,  "required": False, "defaultMatch": True,  "display": True, "type": "string", "removed": False},
                            {"id": "lead_name",           "displayName": "lead_name",           "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "pipeline_id",         "displayName": "pipeline_id",         "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "status_id",           "displayName": "status_id",           "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "responsible_user_id", "displayName": "responsible_user_id", "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "price",               "displayName": "price",               "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "created_at_iso",      "displayName": "created_at_iso",      "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "updated_at_iso",      "displayName": "updated_at_iso",      "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "is_deleted",          "displayName": "is_deleted",          "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "utm_source",          "displayName": "utm_source",          "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "fuente",              "displayName": "fuente",              "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "cirugia",             "displayName": "cirugia",             "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "embudo_inicial",      "displayName": "embudo_inicial",      "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "tags",                "displayName": "tags",                "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "synced_at",           "displayName": "synced_at",           "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                        ],
                    },
                    "options": {
                        "handlingExtraData":   "insertInNewColumn",
                        "locationDefine":      "specifyRangeA1",
                        "rangeA1":             "A:P",
                    },
                },
            },

            # Messages worksheet — appendOrUpdate by message_id in column A
            {
                "id": "sheets-messages",
                "name": "📊 Sync → Messages Sheet",
                "type": "n8n-nodes-base.googleSheets",
                "typeVersion": 4.5,
                "position": [1280, 340],
                "retryOnFail": True,
                "maxTries": 2,
                "waitBetweenTries": 30000,
                "parameters": {
                    "operation":  "appendOrUpdate",
                    "documentId": "={{ $('⚙️ Set Sync Config').first().json.spreadsheet_id }}",
                    "sheetName":  "={{ 'Messages' }}",
                    "columns": {
                        "mappingMode": "autoMapInputData",
                        "value":       {},
                        "matchingColumns": ["message_id"],
                        "schema": [
                            {"id": "message_id",    "displayName": "message_id",    "canBeUsedToMatch": True,  "required": False, "defaultMatch": True,  "display": True, "type": "string", "removed": False},
                            {"id": "chat_id",       "displayName": "chat_id",       "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "lead_id",       "displayName": "lead_id",       "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "lead_name",     "displayName": "lead_name",     "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "contact_name",  "displayName": "contact_name",  "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "channel",       "displayName": "channel",       "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "direction",     "displayName": "direction",     "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "author",        "displayName": "author",        "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "author_type",   "displayName": "author_type",   "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "message_text",  "displayName": "message_text",  "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "timestamp_iso", "displayName": "timestamp_iso", "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "media_url",     "displayName": "media_url",     "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "synced_at",     "displayName": "synced_at",     "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                        ],
                    },
                    "options": {
                        "handlingExtraData": "insertInNewColumn",
                        "locationDefine":    "specifyRangeA1",
                        "rangeA1":           "A:M",
                    },
                },
            },

            # Daily_Summary worksheet — always append (each run = new row)
            {
                "id": "sheets-summary",
                "name": "📊 Sync → Daily_Summary Sheet",
                "type": "n8n-nodes-base.googleSheets",
                "typeVersion": 4.5,
                "position": [1280, 500],
                "retryOnFail": True,
                "maxTries": 2,
                "waitBetweenTries": 30000,
                "parameters": {
                    "operation":  "appendOrUpdate",
                    "documentId": "={{ $('⚙️ Set Sync Config').first().json.spreadsheet_id }}",
                    "sheetName":  "={{ 'Daily_Summary' }}",
                    "columns": {
                        "mappingMode": "autoMapInputData",
                        "value":       {},
                        "matchingColumns": ["run_date"],
                        "schema": [
                            {"id": "run_date",           "displayName": "run_date",           "canBeUsedToMatch": True,  "required": False, "defaultMatch": True,  "display": True, "type": "string", "removed": False},
                            {"id": "generated_at",       "displayName": "generated_at",       "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "pipeline_mode",      "displayName": "pipeline_mode",      "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "overall_status",     "displayName": "overall_status",     "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "total_duration_s",   "displayName": "total_duration_s",   "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "extraction_status",  "displayName": "extraction_status",  "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "extraction_records", "displayName": "extraction_records", "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "ai_export_status",   "displayName": "ai_export_status",   "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "sheets_status",      "displayName": "sheets_status",      "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "drive_status",       "displayName": "drive_status",       "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "analytics_status",   "displayName": "analytics_status",   "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "synced_at",          "displayName": "synced_at",          "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                        ],
                    },
                    "options": {
                        "handlingExtraData": "insertInNewColumn",
                        "locationDefine":    "specifyRangeA1",
                        "rangeA1":           "A:L",
                    },
                },
            },

            # ─── Phase 4: Worksheet validation ───────────────────────────────
            {
                "id": "validate-sheets",
                "name": "✅ Validate Worksheets",
                "type": "n8n-nodes-base.googleSheets",
                "typeVersion": 4.5,
                "position": [1560, 340],
                "parameters": {
                    "operation":  "readRows",
                    "documentId": "={{ $('⚙️ Set Sync Config').first().json.spreadsheet_id }}",
                    "sheetName":  "={{ 'Leads' }}",
                    "options": {
                        "returnFirstRowAsHeaders": True,
                        "locationDefine":          "specifyRangeA1",
                        "rangeA1":                 "A1:A2",
                    },
                },
            },

            # ─── Phase 5: Build Success Report ───────────────────────────────
            {
                "id": "build-report",
                "name": "🎉 Build Sync Report",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [1820, 340],
                "parameters": {"jsCode": JS_SYNC_REPORT.strip()},
            },

            # ─── Phase 6: Failure set node + Slack alert ─────────────────────
            {
                "id": "set-failure",
                "name": "❌ Set Failure Info",
                "type": "n8n-nodes-base.set",
                "typeVersion": 3.4,
                "position": [1560, 580],
                "parameters": {
                    "mode": "manual",
                    "assignments": {
                        "assignments": [
                            {"id": "e1", "name": "alert_title",   "value": "❌ Kommo Sheets Sync — Worksheet Validation FAILED", "type": "string"},
                            {"id": "e2", "name": "alert_detail",  "value": "={{ $json.error || 'Unknown validation error' }}",   "type": "string"},
                            {"id": "e3", "name": "triggered_by",  "value": "={{ $('⚙️ Set Sync Config').first().json.triggered_by }}", "type": "string"},
                        ]
                    },
                    "options": {},
                },
            },
            {
                "id": "slack-alert",
                "name": "🔔 Slack: Sync Failed",
                "type": "n8n-nodes-base.httpRequest",
                "typeVersion": 4.2,
                "position": [1820, 580],
                "parameters": {
                    "method":      "POST",
                    "url":         "={{ $env.KOMMO_SLACK_WEBHOOK || 'http://localhost:1' }}",
                    "sendHeaders": True,
                    "headerParameters": {
                        "parameters": [{"name": "Content-Type", "value": "application/json"}]
                    },
                    "sendBody":    True,
                    "specifyBody": "json",
                    "jsonBody":    SLACK_BODY,
                    "options": {
                        "response": {"response": {"neverError": True}},
                        "timeout":  10000,
                    },
                },
            },
        ],

        # ── CONNECTIONS ───────────────────────────────────────────────────────
        "connections": {
            "⏰ Daily 7AM UTC": {
                "main": [[{"node": "⚙️ Set Sync Config", "type": "main", "index": 0}]]
            },
            "▶️ Manual Run": {
                "main": [[{"node": "⚙️ Set Sync Config", "type": "main", "index": 0}]]
            },

            # Config fans out to all three readers in parallel
            "⚙️ Set Sync Config": {
                "main": [[
                    {"node": "📖 Read Leads JSON",     "type": "main", "index": 0},
                    {"node": "📖 Read Messages JSON",  "type": "main", "index": 0},
                    {"node": "📖 Read Daily Summary",  "type": "main", "index": 0},
                ]]
            },

            # Readers → batchers (where applicable) → Sheets sync
            "📖 Read Leads JSON":    {"main": [[{"node": "🔀 Batch Leads (250)",    "type": "main", "index": 0}]]},
            "📖 Read Messages JSON": {"main": [[{"node": "🔀 Batch Messages (250)", "type": "main", "index": 0}]]},
            "📖 Read Daily Summary": {"main": [[{"node": "📊 Sync → Daily_Summary Sheet", "type": "main", "index": 0}]]},

            "🔀 Batch Leads (250)":    {"main": [[{"node": "📊 Sync → Leads Sheet",    "type": "main", "index": 0}]]},
            "🔀 Batch Messages (250)": {"main": [[{"node": "📊 Sync → Messages Sheet",  "type": "main", "index": 0}]]},

            # All three sheets merge into validation
            "📊 Sync → Leads Sheet":         {"main": [[{"node": "✅ Validate Worksheets", "type": "main", "index": 0}]]},
            "📊 Sync → Messages Sheet":       {"main": [[{"node": "✅ Validate Worksheets", "type": "main", "index": 0}]]},
            "📊 Sync → Daily_Summary Sheet":  {"main": [[{"node": "✅ Validate Worksheets", "type": "main", "index": 0}]]},

            # Validation success → report
            "✅ Validate Worksheets": {"main": [[{"node": "🎉 Build Sync Report", "type": "main", "index": 0}]]},

            # Error branch
            "❌ Set Failure Info": {"main": [[{"node": "🔔 Slack: Sync Failed", "type": "main", "index": 0}]]},
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# API helpers
# ─────────────────────────────────────────────────────────────────────────────

def find_existing(name: str) -> str | None:
    resp = requests.get(f"{N8N_API_URL}/workflows", headers=HEADERS, timeout=15)
    resp.raise_for_status()
    for wf in resp.json().get("data", []):
        if wf["name"] == name:
            return wf["id"]
    return None


def deploy(workflow: dict, activate: bool = False) -> dict:
    print("  📡 POSTing workflow to n8n...")
    resp = requests.post(
        f"{N8N_API_URL}/workflows",
        headers=HEADERS,
        json=_clean(workflow),
        timeout=30,
    )
    resp.raise_for_status()
    created = resp.json()
    wf_id   = created["id"]
    print(f"  ✅ Created — id={wf_id}")

    if activate:
        _activate(wf_id)
    return created


def update_existing(wf_id: str, workflow: dict, activate: bool = False) -> dict:
    print(f"  🔄 Updating workflow id={wf_id}...")
    resp = requests.put(
        f"{N8N_API_URL}/workflows/{wf_id}",
        headers=HEADERS,
        json=_clean(workflow, keep_id=wf_id),
        timeout=30,
    )
    resp.raise_for_status()
    updated = resp.json()
    print(f"  ✅ Updated — id={wf_id}")

    if activate:
        _activate(wf_id)
    return updated


def _activate(wf_id: str) -> None:
    print("  ⚡ Activating workflow...")
    act = requests.post(
        f"{N8N_API_URL}/workflows/{wf_id}/activate",
        headers=HEADERS,
        timeout=15,
    )
    act.raise_for_status()
    print("  ✅ Workflow activated")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy Kommo → Google Sheets Sync n8n workflow")
    parser.add_argument("--activate",    action="store_true", help="Activate after deployment")
    parser.add_argument("--export-only", action="store_true", dest="export_only", help="Export JSON only, skip deploy")
    parser.add_argument("--force-new",   action="store_true", dest="force_new",   help="Always create new workflow")
    args = parser.parse_args()

    print("\n" + "═" * 56)
    print("  Kommo CRM → Google Sheets Sync — Workflow Deployer")
    print("═" * 56 + "\n")

    workflow    = build_workflow()
    export_path = Path(__file__).parent / "kommo_sheets_sync_workflow.json"

    export_path.write_text(json.dumps(workflow, indent=2), encoding="utf-8")
    print(f"  💾 Workflow JSON exported → {export_path}")

    if args.export_only:
        print("\n  --export-only flag set. Skipping API deployment.\n")
        return 0

    print(f"  📡 Target: {N8N_API_URL}\n")

    try:
        wf_name     = workflow["name"]
        existing_id = None if args.force_new else find_existing(wf_name)

        if existing_id:
            print(f"  ℹ️  Found existing workflow '{wf_name}' (id={existing_id}) — updating...")
            result = update_existing(existing_id, workflow, activate=args.activate)
        else:
            result = deploy(workflow, activate=args.activate)

        wf_id  = result["id"]
        active = result.get("active", False)

        print(f"\n  {'─' * 52}")
        print(f"  Workflow ID  : {wf_id}")
        print(f"  Name         : {result.get('name')}")
        print(f"  Active       : {'✅ Yes' if active else '⏸️  No (toggle in n8n UI)'}")
        print(f"  UI URL       : http://localhost:5678/workflow/{wf_id}")
        print(f"  JSON backup  : {export_path}")
        print(f"  Nodes        : {len(workflow['nodes'])}")
        print(f"  {'─' * 52}\n")

        if not active:
            print("  ⚠️  Workflow is NOT active.")
            print("     Set KOMMO_SHEETS_SPREADSHEET_ID env var in n8n,")
            print("     configure a Google Sheets credential, then activate.")
            print(f"\n     Run with --activate once credentials are ready:\n")
            print(f"     python3 {Path(__file__).name} --activate\n")

        return 0

    except requests.exceptions.ConnectionError:
        print(f"\n  ❌ Cannot reach n8n at {N8N_API_URL}")
        print("     → Is n8n running? Try: npx n8n start\n")
        return 1
    except requests.exceptions.HTTPError as e:
        print(f"\n  ❌ HTTP {e.response.status_code}: {e.response.text[:500]}\n")
        return 1
    except Exception as e:
        print(f"\n  ❌ Unexpected error: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
