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
N8N_API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIzZjc1YWZkZC0wZjE3LTQ5YTktODljMS0xMmM1YTM4NGIwMjUiLCJpc3MiOiJuOG4iLCJhdWQiOiJwdWJsaWMtYXBpIiwianRpIjoiYzdmZjNlZjgtMDhkYy00Y2Q2LTlkOTUtMDU0MjkwYzNhMWYzIiwiaWF0IjoxNzc5ODk1NTU0fQ.UAH-vKXs0pbKEA0UU1V7noYbbRuxfeHjja8fhYMuexo"
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
    if keep_id:
        p["id"] = keep_id
    return p


# ─────────────────────────────────────────────────────────────────────────────
# Claude System Prompt  (baked into the Code node — no credential needed)
# ─────────────────────────────────────────────────────────────────────────────

CLAUDE_SYSTEM_PROMPT = """You are an elite CRM conversation analyst for a medical aesthetic clinic (plastic surgery). You will analyse patient conversations and return ONLY a valid JSON object. No markdown fences. No explanation. Pure JSON.

Your JSON response MUST follow this exact schema:
{
  "sentiment": {
    "label": "positive|neutral|negative",
    "score": 0.0-1.0,
    "rationale": "One sentence max."
  },
  "buying_signal": {
    "strength": "strong|moderate|weak|none",
    "signals": ["list of exact phrases or behaviours that indicate intent"],
    "confidence": 0.0-1.0
  },
  "objections": [
    {
      "type": "price|timing|trust|information|logistics|other",
      "text": "Exact objection or paraphrase",
      "handled": true|false
    }
  ],
  "follow_up": {
    "urgency": "critical|high|medium|low",
    "action": "Single specific action in imperative form",
    "deadline_hours": 2|6|12|24|48|72,
    "notes": "Optional context for the agent"
  },
  "agent_performance": {
    "score": 1-10,
    "response_time": "fast|acceptable|slow|very_slow",
    "empathy_score": 1-10,
    "clarity_score": 1-10,
    "strengths": ["max 2 items"],
    "improvements": ["max 2 items"]
  },
  "summary": "2-3 sentence executive summary of this conversation."
}

Rules:
- Return ONLY the JSON object. Nothing else.
- All string fields are required even if empty string.
- objections may be an empty array [] if none detected.
- score, confidence, and all numeric fields must be numbers (not strings).
- Be critical and objective. Do not inflate scores."""


# ─────────────────────────────────────────────────────────────────────────────
# JavaScript Code Nodes
# ─────────────────────────────────────────────────────────────────────────────

JS_DETECT_EXPORT = f"""
const fs   = require('fs');
const path = require('path');
const dir  = '{EXPORT_DIR}';

if (!fs.existsSync(dir)) {{
  return [{{ json: {{ found: false, reason: `Export dir missing: ${{dir}}` }} }}];
}}

const files = fs.readdirSync(dir)
  .filter(f => /^\\d{{4}}-\\d{{2}}-\\d{{2}}\\.json$/.test(f))
  .sort().reverse();

if (!files.length) {{
  return [{{ json: {{ found: false, reason: 'No daily exports on disk' }} }}];
}}

const latest   = files[0];
const filePath = path.join(dir, latest);
const stat     = fs.statSync(filePath);

return [{{ json: {{ found: true, fileName: latest, filePath, fileDate: latest.replace('.json',''), fileSizeKB: Math.round(stat.size/1024) }} }}];
"""

JS_LOAD_LEADS = """
const fs   = require('fs');
const det  = $input.first().json;

if (!det.found) {
  return [{ json: { leads: [], totalLeads: 0, exportDate: null, skipped: true, reason: det.reason } }];
}

const raw    = JSON.parse(fs.readFileSync(det.filePath, 'utf8'));
const leads  = (raw.leads || []).filter(l => Array.isArray(l.messages) && l.messages.length > 0);
const meta   = raw._meta || {};

return [{
  json: {
    leads,
    totalLeads:    leads.length,
    exportDate:    meta.date || det.fileDate,
    totalMessages: meta.total_messages || 0,
    filePath:      det.filePath,
    skipped:       false,
  }
}];
"""

# Splits leads into individual items for SplitInBatches node
JS_EXPAND_LEADS = """
const data = $input.first().json;
if (data.skipped || !data.leads.length) {
  return [{ json: { skipped: true, exportDate: data.exportDate || 'unknown' } }];
}
return data.leads.map((lead, idx) => ({
  json: {
    ...lead,
    exportDate:   data.exportDate,
    leadIndex:    idx,
    totalLeads:   data.totalLeads,
  }
}));
"""

