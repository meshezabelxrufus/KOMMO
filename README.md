# Kommo CRM Automation Pipeline — Milestone 2

## Project Overview
This project provides a robust, production-grade automation pipeline designed to extract data from Kommo CRM, process it into an AI-ready flat format, and prepare it for downstream analytics and Google ecosystem integrations.

It acts as a secure bridge between your Kommo CRM account and your reporting infrastructure.

## Features (Milestone 2)
- **Full Extraction Pipeline**: Automated, paginated fetching of Leads, Contacts, Tasks, Pipelines, and Chats/Messages.
- **Incremental Sync System**: Intelligently fetches only records updated since the last run, significantly reducing API load and execution time.
- **AI-Ready JSON Export System**: Automatically denormalizes complex chat histories into a flat, chronologically sorted `messages_flat.json` format perfectly suited for LLM/Claude analysis.
- **Daily Analytics Engine**: Generates a structured `daily_summary.json` providing execution statistics, data volumes, and system health metrics.
- **Orchestrator Automation**: A master `main.py` controller that sequentially orchestrates extraction, processing, and export phases.
- **Resilience & Reliability**: Built-in exponential backoff retries, API rate-limit protections, and graceful error handling.
- **GitHub Actions Ready**: Configured for automated daily execution.

## Architecture Overview
The pipeline follows a modular, phase-based architecture:

**Kommo CRM** → **Python Engine** (Authentication & Extraction) → **Processing Layer** (Incremental State & Normalization) → **JSON Outputs** (AI-ready Data) → **Analytics Layer** (Run Summaries) → **(Google Integration Layer - Pending)**

## Setup Instructions

### 1. Install Dependencies
Ensure you have Python 3.10+ installed. Create a virtual environment and install the required packages:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Environment Variables
Copy the example configuration file and fill in your credentials:
```bash
cp .env.example .env
```
Ensure your Kommo OAuth credentials (`KOMMO_CLIENT_ID`, `KOMMO_CLIENT_SECRET`, `KOMMO_SUBDOMAIN`, `KOMMO_REDIRECT_URI`) are filled in.

### 3. Authenticate with Kommo
Run the one-time authentication script to authorize the application and securely store your tokens:
```bash
python run_auth.py
```

## How to Run

To run the full automated pipeline incrementally (only fetching new data since the last run):
```bash
source .venv/bin/activate
python main.py --auto-incremental
```

## Outputs Explanation
The system generates local files within the `outputs/`, `daily_exports/`, and `logs/` directories:

- `outputs/messages_flat.json`: The core AI deliverable. A denormalized, chronologically sorted array of all messages and chat history joined with Lead metadata.
- `outputs/leads.json` / `tasks.json` / `pipelines.json`: Raw extracted entity data.
- `daily_exports/YYYY-MM-DD.json`: A daily isolated snapshot of conversations grouped by Lead, optimized for batch AI processing.
- `logs/analytics_YYYY-MM-DD.json`: A structured summary of the day's pipeline run, detailing records processed, durations, and any non-critical failures.

> [!WARNING]
> ## ⚠️ External Dependencies (Google Integrations)
>
> The Google Sheets synchronization and Google Drive upload modules have been **fully built and implemented** into the codebase. However, they are currently **NOT active** because the required Google credentials have not yet been provided.
> 
> **Required Items to Activate Google Integrations:**
> 1. **Google Service Account JSON file**: Needed to authenticate server-to-server with Google.
> 2. **Google Sheets Spreadsheet ID**: The target spreadsheet for daily syncs.
> 3. **Google Drive Folder ID**: The target folder for uploading daily JSON exports.
>
> **Status**: The pipeline runs flawlessly up to the JSON export phase and will gracefully skip the Google integrations, ensuring the rest of your data extraction completes successfully. Once the above credentials are provided in the `.env` file, the Google integrations will automatically become fully operational.

## System Status
| Component | Status |
| :--- | :--- |
| Kommo Extraction | ✅ Complete |
| JSON Pipeline & Flat Formatting | ✅ Complete |
| Analytics Engine | ✅ Complete |
| Orchestrator (`main.py`) | ✅ Complete |
| Google Sheets Sync | ⚠️ Built (Pending Credentials) |
| Google Drive Upload | ⚠️ Built (Pending Credentials) |
| **End-to-End Pipeline** | **90% Complete** |

## Troubleshooting
- **Missing Environment Variables**: If `python main.py` exits immediately, check that your `.env` file exists and is populated.
- **OAuth Issues**: If token validation fails, re-run `python run_auth.py` to generate a fresh token set.
- **API Rate Limits**: The system automatically retries on 429 Too Many Requests. If persistent, consider running the pipeline less frequently.
