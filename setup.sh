#!/usr/bin/env bash
# =============================================================================
# setup.sh — Kommo CRM Integration — One-command project setup
#
# Usage:
#   chmod +x setup.sh
#   ./setup.sh
#
# What this does:
#   1. Verifies Python 3.11+
#   2. Creates a virtual environment (.venv)
#   3. Installs all dependencies from requirements.txt
#   4. Copies .env.example → .env (if .env doesn't exist)
#   5. Generates a Fernet encryption key and patches it into .env
#   6. Prints next steps
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

ok()   { echo -e "${GREEN}  ✓${RESET}  $1"; }
info() { echo -e "${CYAN}  →${RESET}  $1"; }
warn() { echo -e "${YELLOW}  ⚠${RESET}  $1"; }
err()  { echo -e "${RED}  ✗${RESET}  $1"; exit 1; }

echo ""
echo -e "${BOLD}============================================================${RESET}"
echo -e "${BOLD}  Kommo CRM Integration — Project Setup${RESET}"
echo -e "${BOLD}============================================================${RESET}"
echo ""

# ---------------------------------------------------------------------------
# 1. Check Python version
# ---------------------------------------------------------------------------
info "Checking Python version..."

if ! command -v python3 &>/dev/null; then
    err "python3 not found. Install Python 3.11+ and retry."
fi

PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [[ "$PY_MAJOR" -lt 3 || ( "$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 11 ) ]]; then
    err "Python 3.11+ required. Found: Python $PY_VERSION"
fi

ok "Python $PY_VERSION detected"

# ---------------------------------------------------------------------------
# 2. Create virtual environment
# ---------------------------------------------------------------------------
if [[ -d ".venv" ]]; then
    warn "Virtual environment already exists at .venv — skipping creation"
else
    info "Creating virtual environment..."
    python3 -m venv .venv
    ok "Virtual environment created at .venv"
fi

# Activate
source .venv/bin/activate
ok "Virtual environment activated"

# ---------------------------------------------------------------------------
# 3. Upgrade pip silently
# ---------------------------------------------------------------------------
info "Upgrading pip..."
pip install --quiet --upgrade pip
ok "pip upgraded"

# ---------------------------------------------------------------------------
# 4. Install dependencies
# ---------------------------------------------------------------------------
info "Installing dependencies from requirements.txt..."
pip install --quiet -r requirements.txt
ok "Dependencies installed"

# ---------------------------------------------------------------------------
# 5. Copy .env.example → .env
# ---------------------------------------------------------------------------
if [[ -f ".env" ]]; then
    warn ".env already exists — skipping copy"
else
    info "Creating .env from .env.example..."
    cp .env.example .env
    ok ".env created"
fi

# ---------------------------------------------------------------------------
# 6. Generate and inject Fernet encryption key
# ---------------------------------------------------------------------------
info "Generating token encryption key..."

FERNET_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

if grep -q "your_fernet_key_here" .env 2>/dev/null; then
    # Replace the placeholder on both macOS (BSD sed) and Linux (GNU sed)
    if [[ "$(uname)" == "Darwin" ]]; then
        sed -i '' "s|your_fernet_key_here|${FERNET_KEY}|g" .env
    else
        sed -i "s|your_fernet_key_here|${FERNET_KEY}|g" .env
    fi
    ok "TOKEN_ENCRYPTION_KEY generated and written to .env"
else
    warn "TOKEN_ENCRYPTION_KEY already set in .env — skipping"
fi

# ---------------------------------------------------------------------------
# 7. Create required directories
# ---------------------------------------------------------------------------
mkdir -p outputs/data outputs/errors logs state
ok "Output directories ensured (outputs/, logs/, state/)"

# ---------------------------------------------------------------------------
# 8. Print next steps
# ---------------------------------------------------------------------------
echo ""
echo -e "${BOLD}============================================================${RESET}"
echo -e "${BOLD}  Setup Complete${RESET}"
echo -e "${BOLD}============================================================${RESET}"
echo ""
echo -e "  ${YELLOW}Next steps:${RESET}"
echo ""
echo -e "  ${CYAN}1.${RESET} Edit ${BOLD}.env${RESET} and fill in your Kommo credentials:"
echo "     KOMMO_CLIENT_ID=..."
echo "     KOMMO_CLIENT_SECRET=..."
echo "     KOMMO_REDIRECT_URI=..."
echo "     KOMMO_ACCOUNT_DOMAIN=..."
echo ""
echo -e "  ${CYAN}2.${RESET} Activate virtual environment (if not already active):"
echo "     source .venv/bin/activate"
echo ""
echo -e "  ${CYAN}3.${RESET} Run OAuth authorization (one-time):"
echo "     python run_auth.py"
echo ""
echo -e "  ${CYAN}4.${RESET} Run full extraction:"
echo "     python run_extraction.py"
echo ""
echo -e "  ${CYAN}5.${RESET} Or run individual extractors:"
echo "     python run_leads.py"
echo "     python run_pipelines.py"
echo "     python run_tasks.py --slim"
echo ""
echo -e "  ${CYAN}6.${RESET} Run tests:"
echo "     pytest"
echo ""
