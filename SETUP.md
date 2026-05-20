# Kommo CRM Integration — Quick Setup

This guide will walk you through setting up and running the Kommo CRM Automation System locally. The system is designed to securely authenticate with Kommo, extract your CRM data, and produce AI-ready datasets.

## 1. Clone Repository

First, clone the repository to your local machine and navigate into the project directory:

```bash
git clone https://github.com/meshezabelxrufus/KOMMO.git
cd KOMMO
```

## 2. Create Virtual Environment

We recommend running the system in an isolated Python environment to prevent dependency conflicts. The project requires **Python 3.11+**.

```bash
python3 -m venv .venv
source .venv/bin/activate
```

## 3. Install Dependencies

Install the required production packages:

```bash
pip install -r requirements.txt
```

## 4. Configure Sandbox Credentials

The system requires API credentials to securely connect to Kommo. 

1. Copy the provided environment template:
   ```bash
   cp .env.example .env
   ```
2. Open the `.env` file and insert the credentials. *(Note: I will provide sandbox credentials separately via a secure channel).*

## 5. Run OAuth Authorization

Before extracting data, the system must authorize the Kommo account and generate a secure access token. This only needs to be done once.

```bash
python run_auth.py
```
Follow the on-screen prompt to click the authorization link, grant access, and paste the resulting authorization code back into the terminal. The system will automatically securely store and manage future token refreshes.

## 6. Run Extraction

You can now run the full data pipeline. This command extracts Leads, Pipelines, Tasks, Contacts, and Chats/Messages.

```bash
python main.py
```

*Note: The system supports incremental sync. After this initial full extraction, subsequent runs will automatically fetch only new or updated records.*

## 7. Review Outputs

Once the extraction is complete, navigate to the `outputs/` directory to view the structured datasets. 

The most critical file for AI analysis is **`outputs/messages_flat.json`**. This dataset combines leads, contacts, and chat messages into a single chronological schema optimized specifically for ingestion by Claude AI.
