#!/usr/bin/env bash
# setup-pi.sh — one-shot setup for Parrot Binder on a Raspberry Pi Zero 2
# Run as the default `pi` user (not root).
# Usage:  bash deploy/setup-pi.sh
set -euo pipefail

APP_DIR="$HOME/parrot-binder"
SERVICE_FILE="/etc/systemd/system/parrot-binder.service"
PORT=5000

echo "=== Parrot Binder — Pi Zero 2 setup ==="

# ── 1. Clone / update repo ────────────────────────────────────────────────────
if [ -d "$APP_DIR/.git" ]; then
  echo "Updating existing repo …"
  git -C "$APP_DIR" pull
else
  echo "Cloning repo into $APP_DIR …"
  git clone https://github.com/chadley-therat/Parrot-Trader.git "$APP_DIR"
fi

cd "$APP_DIR"

# ── 2. Python venv + deps ─────────────────────────────────────────────────────
echo "Setting up Python virtual environment …"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q
echo "  Dependencies installed"

# ── 3. Download card data ─────────────────────────────────────────────────────
if [ -f "data/cards.db" ]; then
  echo "Database already present — skipping download."
  echo "  Run:  .venv/bin/python scripts/download_data.py --update  to refresh"
else
  echo "Downloading card data (this takes ~5–10 min on a Pi Zero 2) …"
  # Use API key if set in environment
  .venv/bin/python scripts/download_data.py
fi

# ── 4. Install systemd service ────────────────────────────────────────────────
echo "Installing systemd service …"
sudo cp deploy/parrot-binder.service "$SERVICE_FILE"
# Patch WorkingDirectory and ExecStart with actual paths
sudo sed -i "s|/home/pi/parrot-binder|$APP_DIR|g" "$SERVICE_FILE"
sudo sed -i "s|User=pi|User=$(whoami)|g" "$SERVICE_FILE"

sudo systemctl daemon-reload
sudo systemctl enable parrot-binder
sudo systemctl restart parrot-binder

echo ""
echo "=== Done! ==="
echo ""
echo "Server status:  sudo systemctl status parrot-binder"
echo "Live logs:      sudo journalctl -u parrot-binder -f"
echo ""

# Get Pi's IP
PI_IP=$(hostname -I | awk '{print $1}')
echo "Open on your phone:  http://$PI_IP:$PORT"
echo "Add to home screen for PWA install."
echo ""
echo "Optional — download card images (~450 MB small, ~3 GB with large):"
echo "  .venv/bin/python scripts/download_images.py"
echo "  .venv/bin/python scripts/download_images.py --large"
