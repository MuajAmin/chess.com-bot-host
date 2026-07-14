#!/bin/bash
# =============================================================
# Chess.com Lc0 Bot — VPS Setup Script (Ubuntu/Debian)
# Run as root: sudo bash setup_vps.sh
# =============================================================

set -e

echo "============================================"
echo "  Chess.com Lc0 Bot — VPS Setup"
echo "============================================"

# --- System Update ---
echo "[1/7] Updating system packages..."
apt-get update -y && apt-get upgrade -y

# --- Python 3 + pip ---
echo "[2/7] Installing Python 3 and pip..."
apt-get install -y python3 python3-pip python3-venv

# --- BLAS library (required for Lc0 CPU backend) ---
echo "[3/7] Installing BLAS libraries..."
apt-get install -y libopenblas-dev libopenblas0

# --- Playwright dependencies ---
echo "[4/7] Installing Playwright system dependencies..."
apt-get install -y \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    libwayland-client0 \
    fonts-liberation \
    xdg-utils

# --- Create bot user and directory ---
echo "[5/7] Setting up bot directory..."
BOT_USER="bot"
BOT_DIR="/home/bot/chess.com_bot_host"

if ! id -u "$BOT_USER" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir /home/bot --shell /usr/sbin/nologin "$BOT_USER"
fi
BOT_GROUP="$(id -gn "$BOT_USER")"

mkdir -p "$BOT_DIR"
mkdir -p "$BOT_DIR/logs"
mkdir -p "$BOT_DIR/weights"
chown -R "$BOT_USER:$BOT_GROUP" /home/bot
chmod 750 /home/bot "$BOT_DIR"
chmod 700 "$BOT_DIR/logs" "$BOT_DIR/weights"

# --- Python virtual environment ---
echo "[6/7] Setting up Python environment..."
cd "$BOT_DIR"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
chown -R "$BOT_USER:$BOT_GROUP" "$BOT_DIR"
chmod 750 "$BOT_DIR"
chmod 700 "$BOT_DIR/logs" "$BOT_DIR/weights"
runuser -u "$BOT_USER" -- "$BOT_DIR/venv/bin/python" -m playwright install chromium

# --- Download Lc0 CPU binary ---
echo "[7/7] Downloading Lc0 CPU binary..."
LC0_VERSION="0.31.2"
LC0_URL="https://github.com/LeelaChessZero/lc0/releases/download/v${LC0_VERSION}/lc0-v${LC0_VERSION}-linux-cpu-openblas.tar.gz"

cd /tmp
wget -q "$LC0_URL" -O lc0.tar.gz || {
    echo "WARNING: Could not download Lc0 v${LC0_VERSION}."
    echo "Please download Lc0 manually from: https://github.com/LeelaChessZero/lc0/releases"
    echo "Extract and place the 'lc0' binary in /usr/local/bin/"
}

if [ -f lc0.tar.gz ]; then
    tar -xzf lc0.tar.gz
    # Find the lc0 binary in extracted directory
    LC0_BIN=$(find . -name "lc0" -type f -executable 2>/dev/null | head -1)
    if [ -n "$LC0_BIN" ]; then
        cp "$LC0_BIN" /usr/local/bin/lc0
        chmod +x /usr/local/bin/lc0
        echo "Lc0 installed to /usr/local/bin/lc0"
    else
        echo "WARNING: lc0 binary not found in archive. Install manually."
    fi
    rm -f lc0.tar.gz
fi

# --- Download Lc0 weights ---
echo ""
echo "============================================"
echo "  IMPORTANT: Download Lc0 Weights"
echo "============================================"
echo ""
echo "You need to download a weights file for Lc0."
echo "Options:"
echo ""
echo "  1. Maia (human-like play, recommended for anti-detection):"
echo "     wget -O $BOT_DIR/weights/maia-1900.pb.gz \\"
echo "       https://github.com/CSSLab/maia-chess/releases/download/v1.0/maia-1900.pb.gz"
echo ""
echo "  2. Standard Lc0 weights (stronger, ~40MB):"
echo "     Visit: https://training.lczero.org/networks/"
echo "     Download a 'T2' network and place in $BOT_DIR/weights/"
echo ""

# --- Setup systemd service ---
echo "Setting up systemd service..."
cat > /etc/systemd/system/chess-bot.service << 'EOF'
[Unit]
Description=Chess.com Lc0 Bot
After=network.target

[Service]
Type=simple
User=bot
Group=bot
UMask=0077
WorkingDirectory=/home/bot/chess.com_bot_host
ExecStart=/home/bot/chess.com_bot_host/venv/bin/python -m bot.main
Restart=always
RestartSec=30
NoNewPrivileges=true
PrivateTmp=true
StandardOutput=append:/home/bot/chess.com_bot_host/logs/bot.log
StandardError=append:/home/bot/chess.com_bot_host/logs/bot_error.log

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable chess-bot.service

for secret_file in "$BOT_DIR/config.yaml" "$BOT_DIR/session_cookies.json"; do
    if [ -f "$secret_file" ]; then
        chown "$BOT_USER:$BOT_GROUP" "$secret_file"
        chmod 600 "$secret_file"
    fi
done

echo ""
echo "============================================"
echo "  Setup Complete!"
echo "============================================"
echo ""
echo "Next steps:"
echo "  1. Copy your bot code to: $BOT_DIR/"
echo "  2. Copy config.yaml.example to config.yaml and edit it"
echo "  3. Download Lc0 weights (see above)"
echo "  4. Update config.yaml with weights path"
echo "  5. Start the bot:"
echo "     systemctl start chess-bot"
echo "     journalctl -u chess-bot -f  # view logs"
echo ""
