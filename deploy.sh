#!/bin/bash
# =============================================================
# One-command VPS Deploy Script
# Usage: Run from VPS as root:
#   curl -sL https://raw.githubusercontent.com/MuajAmin/chess.com-bot-host/main/deploy.sh | bash
# OR copy-paste this entire script into the VPS terminal.
# =============================================================

set -e

echo "============================================"
echo "  Chess.com Bot — Full Deploy"
echo "============================================"

# --- Install git if missing ---
if ! command -v git &>/dev/null; then
    echo "[*] Installing git..."
    apt-get update -y && apt-get install -y git
fi

# --- Clone or update the repo ---
BOT_DIR="/home/bot/chess.com_bot_host"

if [ -d "$BOT_DIR/.git" ]; then
    echo "[*] Repo exists, pulling latest..."
    cd "$BOT_DIR"
    git pull origin main
else
    echo "[*] Cloning repository..."
    mkdir -p /home/bot
    git clone https://github.com/MuajAmin/chess.com-bot-host.git "$BOT_DIR"
    cd "$BOT_DIR"
fi

# --- Run the setup script ---
echo "[*] Running setup_vps.sh..."
bash setup_vps.sh

# --- Copy config if not exists ---
if [ ! -f "$BOT_DIR/config.yaml" ]; then
    cp "$BOT_DIR/config.yaml.example" "$BOT_DIR/config.yaml"
    echo ""
    echo "============================================"
    echo "  ACTION REQUIRED: Edit config.yaml"
    echo "============================================"
    echo "  nano $BOT_DIR/config.yaml"
    echo ""
    echo "  Set your chess.com username, password,"
    echo "  and engine weights path."
    echo "============================================"
fi

# --- Download Maia weights if not present ---
WEIGHTS_DIR="$BOT_DIR/weights"
if [ ! -f "$WEIGHTS_DIR/maia-1900.pb.gz" ]; then
    echo "[*] Downloading Maia-1900 weights..."
    mkdir -p "$WEIGHTS_DIR"
    wget -q -O "$WEIGHTS_DIR/maia-1900.pb.gz" \
        "https://github.com/CSSLab/maia-chess/releases/download/v1.0/maia-1900.pb.gz" || \
        echo "WARNING: Failed to download Maia weights. Download manually."
fi

# --- Fix permissions ---
if id -u bot >/dev/null 2>&1; then
    chown -R bot:$(id -gn bot) /home/bot
fi

echo ""
echo "============================================"
echo "  Deploy Complete!"
echo "============================================"
echo ""
echo "  Start the bot:"
echo "    systemctl start chess-bot"
echo ""
echo "  View logs:"
echo "    journalctl -u chess-bot -f"
echo ""
echo "  Edit config:"
echo "    nano $BOT_DIR/config.yaml"
echo "============================================"
