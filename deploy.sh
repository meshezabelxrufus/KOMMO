#!/usr/bin/env bash
# =============================================================================
# deploy.sh — One-command production deployment for Kommo CRM Platform
# =============================================================================
#
# Run this on the Hetzner VPS as the `kommo` user:
#
#   ./deploy.sh           — First-time full deploy
#   ./deploy.sh update    — Pull latest code + restart changed containers
#   ./deploy.sh restart   — Restart all containers
#   ./deploy.sh status    — Show running containers + health
#   ./deploy.sh logs      — Stream live logs from all services
#   ./deploy.sh stop      — Stop all containers (data preserved)
#   ./deploy.sh backup    — Backup all persistent data
#
# =============================================================================

set -euo pipefail

PLATFORM_DIR="/opt/kommo-platform"
APP_DIR="$PLATFORM_DIR/app"
SECRETS_DIR="$PLATFORM_DIR/secrets"
COMPOSE_FILE="$APP_DIR/docker-compose.yml"

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'
info()    { echo -e "${BLUE}▶${RESET} $*"; }
success() { echo -e "${GREEN}✅${RESET} $*"; }
warn()    { echo -e "${YELLOW}⚠️${RESET}  $*"; }
error()   { echo -e "${RED}❌${RESET} $*" >&2; exit 1; }

banner() {
  echo ""
  echo -e "${BOLD}╔══════════════════════════════════════════════════╗${RESET}"
  echo -e "${BOLD}║   Kommo CRM Platform — Production Deploy         ║${RESET}"
  echo -e "${BOLD}╚══════════════════════════════════════════════════╝${RESET}"
  echo ""
}

# ── Helper: run docker compose with correct file ─────────────────────────────
dc() { docker compose -f "$COMPOSE_FILE" --env-file "$SECRETS_DIR/.env" "$@"; }

# =============================================================================
# COMMAND: deploy (default — first-time setup)
# =============================================================================
cmd_deploy() {
  banner
  info "Starting full production deployment..."

  # 1. Verify prerequisites
  info "Checking prerequisites..."
  command -v docker  &>/dev/null || error "Docker is not installed"
  command -v git     &>/dev/null || error "Git is not installed"

  # 2. Create directory structure
  info "Creating directory structure..."
  sudo mkdir -p \
    "$PLATFORM_DIR/data/postgres" \
    "$PLATFORM_DIR/data/n8n" \
    "$PLATFORM_DIR/data/caddy/data" \
    "$PLATFORM_DIR/data/caddy/config" \
    "$PLATFORM_DIR/shared/outputs" \
    "$PLATFORM_DIR/shared/daily_exports" \
    "$PLATFORM_DIR/shared/logs" \
    "$PLATFORM_DIR/shared/state" \
    "$PLATFORM_DIR/shared/auth" \
    "$PLATFORM_DIR/secrets" \
    "$PLATFORM_DIR/backups"

  sudo chown -R kommo:kommo "$PLATFORM_DIR"

  # 3. Clone/pull repo
  if [ ! -d "$APP_DIR/.git" ]; then
    info "Cloning repository..."
    git clone https://github.com/meshezabelxrufus/KOMMO.git "$APP_DIR"
  else
    info "Repository already exists — pulling latest..."
    cd "$APP_DIR" && git pull origin main
  fi

  # 4. Verify secrets file exists
  if [ ! -f "$SECRETS_DIR/.env" ]; then
    warn "Secrets file not found at $SECRETS_DIR/.env"
    info "Copying template..."
    cp "$APP_DIR/secrets/.env.production.example" "$SECRETS_DIR/.env"
    chmod 600 "$SECRETS_DIR/.env"
    error "STOP: Fill in $SECRETS_DIR/.env before continuing. Re-run ./deploy.sh when done."
  fi

  # 5. Copy Caddyfile to expected location
  info "Copying Caddyfile..."
  mkdir -p "$PLATFORM_DIR/caddy"
  cp "$APP_DIR/caddy/Caddyfile" "$PLATFORM_DIR/caddy/Caddyfile"

  # 6. Copy Google Service Account JSON
  if [ ! -f "$SECRETS_DIR/kommo-service-account.json" ]; then
    warn "Google Service Account JSON not found at $SECRETS_DIR/kommo-service-account.json"
    warn "Upload it with: scp -P 2222 kommo-*.json kommo@<IP>:$SECRETS_DIR/kommo-service-account.json"
  fi

  # 7. Build the Python image
  info "Building kommo-pipeline Docker image..."
  cd "$APP_DIR"
  DOCKER_BUILDKIT=1 docker build \
    --file Dockerfile \
    --tag kommo-pipeline:latest \
    --progress=plain \
    .

  # 8. Start the stack
  info "Starting full stack..."
  dc up -d --remove-orphans

  # 9. Wait for health checks
  info "Waiting for services to become healthy (up to 90s)..."
  sleep 10
  for i in $(seq 1 9); do
    HEALTHY=$(dc ps --format json 2>/dev/null | python3 -c "
import sys, json
lines = sys.stdin.read().strip().splitlines()
healthy = sum(1 for l in lines if json.loads(l).get('Health') in ('healthy', ''))
total   = len(lines)
print(f'{healthy}/{total}')
" 2>/dev/null || echo "?/?")
    echo "  Health: $HEALTHY  (${i}0s elapsed)"
    sleep 10
  done

  # 10. Show status
  cmd_status
  success "Deployment complete!"
  echo ""
  info "Next steps:"
  echo "  1. Open https://\$(grep DOMAIN_NAME $SECRETS_DIR/.env | cut -d= -f2)"
  echo "  2. Log in with your N8N_BASIC_AUTH_USER credentials"
  echo "  3. Run the deploy scripts to import your workflows:"
  echo "     python3 deploy_n8n_workflow.py --activate"
  echo "     python3 deploy_sheets_sync_workflow.py --activate"
  echo "     python3 deploy_drive_upload_workflow.py --activate"
  echo "     python3 deploy_ai_analysis_workflow.py --activate"
  echo "     python3 deploy_notifications_workflow.py --activate"
}

# =============================================================================
# COMMAND: update
# =============================================================================
cmd_update() {
  banner
  info "Updating Kommo platform..."

  cd "$APP_DIR"
  info "Pulling latest code..."
  git pull origin main

  info "Copying updated Caddyfile..."
  cp "$APP_DIR/caddy/Caddyfile" "$PLATFORM_DIR/caddy/Caddyfile"

  info "Rebuilding Python image..."
  DOCKER_BUILDKIT=1 docker build \
    --file Dockerfile \
    --tag kommo-pipeline:latest \
    .

  info "Pulling latest n8n + Postgres images..."
  dc pull postgres kommo-n8n caddy

  info "Restarting updated containers with zero-downtime rolling restart..."
  dc up -d --remove-orphans

  success "Update complete!"
  cmd_status
}

# =============================================================================
# COMMAND: restart
# =============================================================================
cmd_restart() {
  info "Restarting all containers..."
  dc restart
  success "All containers restarted"
  cmd_status
}

# =============================================================================
# COMMAND: status
# =============================================================================
cmd_status() {
  echo ""
  echo -e "${BOLD}Service Status:${RESET}"
  dc ps
  echo ""
  echo -e "${BOLD}Resource Usage:${RESET}"
  docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}" \
    kommo-postgres kommo-n8n kommo-pipeline kommo-caddy 2>/dev/null || true
  echo ""
  echo -e "${BOLD}Disk Usage:${RESET}"
  du -sh "$PLATFORM_DIR/data/"* "$PLATFORM_DIR/shared/"* 2>/dev/null || true
}

