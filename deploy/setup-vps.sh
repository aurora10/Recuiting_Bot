#!/usr/bin/env bash
# --------------------------------------------------------------------
# One-time VPS setup script for Recruiter Bot
# Run this ONCE on your ARM64 Ubuntu VPS as root (or a user with sudo).
#
# Usage:
#   ssh root@<VPS_IP>
#   curl -O https://raw.githubusercontent.com/aurora10/Recuiting_Bot/main/deploy/setup-vps.sh
#   bash setup-vps.sh
# --------------------------------------------------------------------

set -euo pipefail

APP_DIR="/srv/recruiter_bot"
REPO_URL="git@github.com:aurora10/Recuiting_Bot.git"

echo "=== Recruiter Bot VPS Setup ==="
echo ""

# ---- 1. Install Docker if missing ----
if ! command -v docker &>/dev/null; then
    echo "[1/5] Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
    echo "Docker installed."
else
    echo "[1/5] Docker already installed — skipping."
fi

# ---- 2. Install Docker Compose plugin if missing ----
if ! docker compose version &>/dev/null; then
    echo "[2/5] Installing Docker Compose plugin..."
    apt-get update -qq
    apt-get install -y -qq docker-compose-plugin
    echo "Docker Compose plugin installed."
else
    echo "[2/5] Docker Compose already installed — skipping."
fi

# ---- 3. Clone repository ----
if [ -d "$APP_DIR/.git" ]; then
    echo "[3/5] Repository already exists at $APP_DIR — pulling latest..."
    cd "$APP_DIR"
    git pull origin main
else
    echo "[3/5] Cloning repository..."
    mkdir -p "$APP_DIR"
    git clone "$REPO_URL" "$APP_DIR"
    cd "$APP_DIR"
fi

# ---- 4. Create persistent data directories ----
echo "[4/5] Creating data directories..."
mkdir -p "$APP_DIR/data/media"
mkdir -p "$APP_DIR/data/dossiers"
chmod 755 "$APP_DIR/data" "$APP_DIR/data/media" "$APP_DIR/data/dossiers"

# ---- 5. Create .env placeholder ----
if [ ! -f "$APP_DIR/.env" ]; then
    echo "[5/5] Creating .env from template..."
    cat > "$APP_DIR/.env" <<'EOF'
API_ID=CHANGEME
API_HASH=CHANGEME
OPENAI_API_KEY=CHANGEME
PHONE=CHANGEME
PHONE_CODE=
GMAIL_USER=CHANGEME
GMAIL_PASS=CHANGEME
EOF
    echo ".env template created — FILL IT IN NOW:"
else
    echo "[5/5] .env already exists — preserving it (DO NOT overwrite)."
fi

echo ""
echo "=============================================="
echo " SETUP COMPLETE"
echo "=============================================="
echo ""
echo "NEXT STEPS (follow in order):"
echo ""
echo "1. EDIT the .env file with real values:"
echo "     vim $APP_DIR/.env"
echo "   (or: nano $APP_DIR/.env)"
echo ""
echo "   Set PHONE_CODE to your Telegram verification code"
echo "   for the FIRST run only. Remove it after login."
echo ""
echo "2. If you want to migrate your existing Telegram session:"
echo "   scp recruitment_session.session root@<VPS>:$APP_DIR/data/"
echo "   (from your Mac, not the VPS)"
echo ""
echo "3. Configure GitHub Secrets at:"
echo "   https://github.com/aurora10/Recuiting_Bot/settings/secrets/actions"
echo ""
echo "   Required secrets:"
echo "     VPS_HOST           = your VPS IP or hostname"
echo "     VPS_USER           = root (or your SSH user)"
echo "     VPS_SSH_KEY        = private SSH key (cat ~/.ssh/id_rsa)"
echo "     DOCKER_HUB_USERNAME = aurora1010"
echo "     DOCKER_HUB_TOKEN   = Docker Hub access token (create at hub.docker.com/settings/security)"
echo ""
echo "4. Start the bot manually for the first time:"
echo "     cd $APP_DIR"
echo "     docker compose pull"
echo "     docker compose up -d"
echo "     docker compose logs -f"
echo ""
echo "   Watch logs. If Telegram asks for a code,"
echo "   set PHONE_CODE in .env and restart:"
echo "     docker compose restart"
echo ""
echo "5. After manual verify, push to 'main' branch and"
echo "   GitHub Actions will auto-deploy from now on."
echo ""
echo "Done!"