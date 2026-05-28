#!/usr/bin/env python3
"""
deploy_kommo_n8n_workflow.py
============================
Builds and deploys the Kommo CRM Daily AI Pipeline workflow to n8n.

Run:
    python3 deploy_kommo_n8n_workflow.py
    python3 deploy_kommo_n8n_workflow.py --activate     # Also activate after deploy
    python3 deploy_kommo_n8n_workflow.py --export-only  # Only write the JSON file
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "-q"])
    import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

N8N_API_URL  = "http://localhost:5678/api/v1"
N8N_API_KEY  = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIzZjc1YWZkZC0wZjE3LTQ5YTktODljMS0xMmM1YTM4NGIwMjUiLCJpc3MiOiJuOG4iLCJhdWQiOiJwdWJsaWMtYXBpIiwianRpIjoiYjE1Y2QwNmItMDc1Yy00NDE4LTgxNTktMzAwZGI4NTI3MzQ5IiwiaWF0IjoxNzc5OTkxNTQ4fQ.wX3Yv9o0lEtoD37Xrkm05y7H5UTiJP6XUdje1I1dreA"
PROJECT_DIR  = "/opt/kommo-platform/app"
PYTHON_BIN   = "python3"

# ── Downstream workflow IDs (deployed in this session) ──────────────────────
WF2_SHEETS_ID       = "wJRwiupLPj56WwL8"   # Kommo CRM → Google Sheets Sync
WF3_DRIVE_ID        = "Xn0w2yhDdB9xEhih"   # Kommo CRM → Google Drive Upload
WF4_AI_ID           = "3JtpaCSfTcr1iCX4"   # Kommo CRM → Claude AI Analysis
WF5_WEBHOOK         = "http://localhost:5678/webhook/kommo-alert"

HEADERS = {
    "X-N8N-API-KEY": N8N_API_KEY,
    "Content-Type":  "application/json",
}

# ---------------------------------------------------------------------------
# Build workflow definition
# ---------------------------------------------------------------------------

def build_workflow() -> dict:
    """Build the complete n8n workflow JSON."""

    # Shell commands run via docker exec - python3 lives in kommo-pipeline
    CMD_RUN = (
        "docker exec -u root kommo-pipeline sh -c "
        f"'cd /app && "
        f"{PYTHON_BIN} main.py --auto-incremental "
        "> /tmp/kommo_run.log 2>&1; "
        "KOMMO_EXIT=$?; "
        "cat /tmp/kommo_run.log; "
        "printf -- ---KOMMO_EXIT_CODE=%s--- $KOMMO_EXIT; "
        "exit 0'"
    )
    CMD_VALIDATE = (
        "docker exec -u root kommo-pipeline sh -c "
        f"'cd /app && "
        "( [ -s outputs/leads.json ] && echo ok_leads "
        "  || echo FAIL_leads ) && "
        "( [ -s outputs/messages_flat.json ] && echo ok_messages "
        "  || echo FAIL_messages ) && "
        "( ls daily_exports/*.json 2>/dev/null | head -1 > /dev/null "
        "  && echo ok_daily_exports || echo FAIL_daily_exports ) && "
        "echo ---VALIDATION=PASSED--- || echo ---VALIDATION=FAILED---'"
    )
    CMD_LOGS = (
        "docker exec -u root kommo-pipeline sh -c "
        "'tail -n 80 /app/logs/kommo.log 2>/dev/null | head -c 8000'"
    )
    CMD_ANALYTICS = (
        "docker exec -u root kommo-pipeline sh -c "
        "'cat /app/logs/analytics_$(date +%Y-%m-%d).json 2>/dev/null "
        "|| echo no_analytics_file; exit 0'"
    )



    # ── Code node scripts ──────────────────────────────────────────────────
    JS_PARSE_EXIT = r"""
const item = $input.first();
const stdout = item.json.stdout || '';

