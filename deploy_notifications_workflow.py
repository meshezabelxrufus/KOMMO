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
N8N_API_KEY  = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIzZjZmMjdhNC02MzVjLTQ1NWItYWQzNS00YjJmNzM1YzIwZmMiLCJpc3MiOiJuOG4i"
    "LCJhdWQiOiJwdWJsaWMtYXBpIiwianRpIjoiODgxYzM5MmEtYWMyMy00NDA1LWJmOTUtZmIxY2E3"
    "OGI5ZjkzIiwiaWF0IjoxNzc4Njc1MTI3fQ.A5hIJOhfpqHy2Xk2mnZDEgeQLLcLjQxTC7HOx4S52e4"
)
PROJECT_DIR = "/Users/abdulwaseyhussain/Downloads/KOMMO"

HEADERS = {"X-N8N-API-KEY": N8N_API_KEY, "Content-Type": "application/json"}
_READ_ONLY = {"active", "tags", "id", "createdAt", "updatedAt", "versionId"}

def _clean(wf: dict, keep_id: str | None = None) -> dict:
    p = {k: v for k, v in wf.items() if k not in _READ_ONLY}
    if keep_id:
        p["id"] = keep_id
    return p


# ─────────────────────────────────────────────────────────────────────────────
# Severity emoji / color maps
# ─────────────────────────────────────────────────────────────────────────────

SEVERITY_EMOJI = {
    "critical": "🚨",
    "warning":  "⚠️",
    "info":     "ℹ️",
}

ALERT_TYPE_EMOJI = {
    "extraction_failure": "💥",
    "google_failure":     "☁️❌",
    "claude_failure":     "🤖❌",
    "missing_export":     "📭",
    "validation_error":   "🔍❌",
    "daily_summary":      "📊",
    "urgent_lead":        "🔴",
    "operational_alert":  "📡",
}

# ─────────────────────────────────────────────────────────────────────────────
# JavaScript Code Nodes
# ─────────────────────────────────────────────────────────────────────────────

# 1. Validate and normalise incoming webhook payload
JS_VALIDATE_PAYLOAD = """
const body = $input.first().json;

const VALID_TYPES = [
  'extraction_failure','google_failure','claude_failure',
  'missing_export','validation_error','daily_summary',
  'urgent_lead','operational_alert'
];
const VALID_SEVERITIES = ['critical','warning','info'];

const alert_type = body.alert_type || 'operational_alert';
const severity   = VALID_SEVERITIES.includes(body.severity) ? body.severity : 'warning';
const title      = body.title   || `${alert_type.replace(/_/g,' ').toUpperCase()} alert`;
const message    = body.message || 'No details provided.';
const source     = body.source  || 'Unknown workflow';
const lead_id    = body.lead_id    || null;
const lead_name  = body.lead_name  || null;
const details    = body.details    || {};
const triggered_by = body.triggered_by || 'webhook';
const ts         = new Date().toISOString();

// Build deduplication fingerprint
const fingerprint = `${alert_type}::${severity}::${source}::${lead_id || 'none'}`;
const fingerprintHash = fingerprint.split('').reduce((a, c) => ((a << 5) - a + c.charCodeAt(0)) | 0, 0);

return [{
  json: {
    alert_type,
    severity,
    title,
    message,
    source,
    lead_id,
    lead_name,
    details,
    triggered_by,
    received_at:   ts,
    fingerprint,
    fingerprintHash: String(Math.abs(fingerprintHash)),
    isValidType:   VALID_TYPES.includes(alert_type),
  }
}];
"""

# 2. Deduplication check — suppress same alert within 60 minutes
JS_DEDUP_CHECK = """
// n8n staticData persists across executions within a workflow
const store    = $getWorkflowStaticData('global');
const incoming = $input.first().json;
const fp       = incoming.fingerprintHash;
const now      = Date.now();
const TTL_MS   = 60 * 60 * 1000; // 60 min

if (!store.dedupMap) store.dedupMap = {};

// Purge old entries
for (const key of Object.keys(store.dedupMap)) {
  if (now - store.dedupMap[key] > TTL_MS) delete store.dedupMap[key];
}

const lastSent   = store.dedupMap[fp] || 0;
const isDuplicate = (now - lastSent) < TTL_MS && incoming.severity !== 'critical';
// Critical alerts ALWAYS go through regardless of dedup

if (!isDuplicate) {
  store.dedupMap[fp] = now;
}

return [{
  json: {
    ...incoming,
    isDuplicate,
    lastSentAgo: lastSent ? Math.round((now - lastSent) / 60000) + ' min ago' : 'never',
  }
}];
"""