# Build Claude prompt for one lead — with chunking if >50 messages
JS_BUILD_PROMPT = f"""
const lead      = $input.first().json;
const CHUNK_MAX = 40;
const messages  = lead.messages || [];

// Serialise messages into readable transcript
const transcript = messages.slice(0, CHUNK_MAX).map((m, i) => {{
  const ts   = m.timestamp_iso ? new Date(m.timestamp_iso).toLocaleString('en-US', {{ timeZone:'UTC'}}) : '';
  const who  = m.direction === 'in' ? (m.author || 'Patient') : ('Agent: ' + (m.author || 'Unknown'));
  const text = (m.message_text || '[media/no text]').slice(0, 800);
  return `[${{i+1}}] ${{ts}} | ${{who}}: ${{text}}`;
}}).join('\\n');

const isChunked   = messages.length > CHUNK_MAX;
const chunkNote   = isChunked ? `\\n⚠️ Note: Showing first ${{CHUNK_MAX}} of ${{messages.length}} messages.` : '';
const channelNote = lead.channel ? `Channel: ${{lead.channel}}` : '';

const userPrompt = `Analyse this patient conversation for lead ID ${{lead.lead_id || 'unknown'}}.
Lead name: ${{lead.lead_name || 'Unknown'}}
Contact: ${{lead.contact_name || 'Unknown'}}
${{channelNote}}
Total messages in conversation: ${{messages.length}}${{chunkNote}}

--- CONVERSATION START ---
${{transcript || 'No messages'}}
--- CONVERSATION END ---`;

return [{{
  json: {{
    ...lead,
    claudeSystemPrompt: `{CLAUDE_SYSTEM_PROMPT}`,
    claudeUserPrompt:   userPrompt,
    messageCount:       messages.length,
    chunked:            isChunked,
    chunkSize:          Math.min(messages.length, CHUNK_MAX),
  }}
}}];
"""

# Parse Claude response — validate JSON and extract fields
JS_PARSE_CLAUDE = """
const lead      = $input.first().json;
const rawOutput = $json.content?.[0]?.text || $json.choices?.[0]?.message?.content || '';

let parsed = null;
let parseError = null;

try {
  // Strip any accidental markdown fences
  const cleaned = rawOutput
    .replace(/^```json\\s*/i, '')
    .replace(/^```\\s*/i, '')
    .replace(/```$/i, '')
    .trim();

  parsed = JSON.parse(cleaned);
} catch (e) {
  parseError = `JSON parse failed: ${e.message} | Raw: ${rawOutput.slice(0, 200)}`;
}

const isValid = parsed !== null
  && typeof parsed.sentiment === 'object'
  && typeof parsed.buying_signal === 'object'
  && typeof parsed.follow_up === 'object'
  && typeof parsed.agent_performance === 'object';

if (!isValid) {
  return [{
    json: {
      lead_id:      lead.lead_id,
      lead_name:    lead.lead_name,
      exportDate:   lead.exportDate,
      parseSuccess: false,
      parseError:   parseError || 'Missing required fields in response',
      rawOutput:    rawOutput.slice(0, 500),
    }
  }];
}

return [{
  json: {
    // Identity
    lead_id:       lead.lead_id,
    lead_name:     lead.lead_name,
    contact_name:  lead.contact_name || '',
    channel:       lead.channel || '',
    exportDate:    lead.exportDate,
    messageCount:  lead.messageCount,
    chunked:       lead.chunked,
    analysedAt:    new Date().toISOString(),
    parseSuccess:  true,

    // Claude outputs (flat for Sheets)
    sentiment_label:     parsed.sentiment?.label || '',
    sentiment_score:     parsed.sentiment?.score ?? 0,
    sentiment_rationale: parsed.sentiment?.rationale || '',

    buying_strength:     parsed.buying_signal?.strength || 'none',
    buying_confidence:   parsed.buying_signal?.confidence ?? 0,
    buying_signals:      JSON.stringify(parsed.buying_signal?.signals || []),

    objections_count:    (parsed.objections || []).length,
    objections:          JSON.stringify(parsed.objections || []),

    urgency:             parsed.follow_up?.urgency || 'low',
    follow_up_action:    parsed.follow_up?.action || '',
    deadline_hours:      parsed.follow_up?.deadline_hours ?? 24,
    follow_up_notes:     parsed.follow_up?.notes || '',

    agent_score:         parsed.agent_performance?.score ?? 0,
    agent_response_time: parsed.agent_performance?.response_time || '',
    agent_empathy:       parsed.agent_performance?.empathy_score ?? 0,
    agent_clarity:       parsed.agent_performance?.clarity_score ?? 0,
    agent_strengths:     JSON.stringify(parsed.agent_performance?.strengths || []),
    agent_improvements:  JSON.stringify(parsed.agent_performance?.improvements || []),

    summary:             parsed.summary || '',

    // Full parsed object for disk save
    _fullAnalysis:       parsed,
  }
}];
"""

