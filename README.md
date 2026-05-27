# Kommo CRM Automation Platform

> Enterprise-grade CRM data extraction, AI processing, and workflow automation — fully containerized and production-ready.

[![Deploy → Production](https://github.com/meshezabelxrufus/KOMMO/actions/workflows/deploy_production.yml/badge.svg)](https://github.com/meshezabelxrufus/KOMMO/actions/workflows/deploy_production.yml)
[![Daily Pipeline](https://github.com/meshezabelxrufus/KOMMO/actions/workflows/daily_sync.yml/badge.svg)](https://github.com/meshezabelxrufus/KOMMO/actions/workflows/daily_sync.yml)

---

## What This Does

Fully automated daily pipeline that:

1. **Extracts** all leads, tasks, contacts, pipelines and WhatsApp chat messages from Kommo CRM
2. **Normalizes** chat histories into AI-ready flat JSON format
3. **Syncs** to Google Sheets (leads, messages, daily summary)
4. **Backs up** daily exports to Google Drive
5. **Runs AI analysis** via Claude (Anthropic) — lead scoring + insights
6. **Sends alerts** to Slack + email on failures or important events
7. **Runs on a schedule** — every day at 6AM UTC, fully automated

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   Hetzner Ubuntu VPS                    │
│                                                         │
│  Internet → Caddy (443/HTTPS, auto SSL)                 │
│                 ↓                                       │
│         kommo-n8n (5678, internal)                      │
│            ↙           ↘                                │
│  kommo-pipeline      postgres (internal)                │
│  (Python backend)    (n8n database)                     │
│                                                         │
│  Shared volumes: outputs/ logs/ daily_exports/ state/   │
└─────────────────────────────────────────────────────────┘
```

**5 n8n Workflows:**
| Workflow | Purpose | Schedule |
|---|---|---|
| Daily AI Pipeline | Master orchestrator | 6AM UTC daily |
| Google Sheets Sync | Sync leads + messages to Sheets | Triggered by pipeline |
| Google Drive Upload | Backup daily JSON exports | Triggered by pipeline |
| Claude AI Analysis | Lead scoring + insights | Triggered by pipeline |
| Notifications Hub | Slack + email alerts | Event-driven |

---

## Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11 |
| Workflow Engine | n8n (self-hosted) |
| Database | PostgreSQL 16 |
| Reverse Proxy | Caddy 2 (auto SSL) |
| Containerization | Docker + Docker Compose |
| CI/CD | GitHub Actions |
| AI | Anthropic Claude |
| Google | Sheets API + Drive API v3 |

---

## Repository Structure

```
KOMMO/
├── api/                    # Kommo API client modules
├── auth/                   # OAuth2 token management
├── config/                 # Settings and configuration
├── extractors/             # Lead, task, chat extractors
├── integrations/           # Google Sheets + Drive clients
├── normalizers/            # Data normalization + daily export
├── utils/                  # Shared utilities
├── state/                  # Sync state persistence
├── tests/                  # Test suite
│
├── main.py                 # Master pipeline orchestrator
├── run_*.py                # Individual step runners
│
├── n8n_exports/            # Production workflow JSONs (5 workflows)
├── deploy_*_workflow.py    # n8n workflow deploy scripts
├── deploy_workflows_to_production.py  # Production import script
│
├── Dockerfile              # Multi-stage Python image (non-root)
├── docker-compose.yml      # Full 4-service production stack
├── caddy/Caddyfile         # Reverse proxy + SSL config
├── deploy.sh               # One-command VPS deployment
├── docker-build.sh         # Image build + smoke test
│
├── secrets/
│   └── .env.production.example   # All required env variables
│
└── .github/workflows/
    ├── daily_sync.yml             # Daily data pipeline (GitHub Actions)
    └── deploy_production.yml      # Production VPS deployment
```

---

## Quick Start — Local Development

```bash
# 1. Clone and set up
git clone https://github.com/meshezabelxrufus/KOMMO.git
cd KOMMO
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env with your Kommo credentials

# 3. Authenticate with Kommo (one-time)
python run_auth.py

# 4. Run the pipeline
python main.py --auto-incremental
```

---

## Production Deployment — Hetzner VPS

### Step 1 — Provision Server
- Spin up **Hetzner CX21** (Ubuntu 22.04, 2 vCPU, 4GB RAM) — ~$5.99/mo
- Follow `server_setup_guide.md` (in docs folder) for initial server hardening

### Step 2 — DNS
Add two A records pointing to your Hetzner IP:
```
n8n.yourdomain.com  →  <HETZNER_IP>
api.yourdomain.com  →  <HETZNER_IP>
```

### Step 3 — Configure Secrets
Copy and fill in the production env file on the server:
```bash
cp secrets/.env.production.example /opt/kommo-platform/secrets/.env
chmod 600 /opt/kommo-platform/secrets/.env
nano /opt/kommo-platform/secrets/.env
```

### Step 4 — Deploy
```bash
# On the server as the kommo user:
git clone https://github.com/meshezabelxrufus/KOMMO.git /opt/kommo-platform/app
cd /opt/kommo-platform/app
./deploy.sh
```

### Step 5 — Import Workflows
```bash
# From your Mac, pointed at production:
python3 deploy_workflows_to_production.py \
  --n8n-url https://n8n.yourdomain.com \
  --api-key YOUR_PROD_API_KEY
```

---

## GitHub Actions CI/CD

### Secrets Required

Set these in **GitHub → Settings → Secrets → Actions**:

#### For the Daily Pipeline (`daily_sync.yml`)
| Secret | Description |
|---|---|
| `KOMMO_CLIENT_ID` | Kommo OAuth App Client ID |
| `KOMMO_CLIENT_SECRET` | Kommo OAuth App Client Secret |
| `KOMMO_REDIRECT_URI` | OAuth redirect URI |
| `KOMMO_SUBDOMAIN` | Your Kommo subdomain |
| `TOKEN_ENCRYPTION_KEY` | Fernet key for token encryption |
| `KOMMO_TOKEN_STORE` | Base64-encoded `auth/token_store.json` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full contents of service account JSON |
| `GOOGLE_SHEETS_SPREADSHEET_ID` | Target spreadsheet ID |
| `GOOGLE_DRIVE_FOLDER_ID` | Target Drive folder ID |
| `SLACK_WEBHOOK_URL` | Slack webhook for failure alerts |

**Encode your token store:**
```bash
base64 -i auth/token_store.json | tr -d '\n'
# Paste the output as KOMMO_TOKEN_STORE
```

#### For the Production Deploy (`deploy_production.yml`)
| Secret | Description |
|---|---|
| `VPS_HOST` | Hetzner server IP |
| `VPS_SSH_PORT` | SSH port (e.g. `2222`) |
| `VPS_USER` | Deploy user (e.g. `kommo`) |
| `VPS_SSH_KEY` | Private SSH key (PEM format) |
| `N8N_DOMAIN` | n8n domain (e.g. `n8n.yourdomain.com`) |
| `SLACK_WEBHOOK_URL` | Same Slack webhook |

**Generate a deployment SSH key:**
```bash
ssh-keygen -t ed25519 -C "github-deploy" -f ~/.ssh/kommo_deploy_key -N ""
# Add public key to server:  ssh-copy-id -i ~/.ssh/kommo_deploy_key.pub -p 2222 kommo@<IP>
# Paste private key contents as VPS_SSH_KEY secret in GitHub
```

---

## Operations

```bash
# On the VPS server
./deploy.sh              # Initial full deployment
./deploy.sh update       # Pull latest code + restart
./deploy.sh restart      # Restart all containers
./deploy.sh status       # Container health + resource usage
./deploy.sh logs         # Stream live logs
./deploy.sh backup       # Backup PostgreSQL + data volumes
./deploy.sh stop         # Stop all (data preserved)
```

---

## System Status

| Component | Status |
|---|---|
| Kommo Extraction (Leads, Tasks, Contacts, Chats) | ✅ Production |
| Incremental Sync Engine | ✅ Production |
| AI-Ready JSON Export | ✅ Production |
| Google Sheets Sync | ✅ Production |
| Google Drive Upload | ✅ Production |
| Claude AI Analysis | ✅ Production |
| Slack + Email Notifications | ✅ Production |
| Docker / Containerized | ✅ Production |
| Caddy HTTPS + SSL | ✅ Production |
| PostgreSQL-backed n8n | ✅ Production |
| GitHub Actions CI/CD | ✅ Production |
| **Platform** | **✅ 100% Complete** |

---

## Security

- All secrets in environment variables — never in source code
- Non-root Docker user (UID 1001) inside all containers
- PostgreSQL, n8n, Python — internal Docker network only
- Only ports 80/443/2222 exposed to the internet
- TLS 1.2/1.3 only — TLS 1.0/1.1 disabled
- HSTS with preload + full security header suite
- Fail2ban + UFW on the host
- SSH keys only — password auth disabled