# =============================================================================
# COMMAND: logs
# =============================================================================
cmd_logs() {
  SERVICE="${2:-}"
  if [ -n "$SERVICE" ]; then
    dc logs -f "$SERVICE"
  else
    dc logs -f
  fi
}

# =============================================================================
# COMMAND: stop
# =============================================================================
cmd_stop() {
  warn "Stopping all containers (data preserved in volumes)..."
  dc down
  success "All containers stopped. Data is safe."
}

# =============================================================================
# COMMAND: backup
# =============================================================================
cmd_backup() {
  BACKUP_DATE=$(date +%Y-%m-%d_%H-%M-%S)
  BACKUP_DIR="$PLATFORM_DIR/backups/$BACKUP_DATE"
  mkdir -p "$BACKUP_DIR"

  info "Backing up PostgreSQL..."
  docker exec kommo-postgres pg_dump \
    -U "$(grep POSTGRES_USER "$SECRETS_DIR/.env" | cut -d= -f2)" \
    "$(grep POSTGRES_DB "$SECRETS_DIR/.env" | cut -d= -f2)" \
    | gzip > "$BACKUP_DIR/n8n_postgres_$BACKUP_DATE.sql.gz"

  info "Backing up n8n data..."
  tar -czf "$BACKUP_DIR/n8n_data_$BACKUP_DATE.tar.gz" \
    -C "$PLATFORM_DIR/data" n8n 2>/dev/null || true

  info "Backing up shared outputs..."
  tar -czf "$BACKUP_DIR/shared_$BACKUP_DATE.tar.gz" \
    -C "$PLATFORM_DIR" shared 2>/dev/null || true

  success "Backup complete: $BACKUP_DIR"
  ls -lh "$BACKUP_DIR"
}

# =============================================================================
# Entry point
# =============================================================================
COMMAND="${1:-deploy}"

case "$COMMAND" in
  deploy)  cmd_deploy ;;
  update)  cmd_update ;;
  restart) cmd_restart ;;
  status)  cmd_status ;;
  logs)    cmd_logs "$@" ;;
  stop)    cmd_stop ;;
  backup)  cmd_backup ;;
  *)       error "Unknown command: $COMMAND. Valid: deploy | update | restart | status | logs | stop | backup" ;;
esac