# 3. Build Slack Block Kit payload — severity-aware rich formatting
JS_BUILD_SLACK = """
const a = $input.first().json;

const emojiMap = {
  critical: '🚨', warning: '⚠️', info: 'ℹ️'
};
const typeEmojiMap = {
  extraction_failure: '💥', google_failure: '☁️❌', claude_failure: '🤖❌',
  missing_export: '📭', validation_error: '🔍❌', daily_summary: '📊',
  urgent_lead: '🔴', operational_alert: '📡'
};
const colorMap = { critical: '#FF0000', warning: '#FFA500', info: '#0078D4' };

const sev   = emojiMap[a.severity]  || '📢';
const type  = typeEmojiMap[a.alert_type] || '📢';
const color = colorMap[a.severity]  || '#888888';

const header = `${sev} ${type}  ${a.title}`;

const fields = [
  { type: 'mrkdwn', text: `*Severity:*\\n${a.severity.toUpperCase()}` },
  { type: 'mrkdwn', text: `*Alert Type:*\\n${a.alert_type.replace(/_/g,' ')}` },
  { type: 'mrkdwn', text: `*Source:*\\n${a.source}` },
  { type: 'mrkdwn', text: `*Triggered by:*\\n${a.triggered_by}` },
  { type: 'mrkdwn', text: `*Time:*\\n${a.received_at}` },
];

if (a.lead_name) fields.push({ type: 'mrkdwn', text: `*Lead:*\\n${a.lead_name} (${a.lead_id || '?'})` });

const detailText = Object.keys(a.details || {}).length
  ? '```' + JSON.stringify(a.details, null, 2).slice(0, 800) + '```'
  : a.message.slice(0, 1000);

const blocks = [
  { type: 'header', text: { type: 'plain_text', text: header.slice(0, 150) } },
  { type: 'section', fields },
  { type: 'section', text: { type: 'mrkdwn', text: `*Details:*\\n${detailText}` } },
  { type: 'divider' },
  { type: 'context', elements: [{ type: 'mrkdwn', text: `Kommo Notifications Hub • ${a.received_at}` }] },
];

return [{
  json: {
    ...a,
    slackPayload: JSON.stringify({ text: header, blocks }),
  }
}];
"""

# 4. Build Telegram message (Markdown)
JS_BUILD_TELEGRAM = """
const a = $input.first().json;

const sev  = { critical:'🚨', warning:'⚠️', info:'ℹ️' }[a.severity] || '📢';
const type = {
  extraction_failure:'💥',google_failure:'☁️❌',claude_failure:'🤖❌',
  missing_export:'📭',validation_error:'🔍❌',daily_summary:'📊',
  urgent_lead:'🔴',operational_alert:'📡'
}[a.alert_type] || '📢';

let text = `${sev} *${a.title}*\\n`;
text += `\\n*Type:* ${a.alert_type.replace(/_/g,' ')}`;
text += `\\n*Severity:* ${a.severity.toUpperCase()}`;
text += `\\n*Source:* ${a.source}`;
if (a.lead_name) text += `\\n*Lead:* ${a.lead_name}`;
text += `\\n\\n${a.message.slice(0, 600)}`;
if (a.triggered_by) text += `\\n\\n_Triggered by: ${a.triggered_by}_`;

return [{ json: { ...a, telegramText: text } }];
"""