const exitMatch = stdout.match(/---KOMMO_EXIT_CODE=(\d+)---/);
const exitCode  = exitMatch ? parseInt(exitMatch[1]) : 1;
const cleanOut  = stdout.replace(/---KOMMO_EXIT_CODE=\d+---/, '').trim();
const lastLines = cleanOut.split('\n').slice(-40).join('\n');

return [{
  json: {
    exitCode,
    success:        exitCode === 0,
    outputSummary:  lastLines,
    fullOutput:     cleanOut,
    startedAt:      $('⚙️ Set Run Config').first().json.started_at,
    triggeredBy:    $('⚙️ Set Run Config').first().json.triggered_by,
    projectDir:     $('⚙️ Set Run Config').first().json.project_dir,
  }
}];
"""

    JS_PARSE_VALIDATION = r"""
const stdout   = $input.first().json.stdout || '';
const passed   = stdout.includes('---VALIDATION=PASSED---');
const lines    = stdout.split('\n');
const failures = lines.filter(l => l.startsWith('FAIL:')).join('\n');
const checks   = lines.filter(l => l.startsWith('✓') || l.startsWith('FAIL:')).join('\n');

return [{
  json: {
    validationPassed:   passed,
    validationOutput:   stdout.trim(),
    validationChecks:   checks,
    validationFailures: failures,
    startedAt:   $('⚙️ Set Run Config').first().json.started_at,
    triggeredBy: $('⚙️ Set Run Config').first().json.triggered_by,
  }
}];
"""

    JS_SUCCESS_REPORT = r"""
const logStdout = $('📋 Capture Logs').first().json.stdout || '';
const config    = $('⚙️ Set Run Config').first().json;

let analytics = {};
try {
  const raw = $('📊 Capture Analytics').first().json.stdout || '{}';
  analytics  = JSON.parse(raw.trim());
} catch(e) {
  analytics  = { raw: $('📊 Capture Analytics').first().json.stdout };
}

const duration = (() => {
  try {
    const start = new Date(config.started_at);
    const end   = new Date();
    const secs  = Math.round((end - start) / 1000);
    return `${Math.floor(secs/60)}m ${secs%60}s`;
  } catch(e) { return 'unknown'; }
})();