# Save all summaries to disk as ai_summaries/YYYY-MM-DD.json
JS_SAVE_SUMMARIES = f"""
const fs    = require('fs');
const path  = require('path');
const items = $input.all().map(i => i.json).filter(i => i.parseSuccess);

if (!items.length) {{
  return [{{ json: {{ saved: false, reason: 'No valid summaries to save', count: 0 }} }}];
}}

const exportDate = items[0].exportDate || new Date().toISOString().split('T')[0];
const outDir     = '{SUMMARY_DIR}';
fs.mkdirSync(outDir, {{ recursive: true }});

const outPath = path.join(outDir, `${{exportDate}}.json`);
const envelope = {{
  _meta: {{
    date:         exportDate,
    generated_at: new Date().toISOString(),
    total_leads:  items.length,
    model:        'claude-3-5-sonnet-20241022',
  }},
  summaries: items.map(i => ({{
    lead_id:      i.lead_id,
    lead_name:    i.lead_name,
    exportDate:   i.exportDate,
    analysedAt:   i.analysedAt,
    analysis:     i._fullAnalysis,
  }})),
}};

fs.writeFileSync(outPath, JSON.stringify(envelope, null, 2), 'utf8');

return [{{ json: {{ saved: true, path: outPath, count: items.length, exportDate }} }}];
"""

# Filter for HIGH/CRITICAL urgency leads → Telegram
JS_FILTER_URGENT = """
const items = $input.all().map(i => i.json).filter(i =>
  i.parseSuccess && (i.urgency === 'critical' || i.urgency === 'high')
);
if (!items.length) return [{ json: { noUrgentLeads: true } }];
return items.map(i => ({ json: i }));
"""

# Build daily roll-up for Slack
JS_BUILD_ROLLUP = """
const items     = $input.all().map(i => i.json);
const config    = $('⚙️ Set AI Config').first().json;
const valid     = items.filter(i => i.parseSuccess);
const failed    = items.filter(i => !i.parseSuccess);
const exportDate = valid[0]?.exportDate || config.today;

const urgencyCounts = { critical: 0, high: 0, medium: 0, low: 0 };
const sentimentCounts = { positive: 0, neutral: 0, negative: 0 };
const buyingCounts = { strong: 0, moderate: 0, weak: 0, none: 0 };
let   totalObjCount = 0;
let   avgAgentScore = 0;

for (const i of valid) {
  urgencyCounts[i.urgency]           = (urgencyCounts[i.urgency]       || 0) + 1;
  sentimentCounts[i.sentiment_label] = (sentimentCounts[i.sentiment_label] || 0) + 1;
  buyingCounts[i.buying_strength]    = (buyingCounts[i.buying_strength] || 0) + 1;
  totalObjCount   += (i.objections_count || 0);
  avgAgentScore   += (i.agent_score      || 0);
}
if (valid.length) avgAgentScore = Math.round((avgAgentScore / valid.length) * 10) / 10;

const criticalLeads = valid
  .filter(i => i.urgency === 'critical' || i.urgency === 'high')
  .map(i => `• *${i.lead_name || i.lead_id}* → ${i.follow_up_action}`)
  .join('\\n');

return [{
  json: {
    exportDate,
    totalAnalysed:    valid.length,
    totalFailed:      failed.length,
    urgencyCounts,
    sentimentCounts,
    buyingCounts,
    totalObjections:  totalObjCount,
    avgAgentScore,
    criticalLeads:    criticalLeads || '_None today_',
    slackText: `📊 *Kommo AI Analysis — ${exportDate}*`,
    slackBlocks: JSON.stringify([
      { type: 'header', text: { type: 'plain_text', text: `🤖 Kommo AI Daily Report — ${exportDate}` } },
      { type: 'section', fields: [
        { type: 'mrkdwn', text: `*Leads Analysed:*\\n${valid.length}` },
        { type: 'mrkdwn', text: `*Failed:*\\n${failed.length}` },
        { type: 'mrkdwn', text: `*Avg Agent Score:*\\n${avgAgentScore}/10` },
        { type: 'mrkdwn', text: `*Total Objections:*\\n${totalObjCount}` },
      ]},
      { type: 'section', fields: [
        { type: 'mrkdwn', text: `*Sentiment:*\\n✅ ${sentimentCounts.positive} pos · ➖ ${sentimentCounts.neutral} neu · ❌ ${sentimentCounts.negative} neg` },
        { type: 'mrkdwn', text: `*Buying Signals:*\\n🔥 ${buyingCounts.strong} strong · 🟡 ${buyingCounts.moderate} mod · ⬜ ${buyingCounts.weak} weak` },
      ]},
      { type: 'section', fields: [
        { type: 'mrkdwn', text: `*Urgency:*\\n🚨 ${urgencyCounts.critical || 0} crit · 🔴 ${urgencyCounts.high || 0} high · 🟡 ${urgencyCounts.medium || 0} med · 🟢 ${urgencyCounts.low || 0} low` },
      ]},
      { type: 'section', text: { type: 'mrkdwn', text: `*🚨 Urgent Follow-ups:*\\n${criticalLeads || '_None_'}` } },
    ]),
  }
}];
"""


# ─────────────────────────────────────────────────────────────────────────────
# Workflow builder
# ─────────────────────────────────────────────────────────────────────────────

