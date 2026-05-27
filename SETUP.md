# Kommo CRM Integration — Setup & Operations Guide

This guide covers the full Kommo CRM Automation System:

| Milestone | What it does |
|-----------|-------------|
| **M1** | OAuth 2.0 auth · Kommo extraction (Leads, Pipelines, Tasks, Contacts, Chats) · Incremental sync |
| **M2** | Google Sheets sync · Daily AI-ready JSON exports · Google Drive upload for Claude AI |

---

## 1. Clone Repository

```bash
git clone https://github.com/meshezabelxrufus/KOMMO.git
cd KOMMO
```

---

## 2. Create Virtual Environment

Requires **Python 3.11+**.

```bash
python3 -m venv .venv
source .venv/bin/activate       # macOS / Linux
# .venv\Scripts\activate        # Windows
```

---

## 3. Install Dependencies

```bash
pip install -r requirements.txt
```

---

## 4. Configure Environment Variables

```bash
cp .env.example .env
```

Open `.env` and fill in **all** required values. The file is split by integration:

### 4a. Kommo API (required for M1)

```
KOMMO_SUBDOMAIN=yourcompany          # e.g. yourcompany.kommo.com
KOMMO_CLIENT_ID=...
KOMMO_CLIENT_SECRET=...
KOMMO_REDIRECT_URI=http://localhost:8080/callback
```

### 4b. Google Service Account (required for M2)

