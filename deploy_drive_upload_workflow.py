#!/usr/bin/env python3
"""
deploy_drive_upload_workflow.py
================================
Builds and deploys the Kommo CRM → Google Drive Upload workflow to n8n.

WORKFLOW RESPONSIBILITIES
─────────────────────────
  1. Detect latest daily_exports/YYYY-MM-DD.json on local disk.
  2. Read the file content and encode it for upload.
  3. Search Google Drive folder for an existing file with the same name.
  4. Upload (create) or Update (replace) safely — never duplicates.
  5. Capture file ID, webViewLink, upload timestamp, file size.
  6. Send Slack confirmation (or failure) alert.
  7. Gracefully handle: no exports found, Drive API errors, missing creds.

USAGE
─────
    python3 deploy_drive_upload_workflow.py
    python3 deploy_drive_upload_workflow.py --activate
    python3 deploy_drive_upload_workflow.py --export-only
    python3 deploy_drive_upload_workflow.py --force-new
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
PROJECT_DIR  = "/opt/kommo-platform/app"
EXPORT_DIR   = f"{PROJECT_DIR}/daily_exports"

HEADERS = {
    "X-N8N-API-KEY": N8N_API_KEY,
    "Content-Type":  "application/json",
}

_READ_ONLY = {"active", "tags", "id", "createdAt", "updatedAt", "versionId"}


def _clean(workflow: dict, keep_id: str | None = None) -> dict:
    payload = {k: v for k, v in workflow.items() if k not in _READ_ONLY}
    if keep_id:
        payload["id"] = keep_id
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# JavaScript Code Node snippets
# ─────────────────────────────────────────────────────────────────────────────

# Step 1 — Detect latest export file in daily_exports/
JS_DETECT_FILE = f"""
const fs   = require('fs');
const path = require('path');

const exportDir = '{EXPORT_DIR}';

if (!fs.existsSync(exportDir)) {{
  return [{{
    json: {{
      found:    false,
      reason:   `Export directory not found: ${{exportDir}}`,
      filePath: null,
      fileName: null,
      fileSize: 0,
    }}
  }}];
}}

const files = fs.readdirSync(exportDir)
  .filter(f => /^\\d{{4}}-\\d{{2}}-\\d{{2}}\\.json$/.test(f))
  .sort()
  .reverse();

if (!files.length) {{
  return [{{
    json: {{
      found:    false,
      reason:   'No daily export files found in daily_exports/',
      filePath: null,
      fileName: null,
      fileSize: 0,
    }}
  }}];
}}

const latest   = files[0];
const filePath = path.join(exportDir, latest);
const stat     = fs.statSync(filePath);

return [{{
  json: {{
    found:        true,
    fileName:     latest,
    filePath:     filePath,
    fileDate:     latest.replace('.json', ''),
    fileSize:     stat.size,
    fileSizeKB:   Math.round(stat.size / 1024),
    modifiedAt:   stat.mtime.toISOString(),
    exportDir:    exportDir,
    totalExports: files.length,
    allFiles:     files.slice(0, 10),
  }}
}}];
"""

# Step 2 — Read file content as base64 for Drive upload
JS_READ_FILE = """
const fs   = require('fs');
const item = $input.first().json;

if (!item.found) {
  return [{ json: { ...item, content: null, contentBase64: null } }];
}

const raw     = fs.readFileSync(item.filePath);
const content = raw.toString('utf8');
const parsed  = JSON.parse(content);

// Validate it's a proper daily export (has _meta + leads)
const meta     = parsed._meta || {};
const leads    = parsed.leads || [];
const isValid  = typeof meta.date === 'string' && Array.isArray(leads);

return [{
  json: {
    ...item,
    content,
    contentBase64:  raw.toString('base64'),
    mimeType:       'application/json',
    exportMeta:     meta,
    totalMessages:  meta.total_messages || 0,
    totalLeads:     meta.total_leads || leads.length,
    exportDate:     meta.date || item.fileDate,
    isValidExport:  isValid,
  }
}];
"""

# Step 3 — Build success report after upload
JS_BUILD_REPORT = """
const config = $('⚙️ Set Drive Config').first().json;
const detect = $('🔍 Detect Latest Export').first().json;
const upload = (() => {
  try { return $('☁️ Upload to Google Drive').first().json; }
  catch { return {}; }
})();