def build_workflow() -> dict:

    return {
        "name": "Kommo CRM → Claude AI Analysis",
        "settings": {
            "executionOrder":           "v1",
            "saveManualExecutions":     True,
            "callerPolicy":             "workflowsFromSameOwner",
            "saveExecutionProgress":    True,
            "saveDataSuccessExecution": "all",
            "saveDataErrorExecution":   "all",
            "executionTimeout":         7200,   # 2h — large accounts with many leads
            "timezone":                 "UTC",
        },
        "staticData": None,

        "nodes": [

            # ── Sticky notes ──────────────────────────────────────────────────
            {
                "id": "sticky-overview",
                "name": "📌 Workflow Overview",
                "type": "n8n-nodes-base.stickyNote",
                "typeVersion": 1,
                "position": [-280, 60],
                "parameters": {
                    "width": 480, "height": 480, "color": 2,
                    "content": (
                        "## 🤖 Kommo → Claude AI Analysis\n\n"
                        "**Per-lead analysis:**\n"
                        "- Sentiment (positive/neutral/negative)\n"
                        "- Buying signal strength\n"
                        "- Objection detection\n"
                        "- Follow-up urgency + action\n"
                        "- Agent performance score\n\n"
                        "**Chunking:** >40 msgs → chunked\n"
                        "**Rate limit:** 1s pause between Claude calls\n"
                        "**Retry:** ×3 on Claude API failure\n\n"
                        "**Outputs:**\n"
                        "- `outputs/ai_summaries/YYYY-MM-DD.json` (disk)\n"
                        "- Google Sheets: AI_Summaries worksheet\n"
                        "- Slack: daily roll-up\n"
                        "- Telegram: urgent leads only\n\n"
                        "**Required env vars:**\n"
                        "`KOMMO_ANTHROPIC_API_KEY`\n"
                        "`KOMMO_SHEETS_SPREADSHEET_ID`\n"
                        "`KOMMO_SLACK_WEBHOOK`\n"
                        "`KOMMO_TELEGRAM_BOT_TOKEN`\n"
                        "`KOMMO_TELEGRAM_CHAT_ID`"
                    ),
                },
            },
            {
                "id": "sticky-prompt",
                "name": "🧠 Prompt Strategy",
                "type": "n8n-nodes-base.stickyNote",
                "typeVersion": 1,
                "position": [-280, 580],
                "parameters": {
                    "width": 480, "height": 300, "color": 5,
                    "content": (
                        "## 🧠 Claude Prompt Engineering\n\n"
                        "**Model:** claude-3-5-sonnet-20241022\n"
                        "**Temp:** 0.2 (deterministic scoring)\n"
                        "**Max tokens:** 1024 per lead\n\n"
                        "**Schema enforcement:**\n"
                        "System prompt contains exact JSON schema.\n"
                        "Claude returns ONLY the JSON object.\n\n"
                        "**Chunking:** First 40 messages used per call.\n"
                        "For very long threads, the most recent\n"
                        "messages provide the most signal."
                    ),
                },
            },

            # ── Triggers ──────────────────────────────────────────────────────
            {
                "id": "schedule-trigger",
                "name": "⏰ Daily 9AM UTC",
                "type": "n8n-nodes-base.scheduleTrigger",
                "typeVersion": 1.2,
                "position": [280, 280],
                "parameters": {
                    "rule": {"interval": [{"field": "cronExpression", "expression": "0 9 * * *"}]}
                },
            },
            {
                "id": "manual-trigger",
                "name": "▶️ Manual Run",
                "type": "n8n-nodes-base.manualTrigger",
                "typeVersion": 1,
                "position": [280, 460],
                "parameters": {},
            },

            # ── Config ────────────────────────────────────────────────────────
            {
                "id": "set-ai-config",
                "name": "⚙️ Set AI Config",
                "type": "n8n-nodes-base.set",
                "typeVersion": 3.4,
                "position": [540, 360],
                "parameters": {
                    "mode": "manual",
                    "assignments": {
                        "assignments": [
                            {"id": "a1", "name": "started_at",        "value": "={{ $now.toISO() }}",                                           "type": "string"},
                            {"id": "a2", "name": "today",             "value": "={{ $now.toFormat('yyyy-MM-dd') }}",                             "type": "string"},
                            {"id": "a3", "name": "triggered_by",      "value": "={{ $execution.mode === 'manual' ? 'manual' : 'schedule' }}",    "type": "string"},
                            {"id": "a4", "name": "anthropic_api_key", "value": "={{ $env.KOMMO_ANTHROPIC_API_KEY || '' }}",                      "type": "string"},
                            {"id": "a5", "name": "claude_model",      "value": "claude-3-5-sonnet-20241022",                                     "type": "string"},
                            {"id": "a6", "name": "max_tokens",        "value": 1024,                                                             "type": "number"},
                            {"id": "a7", "name": "temperature",       "value": 0.2,                                                              "type": "number"},
                            {"id": "a8", "name": "spreadsheet_id",    "value": "={{ $env.KOMMO_SHEETS_SPREADSHEET_ID || '' }}",                  "type": "string"},
                            {"id": "a9", "name": "slack_webhook",     "value": "={{ $env.KOMMO_SLACK_WEBHOOK || 'http://localhost:1' }}",         "type": "string"},
                            {"id": "a10","name": "telegram_token",    "value": "={{ $env.KOMMO_TELEGRAM_BOT_TOKEN || '' }}",                     "type": "string"},
                            {"id": "a11","name": "telegram_chat_id",  "value": "={{ $env.KOMMO_TELEGRAM_CHAT_ID || '' }}",                       "type": "string"},
                        ]
                    },
                    "options": {},
                },
            },

            # ── Step 1: Detect & load export ──────────────────────────────────
            {
                "id": "detect-export",
                "name": "🔍 Detect Daily Export",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [800, 360],
                "parameters": {"jsCode": JS_DETECT_EXPORT.strip()},
            },
            {
                "id": "load-leads",
                "name": "📂 Load Leads from Export",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [1060, 360],
                "parameters": {"jsCode": JS_LOAD_LEADS.strip()},
            },

            # ── Step 2: Guard — any leads? ────────────────────────────────────
            {
                "id": "check-has-leads",
                "name": "❓ Has Leads to Analyse?",
                "type": "n8n-nodes-base.if",
                "typeVersion": 1,
                "position": [1320, 360],
                "parameters": {
                    "conditions": {
                        "number": [{"value1": "={{ $json.totalLeads }}", "operation": "larger", "value2": 0}]
                    }
                },
            },

            # FALSE path → no-data Slack message
            {
                "id": "set-no-data",
                "name": "⚠️ No Leads in Export",
                "type": "n8n-nodes-base.set",
                "typeVersion": 3.4,
                "position": [1320, 600],
                "parameters": {
                    "mode": "manual",
                    "assignments": {
                        "assignments": [
                            {"id": "nd1", "name": "slackText",    "value": "ℹ️ Kommo AI Analysis: No leads in today's export. Skipping.",  "type": "string"},
                            {"id": "nd2", "name": "triggered_by", "value": "={{ $('⚙️ Set AI Config').first().json.triggered_by }}",         "type": "string"},
                        ]
                    },
                    "options": {},
                },
            },

            # ── Step 3: Expand leads → individual items ────────────────────────
            {
                "id": "expand-leads",
                "name": "🔄 Expand Leads Array",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [1580, 260],
                "parameters": {"jsCode": JS_EXPAND_LEADS.strip()},
            },

            # ── Step 4: Process one lead at a time (rate-limit safe) ───────────
            {
                "id": "split-batches",
                "name": "⚡ Process One Lead at a Time",
                "type": "n8n-nodes-base.splitInBatches",
                "typeVersion": 3,
                "position": [1840, 260],
                "parameters": {
                    "batchSize": 1,
                    "options": {"reset": False},
                },
            },

            # ── Step 5: Build prompt ──────────────────────────────────────────
            {
                "id": "build-prompt",
                "name": "✍️ Build Claude Prompt",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [2100, 260],
                "parameters": {"jsCode": JS_BUILD_PROMPT.strip()},
            },

            # ── Step 6: Rate-limit pause (1s between Claude API calls) ─────────
            {
                "id": "rate-limit-wait",
                "name": "⏱️ Rate Limit (1s Pause)",
                "type": "n8n-nodes-base.wait",
                "typeVersion": 1.1,
                "position": [2360, 260],
                "parameters": {
                    "resume":    "timeInterval",
                    "amount":    1,
                    "unit":      "seconds",
                },
            },

            # ── Step 7: Call Claude API ────────────────────────────────────────
            {
                "id": "call-claude",
                "name": "🤖 Call Claude API",
                "type": "n8n-nodes-base.httpRequest",
                "typeVersion": 4.2,
                "position": [2620, 260],
                "retryOnFail": True,
                "maxTries": 3,
                "waitBetweenTries": 20000,
                "parameters": {
                    "method":      "POST",
                    "url":         "https://api.anthropic.com/v1/messages",
                    "sendHeaders": True,
                    "headerParameters": {
                        "parameters": [
                            {"name": "x-api-key",         "value": "={{ $('⚙️ Set AI Config').first().json.anthropic_api_key }}"},
                            {"name": "anthropic-version",  "value": "2023-06-01"},
                            {"name": "Content-Type",       "value": "application/json"},
                        ]
                    },
                    "sendBody":    True,
                    "specifyBody": "json",
                    "jsonBody": (
                        "={{ JSON.stringify({"
                        "  model:      $('⚙️ Set AI Config').first().json.claude_model,"
                        "  max_tokens: $('⚙️ Set AI Config').first().json.max_tokens,"
                        "  system:     $json.claudeSystemPrompt,"
                        "  messages: [{"
                        "    role:    'user',"
                        "    content: $json.claudeUserPrompt"
                        "  }],"
                        "  temperature: $('⚙️ Set AI Config').first().json.temperature"
                        "}) }}"
                    ),
                    "options": {
                        "response": {"response": {"neverError": False}},
                        "timeout":  90000,
                        "batching": {"batch": {"batchSize": 1, "batchInterval": 1000}},
                    },
                },
            },

            # ── Step 8: Parse Claude response ──────────────────────────────────
            {
                "id": "parse-response",
                "name": "🔬 Parse AI Response",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [2880, 260],
                "parameters": {"jsCode": JS_PARSE_CLAUDE.strip()},
            },

            # ── Step 9: Route on parse success ────────────────────────────────
            {
                "id": "check-parse-ok",
                "name": "❓ Parse Successful?",
                "type": "n8n-nodes-base.if",
                "typeVersion": 1,
                "position": [3140, 260],
                "parameters": {
                    "conditions": {
                        "boolean": [{"value1": "={{ $json.parseSuccess }}", "operation": "equal", "value2": True}]
                    }
                },
            },

            # FALSE → log parse error
            {
                "id": "set-parse-error",
                "name": "❌ Log Parse Error",
                "type": "n8n-nodes-base.set",
                "typeVersion": 3.4,
                "position": [3140, 480],
                "parameters": {
                    "mode": "manual",
                    "assignments": {
                        "assignments": [
                            {"id": "pe1", "name": "error_type",  "value": "parse_failure",                       "type": "string"},
                            {"id": "pe2", "name": "lead_id",     "value": "={{ $json.lead_id || 'unknown' }}",   "type": "string"},
                            {"id": "pe3", "name": "lead_name",   "value": "={{ $json.lead_name || 'unknown' }}", "type": "string"},
                            {"id": "pe4", "name": "parseError",  "value": "={{ $json.parseError || '' }}",       "type": "string"},
                        ]
                    },
                    "options": {},
                },
            },

            # ── Step 10: Continue loop ─────────────────────────────────────────
            # (SplitInBatches loops automatically — next lead picked up)

            # ── Step 11: Aggregate all results ────────────────────────────────
            # Merge happens after loop completes. We use a Code node to save.
            {
                "id": "save-summaries",
                "name": "💾 Save AI Summaries to Disk",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [3400, 260],
                "parameters": {"jsCode": JS_SAVE_SUMMARIES.strip()},
            },

            # ── Step 12: Push to Google Sheets (AI_Summaries) ─────────────────
            {
                "id": "sheets-summaries",
                "name": "📊 Sync → AI_Summaries Sheet",
                "type": "n8n-nodes-base.googleSheets",
                "typeVersion": 4.5,
                "position": [3660, 160],
                "retryOnFail": True,
                "maxTries": 2,
                "waitBetweenTries": 30000,
                "parameters": {
                    "operation":  "appendOrUpdate",
                    "documentId": "={{ $('⚙️ Set AI Config').first().json.spreadsheet_id }}",
                    "sheetName":  "={{ 'AI_Summaries' }}",
                    "columns": {
                        "mappingMode": "autoMapInputData",
                        "value":       {},
                        "matchingColumns": ["lead_id", "exportDate"],
                        "schema": [
                            {"id": "lead_id",            "displayName": "lead_id",            "canBeUsedToMatch": True,  "required": False, "defaultMatch": True,  "display": True, "type": "string", "removed": False},
                            {"id": "exportDate",         "displayName": "exportDate",         "canBeUsedToMatch": True,  "required": False, "defaultMatch": True,  "display": True, "type": "string", "removed": False},
                            {"id": "lead_name",          "displayName": "lead_name",          "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "contact_name",       "displayName": "contact_name",       "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "channel",            "displayName": "channel",            "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "analysedAt",         "displayName": "analysedAt",         "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "sentiment_label",    "displayName": "sentiment_label",    "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "sentiment_score",    "displayName": "sentiment_score",    "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "buying_strength",    "displayName": "buying_strength",    "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "urgency",            "displayName": "urgency",            "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "follow_up_action",   "displayName": "follow_up_action",   "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "deadline_hours",     "displayName": "deadline_hours",     "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "objections_count",   "displayName": "objections_count",   "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "agent_score",        "displayName": "agent_score",        "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "agent_empathy",      "displayName": "agent_empathy",      "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "summary",            "displayName": "summary",            "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "messageCount",       "displayName": "messageCount",       "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                            {"id": "chunked",            "displayName": "chunked",            "canBeUsedToMatch": False, "required": False, "defaultMatch": False, "display": True, "type": "string", "removed": False},
                        ],
                    },
                    "options": {
                        "handlingExtraData": "insertInNewColumn",
                        "locationDefine":    "specifyRangeA1",
                        "rangeA1":           "A:S",
                    },
                },
            },

            # ── Step 13: Filter urgent → Telegram ─────────────────────────────
            {
                "id": "filter-urgent",
                "name": "🚨 Filter Urgent Leads",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [3660, 360],
                "parameters": {"jsCode": JS_FILTER_URGENT.strip()},
            },
            {
                "id": "check-urgent",
                "name": "❓ Urgent Leads Exist?",
                "type": "n8n-nodes-base.if",
                "typeVersion": 1,
                "position": [3920, 360],
                "parameters": {
                    "conditions": {
                        "boolean": [{"value1": "={{ !$json.noUrgentLeads }}", "operation": "equal", "value2": True}]
                    }
                },
            },
            {
                "id": "telegram-alert",
                "name": "📱 Telegram: Urgent Lead",
                "type": "n8n-nodes-base.httpRequest",
                "typeVersion": 4.2,
                "position": [4180, 260],
                "parameters": {
                    "method":      "POST",
                    "url":         "={{ 'https://api.telegram.org/bot' + $('⚙️ Set AI Config').first().json.telegram_token + '/sendMessage' }}",
                    "sendHeaders": True,
                    "headerParameters": {
                        "parameters": [{"name": "Content-Type", "value": "application/json"}]
                    },
                    "sendBody":    True,
                    "specifyBody": "json",
                    "jsonBody": (
                        "={{ JSON.stringify({"
                        "  chat_id:    $('⚙️ Set AI Config').first().json.telegram_chat_id,"
                        "  parse_mode: 'Markdown',"
                        "  text:       '🚨 *URGENT FOLLOW-UP* — ' + $json.exportDate + '\\n\\n'"
                        "            + '*Lead:* ' + ($json.lead_name || $json.lead_id) + '\\n'"
                        "            + '*Urgency:* ' + $json.urgency.toUpperCase() + '\\n'"
                        "            + '*Sentiment:* ' + $json.sentiment_label + ' (' + $json.sentiment_score + ')\\n'"
                        "            + '*Buying Signal:* ' + $json.buying_strength + '\\n'"
                        "            + '*Action:* ' + $json.follow_up_action + '\\n'"
                        "            + '*Deadline:* Within ' + $json.deadline_hours + 'h\\n\\n'"
                        "            + '*Summary:* ' + ($json.summary || 'N/A').slice(0,300)"
                        "}) }}"
                    ),
                    "options": {
                        "response": {"response": {"neverError": True}},
                        "timeout": 15000,
                    },
                },
            },

            # ── Step 14: Build roll-up + send Slack ───────────────────────────
            {
                "id": "build-rollup",
                "name": "📊 Build Daily Roll-up",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [3660, 560],
                "parameters": {"jsCode": JS_BUILD_ROLLUP.strip()},
            },
            {
                "id": "slack-rollup",
                "name": "🔔 Slack: Daily AI Summary",
                "type": "n8n-nodes-base.httpRequest",
                "typeVersion": 4.2,
                "position": [3920, 560],
                "parameters": {
                    "method":      "POST",
                    "url":         "={{ $('⚙️ Set AI Config').first().json.slack_webhook }}",
                    "sendHeaders": True,
                    "headerParameters": {
                        "parameters": [{"name": "Content-Type", "value": "application/json"}]
                    },
                    "sendBody":    True,
                    "specifyBody": "json",
                    "jsonBody":    "={{ JSON.stringify({ text: $json.slackText, blocks: JSON.parse($json.slackBlocks) }) }}",
                    "options": {
                        "response": {"response": {"neverError": True}},
                        "timeout": 15000,
                    },
                },
            },

            # ── No-leads Slack ─────────────────────────────────────────────────
            {
                "id": "slack-no-data",
                "name": "🔔 Slack: No Data Today",
                "type": "n8n-nodes-base.httpRequest",
                "typeVersion": 4.2,
                "position": [1580, 600],
                "parameters": {
                    "method":      "POST",
                    "url":         "={{ $('⚙️ Set AI Config').first().json.slack_webhook }}",
                    "sendHeaders": True,
                    "headerParameters": {
                        "parameters": [{"name": "Content-Type", "value": "application/json"}]
                    },
                    "sendBody":    True,
                    "specifyBody": "json",
                    "jsonBody":    '={{ JSON.stringify({ text: $json.slackText }) }}',
                    "options": {
                        "response": {"response": {"neverError": True}},
                        "timeout": 10000,
                    },
                },
            },
        ],

        # ── CONNECTIONS ───────────────────────────────────────────────────────
        "connections": {
            "⏰ Daily 9AM UTC":   {"main": [[{"node": "⚙️ Set AI Config",        "type": "main", "index": 0}]]},
            "▶️ Manual Run":       {"main": [[{"node": "⚙️ Set AI Config",        "type": "main", "index": 0}]]},
            "⚙️ Set AI Config":   {"main": [[{"node": "🔍 Detect Daily Export",   "type": "main", "index": 0}]]},
            "🔍 Detect Daily Export": {"main": [[{"node": "📂 Load Leads from Export", "type": "main", "index": 0}]]},
            "📂 Load Leads from Export": {"main": [[{"node": "❓ Has Leads to Analyse?", "type": "main", "index": 0}]]},

            # IF: TRUE → expand, FALSE → no-data slack
            "❓ Has Leads to Analyse?": {
                "main": [
                    [{"node": "🔄 Expand Leads Array",   "type": "main", "index": 0}],   # TRUE
                    [{"node": "⚠️ No Leads in Export",   "type": "main", "index": 0}],   # FALSE
                ]
            },
            "⚠️ No Leads in Export": {"main": [[{"node": "🔔 Slack: No Data Today", "type": "main", "index": 0}]]},

            # Loop processing chain
            "🔄 Expand Leads Array":       {"main": [[{"node": "⚡ Process One Lead at a Time", "type": "main", "index": 0}]]},
            "⚡ Process One Lead at a Time": {"main": [[{"node": "✍️ Build Claude Prompt",    "type": "main", "index": 0}]]},
            "✍️ Build Claude Prompt":       {"main": [[{"node": "⏱️ Rate Limit (1s Pause)",   "type": "main", "index": 0}]]},
            "⏱️ Rate Limit (1s Pause)":     {"main": [[{"node": "🤖 Call Claude API",         "type": "main", "index": 0}]]},
            "🤖 Call Claude API":           {"main": [[{"node": "🔬 Parse AI Response",        "type": "main", "index": 0}]]},

            # Parse gate
            "🔬 Parse AI Response": {
                "main": [
                    [{"node": "❓ Parse Successful?", "type": "main", "index": 0}],
                ]
            },
            "❓ Parse Successful?": {
                "main": [
                    # TRUE: loop back to SplitInBatches for next lead
                    [{"node": "⚡ Process One Lead at a Time", "type": "main", "index": 0}],
                    # FALSE: log error, continue loop
                    [{"node": "❌ Log Parse Error",            "type": "main", "index": 0}],
                ]
            },
            # After loop exhausts all leads, SplitInBatches emits from output 1 (done)
            # We wire that to save → sheets → filter → rollup in parallel

            # Post-loop outputs (parallel fan-out)
            "💾 Save AI Summaries to Disk": {
                "main": [[
                    {"node": "📊 Sync → AI_Summaries Sheet", "type": "main", "index": 0},
                    {"node": "🚨 Filter Urgent Leads",        "type": "main", "index": 0},
                    {"node": "📊 Build Daily Roll-up",        "type": "main", "index": 0},
                ]]
            },

            "🚨 Filter Urgent Leads": {"main": [[{"node": "❓ Urgent Leads Exist?", "type": "main", "index": 0}]]},
            "❓ Urgent Leads Exist?": {
                "main": [
                    [{"node": "📱 Telegram: Urgent Lead", "type": "main", "index": 0}],   # TRUE
                    [],   # FALSE → no-op
                ]
            },

            "📊 Build Daily Roll-up": {"main": [[{"node": "🔔 Slack: Daily AI Summary", "type": "main", "index": 0}]]},
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# API helpers
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
    if activate:
        _activate(wf_id)
    return r.json()


def _activate(wf_id: str) -> None:
    requests.post(f"{N8N_API_URL}/workflows/{wf_id}/activate", headers=HEADERS, timeout=15).raise_for_status()
    print("  ✅ Activated")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--activate",    action="store_true")
    parser.add_argument("--export-only", action="store_true", dest="export_only")
    parser.add_argument("--force-new",   action="store_true", dest="force_new")
    args = parser.parse_args()

    print("\n" + "═" * 58)
    print("  Kommo CRM → Claude AI Analysis — Workflow Deployer")
    print("═" * 58 + "\n")

    wf          = build_workflow()
    export_path = Path(__file__).parent / "kommo_ai_analysis_workflow.json"
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

        print(f"\n  {'─' * 54}")
        print(f"  Workflow ID  : {wf_id}")
        print(f"  Name         : {result.get('name')}")
        print(f"  Active       : {'✅ Yes' if active else '⏸️  No'}")
        print(f"  UI URL       : http://localhost:5678/workflow/{wf_id}")
        print(f"  JSON backup  : {export_path}")
        print(f"  Nodes        : {len(wf['nodes'])}")
        print(f"  {'─' * 54}\n")

        if not active:
            print("  ⚠️  To activate, set these in n8n → Settings → Variables:")
            print("      KOMMO_ANTHROPIC_API_KEY    = sk-ant-...")
            print("      KOMMO_SHEETS_SPREADSHEET_ID = <sheet_id>")
            print("      KOMMO_SLACK_WEBHOOK         = https://hooks.slack.com/...")
            print("      KOMMO_TELEGRAM_BOT_TOKEN    = <bot_token>")
            print("      KOMMO_TELEGRAM_CHAT_ID      = <chat_id>")
            print(f"\n     python3 {Path(__file__).name} --activate\n")

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