# 5. Build Email HTML body
JS_BUILD_EMAIL = """
const a = $input.first().json;

const colorMap = { critical: '#d32f2f', warning: '#f57c00', info: '#1565c0' };
const color = colorMap[a.severity] || '#333';

const detailRows = Object.entries(a.details || {})
  .map(([k,v]) => `<tr><td style="padding:4px 8px;font-weight:bold;color:#555">${k}</td><td style="padding:4px 8px">${String(v).slice(0,200)}</td></tr>`)
  .join('');

const html = `
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;padding:16px">
  <div style="background:${color};color:white;padding:16px 20px;border-radius:6px 6px 0 0">
    <h2 style="margin:0">${a.severity.toUpperCase()} — ${a.title}</h2>
  </div>
  <div style="border:1px solid #ddd;border-top:none;padding:20px;border-radius:0 0 6px 6px">
    <table style="width:100%;border-collapse:collapse;margin-bottom:16px">
      <tr><td style="padding:4px 8px;font-weight:bold;color:#555">Alert Type</td><td>${a.alert_type}</td></tr>
      <tr><td style="padding:4px 8px;font-weight:bold;color:#555">Source</td><td>${a.source}</td></tr>
      ${a.lead_name ? `<tr><td style="padding:4px 8px;font-weight:bold;color:#555">Lead</td><td>${a.lead_name} (${a.lead_id})</td></tr>` : ''}
      <tr><td style="padding:4px 8px;font-weight:bold;color:#555">Time</td><td>${a.received_at}</td></tr>
      ${detailRows}
    </table>
    <div style="background:#f5f5f5;padding:12px;border-radius:4px;font-family:monospace;white-space:pre-wrap">${a.message.slice(0,1500)}</div>
    <p style="color:#999;font-size:12px;margin-top:16px">Kommo CRM Notification Hub — automated alert</p>
  </div>
</body>
</html>`;

return [{ json: { ...a, emailSubject: `[${a.severity.toUpperCase()}] ${a.title}`, emailHtml: html } }];
"""

# 6. Load daily operational summary from analytics log
JS_LOAD_DAILY_SUMMARY = f"""
const fs   = require('fs');
const path = require('path');
const today = new Date().toISOString().split('T')[0];
const logPath = path.join('{PROJECT_DIR}', 'logs', `analytics_${{today}}.json`);

if (!fs.existsSync(logPath)) {{
  return [{{
    json: {{
      alert_type:  'daily_summary',
      severity:    'info',
      title:       `No analytics log for ${{today}}`,
      message:     'Pipeline may not have run today yet, or analytics step was skipped.',
      source:      'Notifications Hub (cron)',
      triggered_by:'schedule',
      received_at: new Date().toISOString(),
      details:     {{ date: today, log_path: logPath }},
    }}
  }}];
}}

const raw    = JSON.parse(fs.readFileSync(logPath, 'utf8'));
const meta   = raw._meta   || {{}};
const phases = raw.phases  || {{}};
const steps  = raw.steps   || [];

const failedSteps = steps.filter(s => s.status === 'FAILED' || s.status === 'ERROR');
const severity    = failedSteps.length > 0 ? 'warning' : 'info';

const phaseLines = Object.entries(phases)
  .map(([name, p]) => `${{name}}: ${{p.status}} (${{p.records || 0}} records, ${{p.duration_s || 0}}s)`)
  .join('\\n');

const failLines = failedSteps.length
  ? '\\n\\nFailed steps:\\n' + failedSteps.map(s => `  • ${{s.name}}: ${{s.status}}`).join('\\n')
  : '';

return [{{
  json: {{
    alert_type:    'daily_summary',
    severity,
    title:         `Daily Pipeline Summary — ${{today}}`,
    message:       `Status: ${{meta.overall_status || 'UNKNOWN'}} | Mode: ${{meta.pipeline_mode || '?'}} | Duration: ${{meta.total_duration_s || 0}}s\\n\\n${{phaseLines}}${{failLines}}`,
    source:        'Notifications Hub (cron)',
    triggered_by:  'schedule',
    received_at:   new Date().toISOString(),
    details: {{
      date:              today,
      overall_status:    meta.overall_status,
      pipeline_mode:     meta.pipeline_mode,
      total_duration_s:  meta.total_duration_s,
      failed_steps:      failedSteps.length,
      total_steps:       steps.length,
    }},
    fingerprint:     `daily_summary::${{today}}`,
    fingerprintHash: String(Math.abs(today.split('').reduce((a,c) => ((a<<5)-a+c.charCodeAt(0))|0, 0))),
    isDuplicate:     false,
    lastSentAgo:     'never',
  }}
}}];
"""


# ─────────────────────────────────────────────────────────────────────────────
# Workflow builder
# ─────────────────────────────────────────────────────────────────────────────