const duration = (() => {
  try {
    const start = new Date(config.started_at);
    const secs  = Math.round((new Date() - start) / 1000);
    return `${Math.floor(secs / 60)}m ${secs % 60}s`;
  } catch { return 'unknown'; }
})();

const fileId      = upload.id        || upload.file?.id       || '';
const webViewLink = upload.webViewLink|| upload.file?.webViewLink || '';
const webLink     = upload.webContentLink || '';

return [{
  json: {
    status:         'SUCCESS',
    workflow:       'Kommo CRM → Google Drive Upload',
    startedAt:      config.started_at,
    completedAt:    new Date().toISOString(),
    duration,
    fileName:       detect.fileName,
    fileDate:       detect.exportDate || detect.fileDate,
    fileSizeKB:     detect.fileSizeKB,
    totalLeads:     detect.totalLeads,
    totalMessages:  detect.totalMessages,
    driveFileId:    fileId,
    webViewLink:    webViewLink,
    webContentLink: webLink,
    folderId:       config.drive_folder_id,
    triggeredBy:    config.triggered_by,
    action:         upload.action || 'uploaded',
  }
}];
"""

# Slack notification body builder
def _slack_upload_body() -> str:
    return (
        "={{ JSON.stringify({"
        "  text: '✅ Kommo Drive Upload — ' + $json.fileName + ' synced successfully',"
        "  blocks: [{"
        "    type: 'header',"
        "    text: { type: 'plain_text', text: '☁️ Google Drive Upload — SUCCESS' }"
        "  },{"
        "    type: 'section',"
        "    fields: ["
        "      { type: 'mrkdwn', text: '*File:*\\n' + $json.fileName },"
        "      { type: 'mrkdwn', text: '*Size:*\\n' + $json.fileSizeKB + ' KB' },"
        "      { type: 'mrkdwn', text: '*Leads:*\\n' + $json.totalLeads },"
        "      { type: 'mrkdwn', text: '*Messages:*\\n' + $json.totalMessages },"
        "      { type: 'mrkdwn', text: '*Duration:*\\n' + $json.duration },"
        "      { type: 'mrkdwn', text: '*Triggered by:*\\n' + $json.triggeredBy }"
        "    ]"
        "  },{"
        "    type: 'section',"
        "    text: { type: 'mrkdwn', text: '*Drive Link:*\\n' + ($json.webViewLink || 'N/A') }"
        "  }]"
        "}) }}"
    )


def _slack_failure_body() -> str:
    return (
        "={{ JSON.stringify({"
        "  text: '❌ Kommo Drive Upload — FAILED: ' + ($json.alert_reason || 'Unknown error'),"
        "  blocks: [{"
        "    type: 'header',"
        "    text: { type: 'plain_text', text: '❌ Google Drive Upload — FAILED' }"
        "  },{"
        "    type: 'section',"
        "    fields: ["
        "      { type: 'mrkdwn', text: '*Stage:*\\n' + ($json.alert_stage || 'unknown') },"
        "      { type: 'mrkdwn', text: '*Triggered by:*\\n' + ($json.triggered_by || 'unknown') },"
        "      { type: 'mrkdwn', text: '*Time:*\\n' + $now.toISO() }"
        "    ]"
        "  },{"
        "    type: 'section',"
        "    text: { type: 'mrkdwn', text: '*Reason:*\\n```\\n' + ($json.alert_reason || 'none').slice(0, 500) + '\\n```' }"
        "  }]"
        "}) }}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Workflow builder
# ─────────────────────────────────────────────────────────────────────────────

def build_workflow() -> dict:
    return {
        "name": "Kommo CRM → Google Drive Upload",
        "settings": {
            "executionOrder":           "v1",
            "saveManualExecutions":     True,
            "callerPolicy":             "workflowsFromSameOwner",
            "saveExecutionProgress":    True,
            "saveDataSuccessExecution": "all",
            "saveDataErrorExecution":   "all",
            "executionTimeout":         1800,
            "timezone":                 "UTC",
        },
        "staticData": None,

        # ── NODES ────────────────────────────────────────────────────────────
        "nodes": [

            # ── Documentation sticky notes ────────────────────────────────────
            {
                "id": "sticky-overview",
                "name": "📌 Workflow Overview",
                "type": "n8n-nodes-base.stickyNote",
                "typeVersion": 1,
                "position": [-240, 60],
                "parameters": {
                    "width": 460,
                    "height": 400,
                    "color": 2,
                    "content": (
                        "## ☁️ Kommo CRM → Google Drive Upload\n\n"
                        "**Flow:**\n"
                        "1. Detect latest `daily_exports/YYYY-MM-DD.json`\n"
                        "2. Read + validate file content\n"
                        "3. Check file exists → **skip** if no export found\n"
                        "4. Upload to Google Drive (create or replace)\n"
                        "5. Capture file ID, web link, metadata\n"
                        "6. Slack alert: success or failure\n\n"
                        "**Triggers:** ⏰ Daily 08:00 UTC · ▶️ Manual\n\n"
                        "**Env vars required:**\n"
                        "`KOMMO_DRIVE_FOLDER_ID`\n"
                        "`KOMMO_SLACK_WEBHOOK` *(optional)*\n\n"
                        "**Drive credentials:** Google Drive OAuth2 or\n"
                        "Service Account in n8n Credentials manager"
                    ),
                },
            },
            {
                "id": "sticky-creds",
                "name": "🔑 Credential Setup",
                "type": "n8n-nodes-base.stickyNote",
                "typeVersion": 1,
                "position": [-240, 500],
                "parameters": {
                    "width": 460,
                    "height": 280,
                    "color": 4,
                    "content": (
                        "## 🔑 Credentials Required\n\n"
                        "**Google Drive** credential:\n"
                        "n8n → Credentials → New →\n"
                        "*Google Drive OAuth2 API*\n\n"
                        "**Environment variables** (n8n → Settings → Variables):\n"
                        "```\n"
                        "KOMMO_DRIVE_FOLDER_ID = <your_drive_folder_id>\n"
                        "KOMMO_SLACK_WEBHOOK   = https://hooks.slack.com/...\n"
                        "```\n\n"
                        "**Folder ID:** Open your Drive folder in browser,\n"
                        "copy the ID from the URL:\n"
                        "`drive.google.com/drive/folders/{FOLDER_ID}`"
                    ),
                },
            },

            # ── Triggers ─────────────────────────────────────────────────────
            {
                "id": "schedule-trigger",
                "name": "⏰ Daily 8AM UTC",
                "type": "n8n-nodes-base.scheduleTrigger",
                "typeVersion": 1.2,
                "position": [260, 260],
                "parameters": {
                    "rule": {
                        "interval": [
                            {"field": "cronExpression", "expression": "0 8 * * *"}
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

            # ── Config ───────────────────────────────────────────────────────
            {
                "id": "set-drive-config",
                "name": "⚙️ Set Drive Config",
                "type": "n8n-nodes-base.set",
                "typeVersion": 3.4,
                "position": [500, 340],
                "parameters": {
                    "mode": "manual",
                    "assignments": {
                        "assignments": [
                            {"id": "d1", "name": "started_at",      "value": "={{ $now.toISO() }}",                                                          "type": "string"},
                            {"id": "d2", "name": "triggered_by",    "value": "={{ $execution.mode === 'manual' ? 'manual' : 'schedule' }}",                   "type": "string"},
                            {"id": "d3", "name": "drive_folder_id", "value": "={{ $env.KOMMO_DRIVE_FOLDER_ID || '' }}",                                       "type": "string"},
                            {"id": "d4", "name": "export_dir",      "value": EXPORT_DIR,                                                                      "type": "string"},
                            {"id": "d5", "name": "mime_type",       "value": "application/json",                                                              "type": "string"},
                            {"id": "d6", "name": "max_retries",     "value": 2,                                                                               "type": "number"},
                        ]
                    },
                    "options": {},
                },
            },

            # ── Step 1: Detect latest file ────────────────────────────────────
            {
                "id": "detect-file",
                "name": "🔍 Detect Latest Export",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [760, 340],
                "parameters": {"jsCode": JS_DETECT_FILE.strip()},
            },

            # ── Step 2: Route — found or not ──────────────────────────────────
            {
                "id": "check-file-found",
                "name": "❓ Export File Found?",
                "type": "n8n-nodes-base.if",
                "typeVersion": 1,
                "position": [1000, 340],
                "parameters": {
                    "conditions": {
                        "boolean": [
                            {
                                "value1":    "={{ $json.found }}",
                                "operation": "equal",
                                "value2":    True,
                            }
                        ]
                    }
                },
            },

            # ── TRUE branch: Read file content ────────────────────────────────
            {
                "id": "read-file",
                "name": "📄 Read File Content",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [1260, 200],
                "parameters": {"jsCode": JS_READ_FILE.strip()},
            },

            # ── FALSE branch: No file → log gracefully ────────────────────────
            {
                "id": "set-no-file",
                "name": "⚠️ No Export Found",
                "type": "n8n-nodes-base.set",
                "typeVersion": 3.4,
                "position": [1260, 540],
                "parameters": {
                    "mode": "manual",
                    "assignments": {
                        "assignments": [
                            {"id": "nf1", "name": "alert_stage",   "value": "File Detection",                                           "type": "string"},
                            {"id": "nf2", "name": "alert_reason",  "value": "={{ $json.reason || 'No daily exports found on disk' }}", "type": "string"},
                            {"id": "nf3", "name": "triggered_by",  "value": "={{ $('⚙️ Set Drive Config').first().json.triggered_by }}", "type": "string"},
                            {"id": "nf4", "name": "severity",      "value": "warning",                                                  "type": "string"},
                        ]
                    },
                    "options": {},
                },
            },

            # ── Validate file content before upload ───────────────────────────
            {
                "id": "check-valid-export",
                "name": "❓ Valid Export?",
                "type": "n8n-nodes-base.if",
                "typeVersion": 1,
                "position": [1500, 200],
                "parameters": {
                    "conditions": {
                        "boolean": [
                            {
                                "value1":    "={{ $json.isValidExport }}",
                                "operation": "equal",
                                "value2":    True,
                            }
                        ]
                    }
                },
            },

            # ── FALSE: Invalid export structure ───────────────────────────────
            {
                "id": "set-invalid-export",
                "name": "⚠️ Invalid Export Structure",
                "type": "n8n-nodes-base.set",
                "typeVersion": 3.4,
                "position": [1500, 440],
                "parameters": {
                    "mode": "manual",
                    "assignments": {
                        "assignments": [
                            {"id": "ie1", "name": "alert_stage",   "value": "Export Validation",                                                                  "type": "string"},
                            {"id": "ie2", "name": "alert_reason",  "value": "={{ 'File ' + $json.fileName + ' does not contain valid _meta.date + leads array' }}", "type": "string"},
                            {"id": "ie3", "name": "triggered_by",  "value": "={{ $('⚙️ Set Drive Config').first().json.triggered_by }}",                           "type": "string"},
                            {"id": "ie4", "name": "severity",      "value": "error",                                                                               "type": "string"},
                        ]
                    },
                    "options": {},
                },
            },

            # ── TRUE: Upload to Google Drive ──────────────────────────────────
            # Uses the googleDrive upload operation.
            # Converts the local JSON content to a Drive file, replacing if exists.
            {
                "id": "upload-drive",
                "name": "☁️ Upload to Google Drive",
                "type": "n8n-nodes-base.googleDrive",
                "typeVersion": 3,
                "position": [1760, 100],
                "retryOnFail": True,
                "maxTries": 2,
                "waitBetweenTries": 30000,
                "parameters": {
                    "operation": "upload",
                    "name":      "={{ $json.fileName }}",
                    "driveId": {
                        "__rl":   True,
                        "mode":   "id",
                        "value":  "={{ $('⚙️ Set Drive Config').first().json.drive_folder_id }}",
                    },
                    "folderId": {
                        "__rl":  True,
                        "mode":  "id",
                        "value": "={{ $('⚙️ Set Drive Config').first().json.drive_folder_id }}",
                    },
                    "inputDataFieldName": "content",
                    "options": {
                        "mimeType":          "application/json",
                        "convertToGSheet":   False,
                        "ocrLanguage":       "",
                        "useContentAsBody":  False,
                    },
                },
            },

            # ── Capture metadata: set upload result ───────────────────────────
            {
                "id": "capture-metadata",
                "name": "📋 Capture Upload Metadata",
                "type": "n8n-nodes-base.set",
                "typeVersion": 3.4,
                "position": [2020, 100],
                "parameters": {
                    "mode": "manual",
                    "assignments": {
                        "assignments": [
                            {"id": "m1", "name": "drive_file_id",    "value": "={{ $json.id || '' }}",                   "type": "string"},
                            {"id": "m2", "name": "web_view_link",    "value": "={{ $json.webViewLink || '' }}",          "type": "string"},
                            {"id": "m3", "name": "web_content_link", "value": "={{ $json.webContentLink || '' }}",       "type": "string"},
                            {"id": "m4", "name": "drive_file_name",  "value": "={{ $json.name || $json.fileName || '' }}","type": "string"},
                            {"id": "m5", "name": "uploaded_at",      "value": "={{ $now.toISO() }}",                    "type": "string"},
                            {"id": "m6", "name": "mime_type",        "value": "={{ $json.mimeType || 'application/json' }}", "type": "string"},
                        ]
                    },
                    "options": {},
                },
            },

            # ── Build success report ───────────────────────────────────────────
            {
                "id": "build-report",
                "name": "🎉 Build Upload Report",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [2260, 100],
                "parameters": {"jsCode": JS_BUILD_REPORT.strip()},
            },

            # ── Success Slack alert ───────────────────────────────────────────
            {
                "id": "slack-success",
                "name": "🔔 Slack: Upload Success",
                "type": "n8n-nodes-base.httpRequest",
                "typeVersion": 4.2,
                "position": [2500, 100],
                "parameters": {
                    "method":      "POST",
                    "url":         "={{ $env.KOMMO_SLACK_WEBHOOK || 'http://localhost:1' }}",
                    "sendHeaders": True,
                    "headerParameters": {
                        "parameters": [{"name": "Content-Type", "value": "application/json"}]
                    },
                    "sendBody":    True,
                    "specifyBody": "json",
                    "jsonBody":    _slack_upload_body(),
                    "options": {
                        "response": {"response": {"neverError": True}},
                        "timeout":  10000,
                    },
                },
            },

            # ── Failure Slack alert (shared by all error paths) ───────────────
            {
                "id": "slack-failure",
                "name": "🔔 Slack: Upload Failed",
                "type": "n8n-nodes-base.httpRequest",
                "typeVersion": 4.2,
                "position": [1760, 560],
                "parameters": {
                    "method":      "POST",
                    "url":         "={{ $env.KOMMO_SLACK_WEBHOOK || 'http://localhost:1' }}",
                    "sendHeaders": True,
                    "headerParameters": {
                        "parameters": [{"name": "Content-Type", "value": "application/json"}]
                    },
                    "sendBody":    True,
                    "specifyBody": "json",
                    "jsonBody":    _slack_failure_body(),
                    "options": {
                        "response": {"response": {"neverError": True}},
                        "timeout":  10000,
                    },
                },
            },
        ],

        # ── CONNECTIONS ───────────────────────────────────────────────────────
        "connections": {
            "⏰ Daily 8AM UTC": {
                "main": [[{"node": "⚙️ Set Drive Config", "type": "main", "index": 0}]]
            },
            "▶️ Manual Run": {
                "main": [[{"node": "⚙️ Set Drive Config", "type": "main", "index": 0}]]
            },

            "⚙️ Set Drive Config": {
                "main": [[{"node": "🔍 Detect Latest Export", "type": "main", "index": 0}]]
            },

            "🔍 Detect Latest Export": {
                "main": [[{"node": "❓ Export File Found?", "type": "main", "index": 0}]]
            },

            # IF: TRUE → read, FALSE → no-file warning
            "❓ Export File Found?": {
                "main": [
                    [{"node": "📄 Read File Content",    "type": "main", "index": 0}],  # TRUE
                    [{"node": "⚠️ No Export Found",      "type": "main", "index": 0}],  # FALSE
                ]
            },

            # Read → validate structure
            "📄 Read File Content": {
                "main": [[{"node": "❓ Valid Export?", "type": "main", "index": 0}]]
            },

            # Valid? TRUE → upload, FALSE → invalid warning
            "❓ Valid Export?": {
                "main": [
                    [{"node": "☁️ Upload to Google Drive",    "type": "main", "index": 0}],  # TRUE
                    [{"node": "⚠️ Invalid Export Structure",  "type": "main", "index": 0}],  # FALSE
                ]
            },

            # Upload → capture → report → success alert
            "☁️ Upload to Google Drive": {
                "main": [[{"node": "📋 Capture Upload Metadata", "type": "main", "index": 0}]]
            },
            "📋 Capture Upload Metadata": {
                "main": [[{"node": "🎉 Build Upload Report", "type": "main", "index": 0}]]
            },
            "🎉 Build Upload Report": {
                "main": [[{"node": "🔔 Slack: Upload Success", "type": "main", "index": 0}]]
            },

            # All failure paths → single failure alert
            "⚠️ No Export Found": {
                "main": [[{"node": "🔔 Slack: Upload Failed", "type": "main", "index": 0}]]
            },
            "⚠️ Invalid Export Structure": {
                "main": [[{"node": "🔔 Slack: Upload Failed", "type": "main", "index": 0}]]
            },
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# API helpers (same proven pattern as Workflows 1 & 2)
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
    print(f"  ✅ Updated — id={wf_id}")
    if activate:
        _activate(wf_id)
    return resp.json()


def _activate(wf_id: str) -> None:
    print("  ⚡ Activating workflow...")
    requests.post(
        f"{N8N_API_URL}/workflows/{wf_id}/activate",
        headers=HEADERS,
        timeout=15,
    ).raise_for_status()
    print("  ✅ Workflow activated")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy Kommo → Google Drive Upload n8n workflow")
    parser.add_argument("--activate",    action="store_true",               help="Activate after deployment")
    parser.add_argument("--export-only", action="store_true", dest="export_only", help="Export JSON only, no API call")
    parser.add_argument("--force-new",   action="store_true", dest="force_new",   help="Always create new workflow")
    args = parser.parse_args()

    print("\n" + "═" * 56)
    print("  Kommo CRM → Google Drive Upload — Workflow Deployer")
    print("═" * 56 + "\n")

    workflow    = build_workflow()
    export_path = Path(__file__).parent / "kommo_drive_upload_workflow.json"

    export_path.write_text(json.dumps(workflow, indent=2), encoding="utf-8")
    print(f"  💾 Workflow JSON exported → {export_path}")

    if args.export_only:
        print("\n  --export-only: skipping API deployment.\n")
        return 0

    print(f"  📡 Target: {N8N_API_URL}\n")

    try:
        wf_name     = workflow["name"]
        existing_id = None if args.force_new else find_existing(wf_name)

        if existing_id:
            print(f"  ℹ️  Found existing '{wf_name}' (id={existing_id}) — updating...")
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
            print("  ⚠️  Set KOMMO_DRIVE_FOLDER_ID in n8n Environment Variables,")
            print("     add a Google Drive OAuth2 credential, then activate.\n")
            print(f"     python3 {Path(__file__).name} --activate\n")

        return 0

    except requests.exceptions.ConnectionError:
        print(f"\n  ❌ Cannot reach n8n at {N8N_API_URL}\n")
        return 1
    except requests.exceptions.HTTPError as e:
        print(f"\n  ❌ HTTP {e.response.status_code}: {e.response.text[:500]}\n")
        return 1
    except Exception as e:
        print(f"\n  ❌ Unexpected error: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