return [{
  json: {
    status:        'SUCCESS',
    pipeline:      'Kommo CRM — Daily AI Pipeline',
    startedAt:     config.started_at,
    completedAt:   $now.toISO(),
    duration,
    triggeredBy:   config.triggered_by,
    runMode:       config.run_mode,
    logSummary:    logStdout.split('\n').slice(-20).join('\n'),
    analytics,
    validation:    $('🔍 Parse Validation').first().json.validationChecks,
  }
}];
"""

    # ── Slack alert body template ─────────────────────────────────────────
    # Used by both failure alert nodes (different field names passed in)
    def slack_body(title_expr, stage_expr, detail_expr, triggered_expr, time_expr):
        return (
            "={{ JSON.stringify({"
            f"  text: {title_expr},"
            "  blocks: [{"
            "    type: 'header',"
            f"    text: {{ type: 'plain_text', text: {title_expr} }}"
            "  },{"
            "    type: 'section',"
            "    fields: ["
            f"      {{ type: 'mrkdwn', text: '*Stage:*\\n' + {stage_expr} }},"
            f"      {{ type: 'mrkdwn', text: '*Triggered by:*\\n' + {triggered_expr} }},"
            f"      {{ type: 'mrkdwn', text: '*Time:*\\n' + {time_expr} }}"
            "    ]"
            "  },{"
            "    type: 'section',"
            f"    text: {{ type: 'mrkdwn', text: '*Details:*\\n```\\n' + ({detail_expr} || 'none').slice(0,500) + '\\n```' }}"
            "  }]"
            "}) }}"
        )

    SLACK_EXTRACTION_BODY = slack_body(
        "$json.alert_title",
        "$json.alert_stage",
        "$json.alert_details",
        "$json.triggered_by",
        "$json.failed_at",
    )
    SLACK_VALIDATION_BODY = slack_body(
        "$json.alert_title",
        "$json.alert_stage",
        "$json.alert_details",
        "$json.triggered_by",
        "$json.failed_at",
    )

    # ── Workflow definition ────────────────────────────────────────────────
    return {
        "name": "Kommo CRM — Daily AI Pipeline",
        # Note: 'active' is read-only on create — set via PATCH after creation
        "settings": {
            "executionOrder":           "v1",
            "saveManualExecutions":     True,
            "callerPolicy":             "workflowsFromSameOwner",
            "saveExecutionProgress":    True,
            "saveDataSuccessExecution": "all",
            "saveDataErrorExecution":   "all",
            "executionTimeout":         7200,
            "timezone":                 "UTC",
        },
        "staticData": None,
        "tags":       [],

        # ────────────────────────────────────────────────────────────────
        # NODES
        # ────────────────────────────────────────────────────────────────
        "nodes": [

            # ── Documentation sticky notes ────────────────────────────
            {
                "id": "sticky-overview",
                "name": "📌 Workflow Overview",
                "type": "n8n-nodes-base.stickyNote",
                "typeVersion": 1,
                "position": [-220, 80],
                "parameters": {
                    "width":   420,
                    "height":  360,
                    "color":   2,
                    "content": (
                        "## 🤖 Kommo CRM — Daily AI Pipeline\n\n"
                        "**Triggers:** ⏰ Daily 06:00 UTC (cron) · ▶️ Manual\n\n"
                        "**Pipeline steps:**\n"
                        "1. Run `python main.py --auto-incremental`\n"
                        "2. Parse exit code (auto-retry ×2)\n"
                        "3. Validate output files exist\n"
                        "4. Capture logs + analytics\n"
                        "5. Build structured success report\n"
                        "6. 🔔 Slack alert on any failure\n\n"
                        "**Timeout:** 2 hours · **Retries:** 2 (120s gap)"
                    ),
                },
            },
            {
                "id": "sticky-env",
                "name": "🔧 Required Env Vars",
                "type": "n8n-nodes-base.stickyNote",
                "typeVersion": 1,
                "position": [-220, 480],
                "parameters": {
                    "width":   420,
                    "height":  240,
                    "color":   4,
                    "content": (
                        "## 🔧 n8n Environment Variables\n\n"
                        "Set via n8n → Settings → Environment:\n\n"
                        "```\n"
                        "KOMMO_SLACK_WEBHOOK=https://hooks.slack.com/...\n"
                        "```\n\n"
                        "**If unset**, Slack steps skip gracefully\n"
                        "(neverError=true)"
                    ),
                },
            },

            # ── Triggers ──────────────────────────────────────────────
            {
                "id":          "schedule-trigger",
                "name":        "⏰ Daily 6AM UTC",
                "type":        "n8n-nodes-base.scheduleTrigger",
                "typeVersion": 1.2,
                "position":    [280, 260],
                "parameters": {
                    "rule": {
                        "interval": [
                            {"field": "cronExpression", "expression": "0 6 * * *"}
                        ]
                    }
                },
            },
            {
                "id":          "manual-trigger",
                "name":        "▶️ Manual Run",
                "type":        "n8n-nodes-base.manualTrigger",
                "typeVersion": 1,
                "position":    [280, 440],
                "parameters":  {},
            },

            # ── Configuration ─────────────────────────────────────────
            {
                "id":          "set-run-config",
                "name":        "⚙️ Set Run Config",
                "type":        "n8n-nodes-base.set",
                "typeVersion": 3.4,
                "position":    [520, 340],
                "parameters": {
                    "mode": "manual",
                    "assignments": {
                        "assignments": [
                            {"id": "a1", "name": "project_dir",  "value": PROJECT_DIR,             "type": "string"},
                            {"id": "a2", "name": "python_bin",   "value": PYTHON_BIN,              "type": "string"},
                            {"id": "a3", "name": "run_mode",     "value": "incremental",            "type": "string"},
                            {"id": "a4", "name": "started_at",   "value": "={{ $now.toISO() }}",    "type": "string"},
                            {"id": "a5", "name": "triggered_by",
                             "value": "={{ $execution.mode === 'manual' ? 'manual' : 'schedule' }}",
                             "type": "string"},
                            {"id": "a6", "name": "retry_count",  "value": 2,                       "type": "number"},
                        ]
                    },
                    "options": {},
                },
            },

            # ── Extraction ────────────────────────────────────────────
            {
                "id":          "run-pipeline",
                "name":        "🚀 Run Kommo Pipeline",
                "type":        "n8n-nodes-base.executeCommand",
                "typeVersion": 1,
                "position":    [780, 340],
                "parameters":  {"command": CMD_RUN},
                "retryOnFail":     True,
                "maxTries":        2,
                "waitBetweenTries": 120000,
            },

            # ── Parse Exit Code ───────────────────────────────────────
            {
                "id":          "parse-exit-code",
                "name":        "🔍 Parse Exit Code",
                "type":        "n8n-nodes-base.code",
                "typeVersion": 2,
                "position":    [1040, 340],
                "parameters":  {"jsCode": JS_PARSE_EXIT.strip()},
            },

            # ── Pipeline Success Check ────────────────────────────────
            {
                "id":          "check-pipeline-ok",
                "name":        "❓ Pipeline OK?",
                "type":        "n8n-nodes-base.if",
                "typeVersion": 1,
                "position":    [1280, 340],
                "parameters": {
                    "conditions": {
                        "boolean": [
                            {
                                "value1":    "={{ $json.success }}",
                                "operation": "equal",
                                "value2":    True,
                            }
                        ]
                    }
                },
            },

            # ── TRUE branch: Validate Outputs ─────────────────────────
            {
                "id":          "validate-outputs",
                "name":        "✅ Validate Outputs",
                "type":        "n8n-nodes-base.executeCommand",
                "typeVersion": 1,
                "position":    [1540, 220],
                "parameters":  {"command": CMD_VALIDATE},
            },

            # ── FALSE branch: Capture Extraction Failure ──────────────
            {
                "id":          "set-extraction-failure",
                "name":        "❌ Extraction Failed",
                "type":        "n8n-nodes-base.set",
                "typeVersion": 3.4,
                "position":    [1540, 500],
                "parameters": {
                    "mode": "manual",
                    "assignments": {
                        "assignments": [
                            {"id": "f1", "name": "alert_title",   "value": "❌ Kommo Pipeline — Extraction FAILED",     "type": "string"},
                            {"id": "f2", "name": "alert_stage",   "value": "Extraction (python main.py)",               "type": "string"},
                            {"id": "f3", "name": "alert_details", "value": "={{ $json.outputSummary }}",                 "type": "string"},
                            {"id": "f4", "name": "exit_code",     "value": "={{ $json.exitCode }}",                      "type": "string"},
                            {"id": "f5", "name": "triggered_by",  "value": "={{ $json.triggeredBy }}",                   "type": "string"},
                            {"id": "f6", "name": "failed_at",     "value": "={{ $now.toISO() }}",                        "type": "string"},
                        ]
                    },
                    "options": {},
                },
            },

            # ── Parse Validation Result ───────────────────────────────
            {
                "id":          "parse-validation",
                "name":        "🔍 Parse Validation",
                "type":        "n8n-nodes-base.code",
                "typeVersion": 2,
                "position":    [1800, 220],
                "parameters":  {"jsCode": JS_PARSE_VALIDATION.strip()},
            },

            # ── Outputs Valid Check ───────────────────────────────────
            {
                "id":          "check-outputs-valid",
                "name":        "❓ Outputs Valid?",
                "type":        "n8n-nodes-base.if",
                "typeVersion": 1,
                "position":    [2040, 220],
                "parameters": {
                    "conditions": {
                        "boolean": [
                            {
                                "value1":    "={{ $json.validationPassed }}",
                                "operation": "equal",
                                "value2":    True,
                            }
                        ]
                    }
                },
            },

            # ── TRUE: Capture Logs ────────────────────────────────────
            {
                "id":          "capture-logs",
                "name":        "📋 Capture Logs",
                "type":        "n8n-nodes-base.executeCommand",
                "typeVersion": 1,
                "position":    [2300, 100],
                "parameters":  {"command": CMD_LOGS},
            },

            # ── FALSE: Capture Validation Failure ─────────────────────
            {
                "id":          "set-validation-failure",
                "name":        "⚠️ Validation Failed",
                "type":        "n8n-nodes-base.set",
                "typeVersion": 3.4,
                "position":    [2300, 360],
                "parameters": {
                    "mode": "manual",
                    "assignments": {
                        "assignments": [
                            {"id": "v1", "name": "alert_title",   "value": "⚠️ Kommo Pipeline — Output Validation FAILED", "type": "string"},
                            {"id": "v2", "name": "alert_stage",   "value": "Output Validation",                             "type": "string"},
                            {"id": "v3", "name": "alert_details", "value": "={{ $json.validationFailures }}",               "type": "string"},
                            {"id": "v4", "name": "triggered_by",  "value": "={{ $json.triggeredBy }}",                      "type": "string"},
                            {"id": "v5", "name": "failed_at",     "value": "={{ $now.toISO() }}",                           "type": "string"},
                        ]
                    },
                    "options": {},
                },
            },

            # ── Capture Analytics ─────────────────────────────────────
            {
                "id":          "capture-analytics",
                "name":        "📊 Capture Analytics",
                "type":        "n8n-nodes-base.executeCommand",
                "typeVersion": 1,
                "position":    [2560, 100],
                "parameters":  {"command": CMD_ANALYTICS},
            },

            # ── Build Success Report ──────────────────────────────────
            {
                "id":          "build-success-report",
                "name":        "🎉 Build Success Report",
                "type":        "n8n-nodes-base.code",
                "typeVersion": 2,
                "position":    [2820, 100],
                "parameters":  {"jsCode": JS_SUCCESS_REPORT.strip()},
            },

            # ── Downstream Chain: WF2 → WF3 → WF4 ───────────────────
            {
                "id":          "trigger-sheets",
                "name":        "📊 Trigger: Sheets Sync (WF2)",
                "type":        "n8n-nodes-base.executeWorkflow",
                "typeVersion": 1,
                "position":    [3080, 100],
                "parameters": {
                    "source":     "database",
                    "workflowId": WF2_SHEETS_ID,
                    "mode":       "once",
                    "options":    {},
                },
            },
            {
                "id":          "trigger-drive",
                "name":        "☁️ Trigger: Drive Upload (WF3)",
                "type":        "n8n-nodes-base.executeWorkflow",
                "typeVersion": 1,
                "position":    [3340, 100],
                "parameters": {
                    "source":     "database",
                    "workflowId": WF3_DRIVE_ID,
                    "mode":       "once",
                    "options":    {},
                },
            },
            {
                "id":          "trigger-ai",
                "name":        "🤖 Trigger: AI Analysis (WF4)",
                "type":        "n8n-nodes-base.executeWorkflow",
                "typeVersion": 1,
                "position":    [3600, 100],
                "parameters": {
                    "source":     "database",
                    "workflowId": WF4_AI_ID,
                    "mode":       "once",
                    "options":    {},
                },
            },

            # ── Notify Hub: Pipeline SUCCESS ──────────────────────────
            {
                "id":          "notify-hub-success",
                "name":        "📡 Notify Hub: Pipeline Complete",
                "type":        "n8n-nodes-base.httpRequest",
                "typeVersion": 4.2,
                "position":    [3860, 100],
                "parameters": {
                    "method":      "POST",
                    "url":         WF5_WEBHOOK,
                    "sendHeaders": True,
                    "headerParameters": {
                        "parameters": [{"name": "Content-Type", "value": "application/json"}]
                    },
                    "sendBody":    True,
                    "specifyBody": "json",
                    "jsonBody": (
                        "={{ JSON.stringify({"
                        "  alert_type:   'operational_alert',"
                        "  severity:     'info',"
                        "  title:        '✅ Kommo Pipeline — Full Run Complete',"
                        "  message:      'All 4 downstream workflows completed successfully.',"
                        "  source:       'Workflow 1 — Master Orchestration',"
                        "  triggered_by: $('⚙️ Set Run Config').first().json.triggered_by,"
                        "  details: {"
                        "    started_at:   $('⚙️ Set Run Config').first().json.started_at,"
                        "    completed_at: $now.toISO(),"
                        "    run_mode:     $('⚙️ Set Run Config').first().json.run_mode,"
                        "  }"
                        "}) }}"
                    ),
                    "options": {
                        "response": {"response": {"neverError": True}},
                        "timeout":  10000,
                    },
                },
            },

            # ── Notify Hub: EXTRACTION failure ────────────────────────
            {
                "id":          "alert-extraction-failed",
                "name":        "🔔 Alert: Extraction Failed",
                "type":        "n8n-nodes-base.httpRequest",
                "typeVersion": 4.2,
                "position":    [1800, 500],
                "parameters": {
                    "method":      "POST",
                    "url":         WF5_WEBHOOK,
                    "sendHeaders": True,
                    "headerParameters": {
                        "parameters": [{"name": "Content-Type", "value": "application/json"}]
                    },
                    "sendBody":    True,
                    "specifyBody": "json",
                    "bodyParameters": {
                        "parameters": [
                            {"name": "alert_type",   "value": "extraction_failure"},
                            {"name": "severity",     "value": "critical"},
                            {"name": "title",        "value": "={{ $json.alert_title }}"},
                            {"name": "message",      "value": "={{ $json.alert_details || 'Pipeline exit code: ' + $json.exit_code }}"},
                            {"name": "source",       "value": "Workflow 1 — Master Orchestration"},
                            {"name": "triggered_by", "value": "={{ $json.triggered_by }}"},
                            {"name": "stage",        "value": "={{ $json.alert_stage }}"},
                            {"name": "exit_code",    "value": "={{ $json.exit_code }}"},
                        ]
                    },
                    "options": {
                        "response": {"response": {"neverError": True}},
                        "timeout":  10000,
                    },
                },
            },

            # ── Notify Hub: VALIDATION failure ────────────────────────
            {
                "id":          "alert-validation-failed",
                "name":        "🔔 Alert: Validation Failed",
                "type":        "n8n-nodes-base.httpRequest",
                "typeVersion": 4.2,
                "position":    [2560, 360],
                "parameters": {
                    "method":      "POST",
                    "url":         WF5_WEBHOOK,
                    "sendHeaders": True,
                    "headerParameters": {
                        "parameters": [{"name": "Content-Type", "value": "application/json"}]
                    },
                    "sendBody":    True,
                    "specifyBody": "json",
                    "bodyParameters": {
                        "parameters": [
                            {"name": "alert_type",   "value": "validation_error"},
                            {"name": "severity",     "value": "critical"},
                            {"name": "title",        "value": "={{ $json.alert_title }}"},
                            {"name": "message",      "value": "={{ $json.alert_details || 'One or more output files missing or invalid.' }}"},
                            {"name": "source",       "value": "Workflow 1 — Master Orchestration"},
                            {"name": "triggered_by", "value": "={{ $json.triggered_by }}"},
                            {"name": "stage",        "value": "={{ $json.alert_stage }}"},
                        ]
                    },
                    "options": {
                        "response": {"response": {"neverError": True}},
                        "timeout":  10000,
                    },
                },
            },
        ],

        # ────────────────────────────────────────────────────────────────
        # CONNECTIONS
        # ────────────────────────────────────────────────────────────────
        "connections": {
            # Both triggers → Set Run Config
            "⏰ Daily 6AM UTC": {
                "main": [[{"node": "⚙️ Set Run Config", "type": "main", "index": 0}]]
            },
            "▶️ Manual Run": {
                "main": [[{"node": "⚙️ Set Run Config", "type": "main", "index": 0}]]
            },

            # Config → Run Pipeline → Parse Exit → IF
            "⚙️ Set Run Config": {
                "main": [[{"node": "🚀 Run Kommo Pipeline", "type": "main", "index": 0}]]
            },
            "🚀 Run Kommo Pipeline": {
                "main": [[{"node": "🔍 Parse Exit Code", "type": "main", "index": 0}]]
            },
            "🔍 Parse Exit Code": {
                "main": [[{"node": "❓ Pipeline OK?", "type": "main", "index": 0}]]
            },

            # IF Pipeline OK: TRUE → validate, FALSE → extraction failure
            "❓ Pipeline OK?": {
                "main": [
                    [{"node": "✅ Validate Outputs",   "type": "main", "index": 0}],  # output 0 = TRUE
                    [{"node": "❌ Extraction Failed",   "type": "main", "index": 0}],  # output 1 = FALSE
                ]
            },

            # Validate → Parse → IF Outputs Valid
            "✅ Validate Outputs": {
                "main": [[{"node": "🔍 Parse Validation", "type": "main", "index": 0}]]
            },
            "🔍 Parse Validation": {
                "main": [[{"node": "❓ Outputs Valid?", "type": "main", "index": 0}]]
            },

            # IF Outputs Valid: TRUE → logs, FALSE → validation failure
            "❓ Outputs Valid?": {
                "main": [
                    [{"node": "📋 Capture Logs",      "type": "main", "index": 0}],  # TRUE
                    [{"node": "⚠️ Validation Failed", "type": "main", "index": 0}],  # FALSE
                ]
            },

            # Success path: logs → analytics → report → WF2 → WF3 → WF4 → Hub notify
            "📋 Capture Logs": {
                "main": [[{"node": "📊 Capture Analytics", "type": "main", "index": 0}]]
            },
            "📊 Capture Analytics": {
                "main": [[{"node": "🎉 Build Success Report", "type": "main", "index": 0}]]
            },
            "🎉 Build Success Report": {
                "main": [[{"node": "📊 Trigger: Sheets Sync (WF2)", "type": "main", "index": 0}]]
            },
            "📊 Trigger: Sheets Sync (WF2)": {
                "main": [[{"node": "☁️ Trigger: Drive Upload (WF3)", "type": "main", "index": 0}]]
            },
            "☁️ Trigger: Drive Upload (WF3)": {
                "main": [[{"node": "🤖 Trigger: AI Analysis (WF4)", "type": "main", "index": 0}]]
            },
            "🤖 Trigger: AI Analysis (WF4)": {
                "main": [[{"node": "📡 Notify Hub: Pipeline Complete", "type": "main", "index": 0}]]
            },

            # Failure paths → Notifications Hub (critical alert)
            "❌ Extraction Failed": {
                "main": [[{"node": "🔔 Alert: Extraction Failed", "type": "main", "index": 0}]]
            },
            "⚠️ Validation Failed": {
                "main": [[{"node": "🔔 Alert: Validation Failed", "type": "main", "index": 0}]]
            },
        },
    }


# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------

# Fields that n8n marks as read-only and rejects in POST / PUT bodies
_READ_ONLY_FIELDS = {"active", "tags", "id", "createdAt", "updatedAt", "versionId"}


def _safe_payload(workflow: dict, keep_id: str | None = None) -> dict:
    """Return a copy of the workflow dict with all read-only fields removed."""
    payload = {k: v for k, v in workflow.items() if k not in _READ_ONLY_FIELDS}
    return payload


def deploy(workflow: dict, activate: bool = False) -> dict:
    """POST the workflow to n8n API and optionally activate it."""
    print("  📡 POSTing workflow to n8n...")
    resp = requests.post(
        f"{N8N_API_URL}/workflows",
        headers=HEADERS,
        json=_safe_payload(workflow),
        timeout=30,
    )
    resp.raise_for_status()
    created = resp.json()
    wf_id   = created["id"]
    print(f"  ✅ Created workflow — id={wf_id}")

    if activate:
        print("  ⚡ Activating workflow...")
        act = requests.post(
            f"{N8N_API_URL}/workflows/{wf_id}/activate",
            headers=HEADERS,
            timeout=15,
        )
        act.raise_for_status()
        print(f"  ✅ Workflow activated")

    return created


def update_existing(wf_id: str, workflow: dict, activate: bool = False) -> dict:
    """PUT to update an existing workflow by ID.
    NOTE: n8n rejects 'id' in the PUT body even though it is in the URL path.
    We strip it from the payload completely.
    """
    print(f"  🔄 Updating existing workflow id={wf_id}...")
    # n8n PUT /workflows/{id} must NOT include id in body
    payload = {k: v for k, v in workflow.items() if k not in _READ_ONLY_FIELDS}
    resp = requests.put(
        f"{N8N_API_URL}/workflows/{wf_id}",
        headers=HEADERS,
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    updated = resp.json()
    print(f"  ✅ Workflow updated — id={wf_id}")

    if activate:
        act = requests.post(
            f"{N8N_API_URL}/workflows/{wf_id}/activate",
            headers=HEADERS,
            timeout=15,
        )
        act.raise_for_status()
        print("  ✅ Workflow activated")

    return updated


def find_existing(name: str) -> str | None:
    """Return the ID of an existing workflow with this name, or None."""
    resp = requests.get(f"{N8N_API_URL}/workflows", headers=HEADERS, timeout=15)
    resp.raise_for_status()
    for wf in resp.json().get("data", []):
        if wf["name"] == name:
            return wf["id"]
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy Kommo CRM n8n workflow")
    parser.add_argument("--activate",     action="store_true", help="Activate workflow after deployment")
    parser.add_argument("--export-only",  action="store_true", help="Write JSON only, do not deploy")
    parser.add_argument("--force-new",    action="store_true", help="Always create new, don't update existing")
    args = parser.parse_args()

    print("\n═" * 36)
    print("  Kommo CRM — n8n Workflow Deployer")
    print("═" * 36 + "\n")

    workflow    = build_workflow()
    export_path = Path(__file__).parent / "kommo_n8n_workflow.json"

    # Always export JSON (for manual import backup)
    export_path.write_text(json.dumps(workflow, indent=2), encoding="utf-8")
    print(f"  💾 Workflow JSON exported → {export_path}")

    if args.export_only:
        print("\n  --export-only flag set. Skipping deployment.\n")
        return 0

    print(f"  📡 Target: {N8N_API_URL}\n")

    try:
        wf_name    = workflow["name"]
        existing_id = None if args.force_new else find_existing(wf_name)

        if existing_id:
            print(f"  ℹ️  Found existing workflow '{wf_name}' (id={existing_id}) — updating...")
            result = update_existing(existing_id, workflow, activate=args.activate)
        else:
            result = deploy(workflow, activate=args.activate)

        wf_id = result["id"]
        active = result.get("active", False)

        print(f"\n  ─────────────────────────────────────────")
        print(f"  Workflow ID  : {wf_id}")
        print(f"  Name         : {result.get('name')}")
        print(f"  Active       : {'✅ Yes' if active else '⏸️  No (activate in n8n UI)'}")
        print(f"  UI URL       : http://localhost:5678/workflow/{wf_id}")
        print(f"  JSON backup  : {export_path}")
        print(f"  Nodes        : {len(workflow['nodes'])}")
        print(f"  ─────────────────────────────────────────")

        if not active:
            print("\n  ⚠️  Workflow is NOT active. To start the daily schedule:")
            print(f"     Open http://localhost:5678/workflow/{wf_id}")
            print("     Toggle the 'Active' switch to ON")
            print("\n  Or re-run with --activate flag:")
            print(f"     python3 {Path(__file__).name} --activate\n")

        return 0

    except requests.exceptions.ConnectionError:
        print("\n  ❌ Cannot reach n8n at", N8N_API_URL)
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