def build_workflow() -> dict:
    return {
        "name": "Kommo CRM — Notifications Hub",
        "settings": {
            "executionOrder":           "v1",
            "saveManualExecutions":     True,
            "callerPolicy":             "workflowsFromSameOwner",
            "saveExecutionProgress":    True,
            "saveDataSuccessExecution": "all",
            "saveDataErrorExecution":   "all",
            "executionTimeout":         300,
            "timezone":                 "UTC",
        },
        "staticData": None,

        "nodes": [

            # ── Sticky notes ──────────────────────────────────────────────────
            {
                "id": "sticky-overview",
                "name": "📌 Hub Overview",
                "type": "n8n-nodes-base.stickyNote",
                "typeVersion": 1,
                "position": [-280, 60],
                "parameters": {
                    "width": 500, "height": 540, "color": 2,
                    "content": (
                        "## 🔔 Kommo Notifications Hub\n\n"
                        "**Central alert dispatcher for all 4 workflows.**\n"
                        "Other workflows POST to this webhook instead\n"
                        "of directly calling Slack/Telegram/Email.\n\n"
                        "**Inbound triggers:**\n"
                        "- Webhook POST from any pipeline workflow\n"
                        "- Cron: 10:00 UTC daily operational summary\n"
                        "- Manual test run\n\n"
                        "**Routing logic:**\n"
                        "- `critical` → Slack + Telegram + Email\n"
                        "- `warning`  → Slack + Telegram\n"
                        "- `info`     → Slack only\n"
                        "- `urgent_lead` → Slack + Telegram (always)\n\n"
                        "**Deduplication:** Same alert fingerprint\n"
                        "suppressed for 60 minutes (except critical).\n\n"
                        "**Required env vars:**\n"
                        "`KOMMO_SLACK_WEBHOOK`\n"
                        "`KOMMO_TELEGRAM_BOT_TOKEN`\n"
                        "`KOMMO_TELEGRAM_CHAT_ID`\n"
                        "`KOMMO_ALERT_EMAIL_TO`\n"
                        "Gmail credential in n8n Credentials"
                    ),
                },
            },
            {
                "id": "sticky-payload",
                "name": "📋 Payload Schema",
                "type": "n8n-nodes-base.stickyNote",
                "typeVersion": 1,
                "position": [-280, 640],
                "parameters": {
                    "width": 500, "height": 340, "color": 4,
                    "content": (
                        "## 📋 POST Payload Schema\n\n"
                        "```json\n"
                        "{\n"
                        '  "alert_type": "extraction_failure",\n'
                        '  "severity":   "critical",\n'
                        '  "title":      "Kommo extraction failed",\n'
                        '  "message":    "Full error message here",\n'
                        '  "source":     "Workflow 1 — Daily Pipeline",\n'
                        '  "lead_id":    "12345",\n'
                        '  "lead_name":  "John Doe",\n'
                        '  "details":    { "step": "leads", "error": "..." },\n'
                        '  "triggered_by": "schedule"\n'
                        "}\n"
                        "```\n\n"
                        "**Alert types:** `extraction_failure` `google_failure`\n"
                        "`claude_failure` `missing_export` `validation_error`\n"
                        "`daily_summary` `urgent_lead` `operational_alert`"
                    ),
                },
            },

            # ── Triggers (3) ──────────────────────────────────────────────────
            {
                "id": "webhook-trigger",
                "name": "📥 Webhook: Receive Alert",
                "type": "n8n-nodes-base.webhook",
                "typeVersion": 2,
                "position": [280, 200],
                "webhookId": "kommo-notifications-hub",
                "parameters": {
                    "httpMethod":       "POST",
                    "path":             "kommo-alert",
                    "responseMode":     "responseNode",
                    "responseData":     "allEntries",
                    "options": {
                        "rawBody":      False,
                        "allowedOrigins": "*",
                    },
                },
            },
            {
                "id": "schedule-daily",
                "name": "⏰ Daily 10AM UTC Summary",
                "type": "n8n-nodes-base.scheduleTrigger",
                "typeVersion": 1.2,
                "position": [280, 380],
                "parameters": {
                    "rule": {"interval": [{"field": "cronExpression", "expression": "0 10 * * *"}]}
                },
            },
            {
                "id": "manual-trigger",
                "name": "▶️ Manual Test",
                "type": "n8n-nodes-base.manualTrigger",
                "typeVersion": 1,
                "position": [280, 560],
                "parameters": {},
            },

            # ── Respond immediately to webhook caller ─────────────────────────
            {
                "id": "webhook-respond",
                "name": "✅ Respond 202 Accepted",
                "type": "n8n-nodes-base.respondToWebhook",
                "typeVersion": 1.1,
                "position": [560, 200],
                "parameters": {
                    "respondWith":   "json",
                    "responseBody":  '={{ JSON.stringify({ status: "accepted", ts: $now.toISO() }) }}',
                    "options": {
                        "responseCode": 202,
                        "responseHeaders": {
                            "entries": [{"name": "Content-Type", "value": "application/json"}]
                        },
                    },
                },
            },

            # ── Route source ──────────────────────────────────────────────────
            # Switch: webhook path vs schedule/manual path
            {
                "id": "route-source",
                "name": "🔀 Route: Webhook vs Cron",
                "type": "n8n-nodes-base.switch",
                "typeVersion": 3,
                "position": [560, 440],
                "parameters": {
                    "mode":   "expression",
                    "output": "={{ $execution.mode === 'webhook' ? 0 : 1 }}",
                    "outputsAmount": 2,
                    "options": {"fallbackOutput": 1},
                },
            },

            # ── Load daily summary (cron/manual path) ─────────────────────────
            {
                "id": "load-daily",
                "name": "📂 Load Daily Analytics Log",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [820, 560],
                "parameters": {"jsCode": JS_LOAD_DAILY_SUMMARY.strip()},
            },

            # ── Validate + normalize payload ──────────────────────────────────
            {
                "id": "validate-payload",
                "name": "🔬 Validate & Normalise Payload",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [820, 380],
                "parameters": {"jsCode": JS_VALIDATE_PAYLOAD.strip()},
            },

            # ── Merge both paths ──────────────────────────────────────────────
            {
                "id": "merge-paths",
                "name": "🔗 Merge Alert Paths",
                "type": "n8n-nodes-base.merge",
                "typeVersion": 3,
                "position": [1080, 440],
                "parameters": {
                    "mode":    "passThrough",
                    "output":  "input1",
                    "options": {},
                },
            },

            # ── Deduplication ─────────────────────────────────────────────────
            {
                "id": "dedup-check",
                "name": "🛡️ Deduplication Check",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [1340, 440],
                "parameters": {"jsCode": JS_DEDUP_CHECK.strip()},
            },

            # ── Gate: suppress duplicate? ─────────────────────────────────────
            {
                "id": "check-duplicate",
                "name": "❓ Is Duplicate? (suppress)",
                "type": "n8n-nodes-base.if",
                "typeVersion": 1,
                "position": [1600, 440],
                "parameters": {
                    "conditions": {
                        "boolean": [{"value1": "={{ $json.isDuplicate }}", "operation": "equal", "value2": True}]
                    }
                },
            },

            # TRUE (duplicate) → log and stop
            {
                "id": "log-suppressed",
                "name": "🔕 Log Suppressed Alert",
                "type": "n8n-nodes-base.set",
                "typeVersion": 3.4,
                "position": [1600, 660],
                "parameters": {
                    "mode": "manual",
                    "assignments": {
                        "assignments": [
                            {"id": "s1", "name": "suppressed",   "value": True,                                                         "type": "boolean"},
                            {"id": "s2", "name": "reason",       "value": "={{ 'Duplicate suppressed — last sent ' + $json.lastSentAgo }}", "type": "string"},
                            {"id": "s3", "name": "fingerprint",  "value": "={{ $json.fingerprint }}",                                   "type": "string"},
                            {"id": "s4", "name": "alert_type",   "value": "={{ $json.alert_type }}",                                    "type": "string"},
                        ]
                    },
                    "options": {},
                },
            },

            # FALSE (not duplicate) → build messages
            # Build all 3 message formats in parallel via Code nodes
            {
                "id": "build-slack-msg",
                "name": "🔧 Build Slack Message",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [1860, 280],
                "parameters": {"jsCode": JS_BUILD_SLACK.strip()},
            },
            {
                "id": "build-telegram-msg",
                "name": "🔧 Build Telegram Message",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [1860, 440],
                "parameters": {"jsCode": JS_BUILD_TELEGRAM.strip()},
            },
            {
                "id": "build-email-msg",
                "name": "🔧 Build Email Body",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [1860, 600],
                "parameters": {"jsCode": JS_BUILD_EMAIL.strip()},
            },

            # ── Channel routing by severity ───────────────────────────────────
            # SLACK — always sent (all severities)
            {
                "id": "send-slack",
                "name": "🔔 Send Slack Alert",
                "type": "n8n-nodes-base.httpRequest",
                "typeVersion": 4.2,
                "position": [2120, 280],
                "retryOnFail": True,
                "maxTries": 3,
                "waitBetweenTries": 10000,
                "parameters": {
                    "method":      "POST",
                    "url":         "={{ $env.KOMMO_SLACK_WEBHOOK || 'http://localhost:1' }}",
                    "sendHeaders": True,
                    "headerParameters": {
                        "parameters": [{"name": "Content-Type", "value": "application/json"}]
                    },
                    "sendBody":    True,
                    "specifyBody": "json",
                    "jsonBody":    "={{ $json.slackPayload }}",
                    "options": {
                        "response": {"response": {"neverError": True}},
                        "timeout":  15000,
                    },
                },
            },

            # TELEGRAM severity gate — skip info-only alerts
            {
                "id": "check-telegram-severity",
                "name": "❓ Telegram Threshold Met?",
                "type": "n8n-nodes-base.if",
                "typeVersion": 1,
                "position": [2120, 440],
                "parameters": {
                    "conditions": {
                        "string": [
                            {
                                "value1":    "={{ $json.severity + '|' + $json.alert_type }}",
                                "operation": "regex",
                                "value2":    "^(critical|warning)|urgent_lead",
                            }
                        ]
                    }
                },
            },
            {
                "id": "send-telegram",
                "name": "📱 Send Telegram Alert",
                "type": "n8n-nodes-base.httpRequest",
                "typeVersion": 4.2,
                "position": [2380, 380],
                "retryOnFail": True,
                "maxTries": 3,
                "waitBetweenTries": 10000,
                "parameters": {
                    "method":      "POST",
                    "url":         "={{ 'https://api.telegram.org/bot' + ($env.KOMMO_TELEGRAM_BOT_TOKEN || 'placeholder') + '/sendMessage' }}",
                    "sendHeaders": True,
                    "headerParameters": {
                        "parameters": [{"name": "Content-Type", "value": "application/json"}]
                    },
                    "sendBody":    True,
                    "specifyBody": "json",
                    "jsonBody": (
                        "={{ JSON.stringify({"
                        "  chat_id: $env.KOMMO_TELEGRAM_CHAT_ID || '0',"
                        "  parse_mode: 'Markdown',"
                        "  text: $json.telegramText"
                        "}) }}"
                    ),
                    "options": {
                        "response": {"response": {"neverError": True}},
                        "timeout":  15000,
                    },
                },
            },

            # EMAIL severity gate — critical only
            {
                "id": "check-email-severity",
                "name": "❓ Email Threshold Met?",
                "type": "n8n-nodes-base.if",
                "typeVersion": 1,
                "position": [2120, 600],
                "parameters": {
                    "conditions": {
                        "string": [
                            {"value1": "={{ $json.severity }}", "operation": "equal", "value2": "critical"}
                        ]
                    }
                },
            },
            {
                "id": "send-email",
                "name": "📧 Send Email Alert (Gmail)",
                "type": "n8n-nodes-base.gmail",
                "typeVersion": 2.1,
                "position": [2380, 560],
                "retryOnFail": True,
                "maxTries": 2,
                "waitBetweenTries": 15000,
                "parameters": {
                    "sendTo":   "={{ $env.KOMMO_ALERT_EMAIL_TO || '' }}",
                    "subject":  "={{ $json.emailSubject }}",
                    "emailType":"html",
                    "message":  "={{ $json.emailHtml }}",
                    "options": {
                        "appendAttribution": False,
                    },
                },
            },

            # ── Escalation: CRITICAL → immediate second Telegram push ──────────
            {
                "id": "check-escalate",
                "name": "❓ Escalate? (critical only)",
                "type": "n8n-nodes-base.if",
                "typeVersion": 1,
                "position": [2640, 440],
                "parameters": {
                    "conditions": {
                        "string": [
                            {"value1": "={{ $json.severity }}", "operation": "equal", "value2": "critical"}
                        ]
                    }
                },
            },
            {
                "id": "wait-escalation",
                "name": "⏱️ Escalation Delay (5 min)",
                "type": "n8n-nodes-base.wait",
                "typeVersion": 1.1,
                "position": [2900, 380],
                "parameters": {
                    "resume": "timeInterval",
                    "amount": 5,
                    "unit":   "minutes",
                },
            },
            {
                "id": "escalation-telegram",
                "name": "🚨 Escalation: Repeat Telegram",
                "type": "n8n-nodes-base.httpRequest",
                "typeVersion": 4.2,
                "position": [3160, 380],
                "parameters": {
                    "method":      "POST",
                    "url":         "={{ 'https://api.telegram.org/bot' + ($env.KOMMO_TELEGRAM_BOT_TOKEN || 'placeholder') + '/sendMessage' }}",
                    "sendHeaders": True,
                    "headerParameters": {
                        "parameters": [{"name": "Content-Type", "value": "application/json"}]
                    },
                    "sendBody":    True,
                    "specifyBody": "json",
                    "jsonBody": (
                        "={{ JSON.stringify({"
                        "  chat_id: $env.KOMMO_TELEGRAM_CHAT_ID || '0',"
                        "  parse_mode: 'Markdown',"
                        "  text: '🚨 *ESCALATION REPEAT* — ' + $json.title + '\\n\\nThis CRITICAL alert has not been acknowledged.\\n\\n' + $json.message.slice(0,400)"
                        "}) }}"
                    ),
                    "options": {
                        "response": {"response": {"neverError": True}},
                        "timeout": 15000,
                    },
                },
            },
        ],

        # ── CONNECTIONS ───────────────────────────────────────────────────────
        "connections": {
            # Webhook path: respond immediately, then validate
            "📥 Webhook: Receive Alert": {
                "main": [[
                    {"node": "✅ Respond 202 Accepted", "type": "main", "index": 0},
                    {"node": "🔀 Route: Webhook vs Cron", "type": "main", "index": 0},
                ]]
            },
            # Schedule and manual go directly to router
            "⏰ Daily 10AM UTC Summary": {
                "main": [[{"node": "🔀 Route: Webhook vs Cron", "type": "main", "index": 0}]]
            },
            "▶️ Manual Test": {
                "main": [[{"node": "🔀 Route: Webhook vs Cron", "type": "main", "index": 0}]]
            },

            # Router: 0 = webhook → validate, 1 = cron/manual → load daily
            "🔀 Route: Webhook vs Cron": {
                "main": [
                    [{"node": "🔬 Validate & Normalise Payload", "type": "main", "index": 0}],   # 0
                    [{"node": "📂 Load Daily Analytics Log",      "type": "main", "index": 0}],   # 1
                ]
            },

            # Both paths merge
            "🔬 Validate & Normalise Payload": {
                "main": [[{"node": "🔗 Merge Alert Paths", "type": "main", "index": 0}]]
            },
            "📂 Load Daily Analytics Log": {
                "main": [[{"node": "🔗 Merge Alert Paths", "type": "main", "index": 1}]]
            },

            # Merge → dedup → gate
            "🔗 Merge Alert Paths":   {"main": [[{"node": "🛡️ Deduplication Check",    "type": "main", "index": 0}]]},
            "🛡️ Deduplication Check": {"main": [[{"node": "❓ Is Duplicate? (suppress)","type": "main", "index": 0}]]},

            # Duplicate gate: TRUE → suppress log, FALSE → build messages (parallel)
            "❓ Is Duplicate? (suppress)": {
                "main": [
                    # FALSE (not duplicate) → fan out to 3 builders
                    [
                        {"node": "🔧 Build Slack Message",   "type": "main", "index": 0},
                        {"node": "🔧 Build Telegram Message","type": "main", "index": 0},
                        {"node": "🔧 Build Email Body",      "type": "main", "index": 0},
                    ],
                    # TRUE (duplicate) → suppress
                    [{"node": "🔕 Log Suppressed Alert", "type": "main", "index": 0}],
                ]
            },

            # Build → Send (with severity gates)
            "🔧 Build Slack Message":    {"main": [[{"node": "🔔 Send Slack Alert",          "type": "main", "index": 0}]]},
            "🔧 Build Telegram Message": {"main": [[{"node": "❓ Telegram Threshold Met?",    "type": "main", "index": 0}]]},
            "🔧 Build Email Body":       {"main": [[{"node": "❓ Email Threshold Met?",        "type": "main", "index": 0}]]},

            # Telegram gate: TRUE → send, FALSE → no-op
            "❓ Telegram Threshold Met?": {
                "main": [
                    [{"node": "📱 Send Telegram Alert", "type": "main", "index": 0}],  # TRUE
                    [],  # FALSE
                ]
            },
            # Email gate: TRUE → send, FALSE → no-op
            "❓ Email Threshold Met?": {
                "main": [
                    [{"node": "📧 Send Email Alert (Gmail)", "type": "main", "index": 0}],  # TRUE
                    [],  # FALSE
                ]
            },

            # Escalation: after Telegram send, check if critical
            "📱 Send Telegram Alert": {
                "main": [[{"node": "❓ Escalate? (critical only)", "type": "main", "index": 0}]]
            },
            "❓ Escalate? (critical only)": {
                "main": [
                    [{"node": "⏱️ Escalation Delay (5 min)", "type": "main", "index": 0}],  # TRUE
                    [],  # FALSE
                ]
            },
            "⏱️ Escalation Delay (5 min)": {
                "main": [[{"node": "🚨 Escalation: Repeat Telegram", "type": "main", "index": 0}]]
            },
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Deploy helpers
# ─────────────────────────────────────────────────────────────────────────────

def find_existing(name: str) -> str | None:
    r = requests.get(f"{N8N_API_URL}/workflows", headers=HEADERS, timeout=15)
    r.raise_for_status()
    for wf in r.json().get("data", []):
        if wf["name"] == name:
            return wf["id"]
    return None

def deploy(wf: dict, activate: bool = False) -> dict:
    print("  📡 POSTing workflow to n8n...")
    r = requests.post(f"{N8N_API_URL}/workflows", headers=HEADERS, json=_clean(wf), timeout=30)
    r.raise_for_status()
    created = r.json()
    print(f"  ✅ Created — id={created['id']}")
    if activate:
        _activate(created["id"])
    return created

def update_existing(wf_id: str, wf: dict, activate: bool = False) -> dict:
    print(f"  🔄 Updating id={wf_id}...")
    r = requests.put(f"{N8N_API_URL}/workflows/{wf_id}", headers=HEADERS, json=_clean(wf, keep_id=wf_id), timeout=30)
    r.raise_for_status()
    print(f"  ✅ Updated — id={wf_id}")
    if activate:
        _activate(wf_id)
    return r.json()

def _activate(wf_id: str) -> None:
    requests.patch(f"{N8N_API_URL}/workflows/{wf_id}/activate", headers=HEADERS, timeout=15).raise_for_status()
    print("  ✅ Activated")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--activate",    action="store_true")
    parser.add_argument("--export-only", action="store_true", dest="export_only")
    parser.add_argument("--force-new",   action="store_true", dest="force_new")
    args = parser.parse_args()

    print("\n" + "═" * 58)
    print("  Kommo CRM — Notifications Hub — Workflow Deployer")
    print("═" * 58 + "\n")

    wf          = build_workflow()
    export_path = Path(__file__).parent / "kommo_notifications_workflow.json"
    export_path.write_text(json.dumps(wf, indent=2), encoding="utf-8")
    print(f"  💾 JSON exported → {export_path}")

    if args.export_only:
        print("\n  --export-only: skipping deployment.\n")
        return 0

    print(f"  📡 Target: {N8N_API_URL}\n")

    try:
        existing_id = None if args.force_new else find_existing(wf["name"])
        if existing_id:
            print(f"  ℹ️  Found existing workflow (id={existing_id}) — updating...")
            result = update_existing(existing_id, wf, args.activate)
        else:
            result = deploy(wf, args.activate)

        wf_id  = result["id"]
        active = result.get("active", False)

        # Extract webhook URL if possible
        webhook_url = f"http://localhost:5678/webhook/kommo-alert"

        print(f"\n  {'─' * 54}")
        print(f"  Workflow ID   : {wf_id}")
        print(f"  Name          : {result.get('name')}")
        print(f"  Active        : {'✅ Yes' if active else '⏸️  No'}")
        print(f"  UI URL        : http://localhost:5678/workflow/{wf_id}")
        print(f"  Webhook URL   : {webhook_url}")
        print(f"  JSON backup   : {export_path}")
        print(f"  Nodes         : {len(wf['nodes'])}")
        print(f"  {'─' * 54}\n")

        if not active:
            print("  ⚠️  Required env vars (n8n → Settings → Variables):")
            print("      KOMMO_SLACK_WEBHOOK         = https://hooks.slack.com/...")
            print("      KOMMO_TELEGRAM_BOT_TOKEN    = <bot_token>")
            print("      KOMMO_TELEGRAM_CHAT_ID      = <chat_id>")
            print("      KOMMO_ALERT_EMAIL_TO        = team@yourcompany.com")
            print()
            print("  ⚠️  Gmail credential required in n8n Credentials manager.")
            print()
            print("  📡  Once active, other workflows send alerts via:")
            print(f"      POST {webhook_url}")
            print()
            print(f"     python3 {Path(__file__).name} --activate\n")

        return 0

    except requests.exceptions.ConnectionError:
        print(f"\n  ❌ Cannot reach n8n at {N8N_API_URL}\n")
        return 1
    except requests.exceptions.HTTPError as e:
        print(f"\n  ❌ HTTP {e.response.status_code}: {e.response.text[:500]}\n")
        return 1
    except Exception as e:
        print(f"\n  ❌ Unexpected: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