Create a Service Account in [Google Cloud Console](https://console.cloud.google.com/):
1. IAM & Admin → Service Accounts → Create
2. Download the JSON key file
3. Share your Spreadsheet **and** Drive folder with the service account email (Editor role)

```
GOOGLE_SERVICE_ACCOUNT_FILE=/path/to/service-account-key.json
```

### 4c. Google Sheets (required for Sheets sync)

1. Create a new Google Spreadsheet
2. Copy the spreadsheet ID from the URL:
   `https://docs.google.com/spreadsheets/d/**<SPREADSHEET_ID>**/edit`

```
GOOGLE_SHEETS_SPREADSHEET_ID=1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms
```

### 4d. Google Drive (required for Drive upload)

1. Create a folder in Google Drive
2. Copy the folder ID from the URL:
   `https://drive.google.com/drive/folders/**<FOLDER_ID>**`
3. Share the folder with the service account email (Editor role)

```
GOOGLE_DRIVE_FOLDER_ID=1A2B3C4D5E6F7G8H9I0J
```

---

## 5. Milestone 1 — Kommo Extraction

### 5a. OAuth Authorization (one-time)

Run the authorization flow once to obtain and store your access tokens:

```bash
python run_auth.py
```

Follow the on-screen link, grant access in the browser, and paste the authorization code back into the terminal. Tokens are stored securely and refreshed automatically on every subsequent run.

### 5b. Full Extraction

Extract all entities — Leads, Pipelines, Tasks, Contacts, and Chats/Messages:

```bash
python main.py
```

Outputs are written to `outputs/`:

| File | Contents |
|------|----------|
| `outputs/leads.json` | All leads with custom fields |
| `outputs/pipelines.json` | Pipeline + stage definitions |
| `outputs/tasks.json` | All tasks |
| `outputs/contacts.json` | All contacts |
| `outputs/messages_flat.json` | Flattened chat messages (AI-ready) |

### 5c. Incremental Extraction

After the first full run, fetch only new/updated records:

```bash
python main.py --auto-incremental
```

### 5d. Individual Entity Runners

```bash
python run_leads.py           # Leads only
python run_pipelines.py       # Pipelines only
python run_tasks.py           # Tasks only
python run_contacts.py        # Contacts only
python run_chats.py           # Chats + messages (generates messages_flat.json)
```

---

## 6. Milestone 2 — Google Sheets Sync

Pushes extracted data to a Google Spreadsheet in three worksheets:
`Leads`, `Messages`, `Daily_Summary`.

**Prerequisites:** Complete Step 4b + 4c and run `python main.py` at least once.

### Sync all worksheets (recommended daily command)

```bash
python run_sheets_sync.py
```

### Selective sync

```bash
python run_sheets_sync.py --leads-only       # Only the Leads worksheet
python run_sheets_sync.py --messages-only    # Only the Messages worksheet
python run_sheets_sync.py --no-summary       # Skip the Daily_Summary row
```

### Options

| Flag | Description |
|------|-------------|
| `--leads-only` | Sync only the Leads worksheet |
| `--messages-only` | Sync only the Messages worksheet |
| `--no-summary` | Skip writing the Daily_Summary audit row |
| `--output-dir PATH` | Use a custom outputs directory (default: `outputs/`) |
| `--debug` | Enable verbose DEBUG logging |

**Exit codes:** `0` = success · `1` = sync errors · `2` = auth/config failure

---

## 7. Milestone 2 — Daily AI Export

Reads `outputs/messages_flat.json` and generates one structured JSON file per calendar day in `daily_exports/`. Each file groups messages by lead, sorted chronologically — ready for Claude AI.

**Prerequisites:** Run `python run_chats.py` first to generate `messages_flat.json`.

### Export latest day (recommended daily command)

```bash
python run_daily_export.py
```

### Export options

```bash
python run_daily_export.py --date 2025-01-15     # Specific date
python run_daily_export.py --all                  # Every available date
python run_daily_export.py --list-dates           # Preview available dates (no files written)
```

### Output structure

```
daily_exports/
    2025-01-14.json
    2025-01-15.json
    ...
```

Each file:

```json
{
  "_meta": { "date": "2025-01-15", "generated_at": "...", "total_leads": 12, "total_messages": 340 },
  "leads": [
    {
      "lead_id": 123,
      "stats": { "message_count": 28, "first_message_at": "...", "last_message_at": "..." },
      "messages": [ ... ]
    }
  ]
}
```

### Options

| Flag | Description |
|------|-------------|
| `--date YYYY-MM-DD` | Export a specific date |
| `--all` | Generate exports for all dates |
| `--list-dates` | List available dates and exit |
| `--input FILE` | Custom path to messages_flat.json |
| `--export-dir DIR` | Custom output directory (default: `daily_exports/`) |
| `--debug` | Enable verbose DEBUG logging |

**Exit codes:** `0` = success · `1` = export errors · `2` = input missing/invalid · `3` = date format error

---

## 8. Milestone 2 — Google Drive Upload

Uploads daily AI-ready JSON exports to a designated Google Drive folder for Claude AI access. Files are deduplicated (update-in-place) so Drive links stay stable.

**Prerequisites:** Complete Step 4b + 4d. Run `python run_daily_export.py` first.

### Upload latest export (recommended daily command)

```bash
python run_drive_upload.py
```

### Upload options

```bash
python run_drive_upload.py --date 2025-01-15         # Specific date
python run_drive_upload.py --all                      # All local exports
python run_drive_upload.py --all --skip-existing      # All, skip already uploaded
python run_drive_upload.py --file /path/to/file.json  # Any specific file
python run_drive_upload.py --list                     # List files currently in Drive
python run_drive_upload.py --delete 2025-01-15.json   # Delete from Drive
```

### Options

| Flag | Description |
|------|-------------|
| `--date YYYY-MM-DD` | Upload a specific date's export |
| `--all` | Upload all local YYYY-MM-DD.json files |
| `--skip-existing` | With `--all`, skip files already in Drive |
| `--file PATH` | Upload any specific local file |
| `--list` | List files in Drive (no upload) |
| `--delete FILENAME` | Delete a file from Drive |
| `--export-dir DIR` | Custom local exports directory (default: `daily_exports/`) |
| `--debug` | Enable verbose DEBUG logging |

**Exit codes:** `0` = success · `1` = upload errors · `2` = auth/config failure · `3` = local file not found

---

## 9. Recommended Daily Workflow

Run these four commands each day (or schedule via GitHub Actions / cron):

```bash
# 1. Pull latest CRM data
python main.py --auto-incremental

# 2. Generate today's AI-ready export
python run_daily_export.py

# 3. Upload to Drive for Claude AI
python run_drive_upload.py

# 4. Push to Google Sheets (optional: for human review)
python run_sheets_sync.py
```

Or use the Makefile shortcuts:

```bash
make extract-incremental
make daily-export
make drive-upload
make sheets-sync
```

---

## 10. Makefile Quick Reference

```bash
make help                  # Show all available commands
make setup                 # One-command project setup (creates venv + installs deps)
make auth                  # Run OAuth authorization (one-time)

# ── Extraction ───────────────────────────────────────────
make extract               # Full extraction (all entities)
make extract-incremental   # Incremental extraction (new/updated only)
make leads                 # Leads only
make pipelines             # Pipelines only
make tasks                 # Tasks only
make contacts              # Contacts only
make chats                 # Chats + messages

# ── Milestone 2 ──────────────────────────────────────────
make daily-export          # Generate today's AI-ready JSON export
make daily-export-all      # Generate exports for ALL available dates
make drive-upload          # Upload latest export to Google Drive
make drive-upload-all      # Upload all local exports to Drive
make sheets-sync           # Sync all worksheets to Google Sheets

# ── Tests ────────────────────────────────────────────────
make test                  # Run all tests
make test-verbose          # Tests with verbose output
make test-coverage         # Tests + HTML coverage report

# ── State & Cleanup ──────────────────────────────────────
make state                 # Print current sync state
make reset-state           # Force full re-extraction next run
make clean                 # Remove outputs + state + cache
```

---

## 11. Troubleshooting

### "Missing env var" errors
→ Run `cat .env` and verify all required variables are set.
→ Never commit `.env` to git — it is already listed in `.gitignore`.

### "Service account has no access"
→ Confirm you shared the Spreadsheet/Drive folder directly with the service account email (visible inside the JSON key file under `"client_email"`), with **Editor** role.

### "Token expired" / Kommo auth errors
→ Run `python run_auth.py` again to re-authorize.

### "messages_flat.json not found"
→ Run `python run_chats.py` (or `python main.py`) to generate the extraction first.

### "No exports in daily_exports/"
→ Run `python run_daily_export.py` before uploading to Drive.

### Drive links broken after re-upload
→ This should not happen — the system always updates the existing file in place, preserving `file_id` and therefore the shareable link.

---

## 12. GitHub Actions Automation

The pipeline runs automatically every day via `.github/workflows/`. Set the following repository secrets:

| Secret | Value |
|--------|-------|
| `KOMMO_SUBDOMAIN` | Your Kommo subdomain |
| `KOMMO_CLIENT_ID` | Kommo OAuth client ID |
| `KOMMO_CLIENT_SECRET` | Kommo OAuth client secret |
| `KOMMO_REDIRECT_URI` | OAuth redirect URI |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full contents of the service account JSON key |
| `GOOGLE_SHEETS_SPREADSHEET_ID` | Target spreadsheet ID |
| `GOOGLE_DRIVE_FOLDER_ID` | Target Drive folder ID |

> **Note:** Credentials are injected at runtime via GitHub Actions encrypted secrets and are never logged or stored in the repository.
