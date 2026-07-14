# Chess.com Lc0 Bot (Host Edition)

A high-performance, automated Chess.com companion utilizing the Leela Chess Zero (Lc0) engine. This implementation uses a process-isolated architecture specifically optimized for low-specification VPS environments (e.g., 1 Core CPU, 1GB RAM) to sustain continuous play without memory leaks.

---

## Process Isolation Flow

Here is the architectural workflow of the host and game-specific subprocess communication:

<div align="center">
  <img src="assets/architecture.svg" alt="Process Isolation Architecture" width="100%" />
</div>

---

## Core Capabilities

| Feature | Description | Implementation Details |
| :--- | :--- | :--- |
| **Engine Lifecycle** | On-demand allocation | Lc0 is initialized at game start and terminated instantly upon game completion. |
| **Memory Isolation** | CDP subprocess structure | Game operations run inside a subprocess through a localhost Chrome DevTools endpoint. Prevents engine memory fragmentation. |
| **Humanized Delays** | Clock-aware timing models | Delay timings dynamically scale with time control type, remaining time ratios, and complexity. |
| **Position Reading** | Multi-layered parsing | Reconstructs boards via move-list SAN replay. Falls back to DOM attributes and JS state variables. |
| **Session Control** | Cookie state persistence | Persists active browser authentication contexts to bypass repetitive credential verification. |
| **System Alerts** | Discord and Telegram push | Publishes game results, starts, daily limits, and warning logs via non-blocking async HTTP POST. |

---

## Resource Usage Profiles

| Runtime State | Memory Usage | CPU Load | Target Runtime |
| :--- | :--- | :--- | :--- |
| **Idle / Polling** | ~350 MB | < 1% | Infinite |
| **Active / Maia Engine** | ~550 MB | < 5% (policy-only nodes=1) | During game |
| **Active / Lc0 Search** | ~800 MB | Configurable threads | During game |

---

## Configuration Guide

The bot is configured using `config.yaml`. Below is a comprehensive overview of available options:

### Complete Configuration Reference

```yaml
account:
  username: "your_chess_username"
  password: "${CHESS_BOT_PASSWORD}"
  login_mode: "auto"           # auto | cookie_only | credentials

challenge:
  mode: "whitelist"            # whitelist | open
  allowed_users:
    - "friend1"
    - "friend2"

engine:
  type: "auto"                 # auto | maia | lc0
  path: "/usr/local/bin/lc0"
  weights: "/home/bot/weights/maia-1900.pb.gz"
  backend: "blas"
  threads: 1
  nn_cache_size: 200000
  time_per_move: 1.5           # Only used if type is "lc0"

humanizer:
  enabled: true
  delay_min: 0.3               # Minimum base delay (seconds)
  delay_max: 1.5               # Maximum base delay (seconds)
  blunder_chance: 0.08         # 8% baseline blunder rate
  premove_chance: 0.05         # 5% baseline premove rate
  rating_mimic: 1800

notifications:
  webhook_url: ""              # Telegram or Discord webhook endpoint URL

server:
  check_interval: 12           # Seconds between challenge checks
  max_games_per_day: 5         # Daily limit to avoid suspicion
  cookie_file: "session_cookies.json"
  headless: true               # Set to false to watch browser actions
  log_level: "INFO"            # DEBUG | INFO | WARNING | ERROR
  max_context_games: 3         # Recreate browser context every N games
  worker_timeout_seconds: 7200 # Kill a stuck game worker after this many seconds
  browser_no_sandbox: false    # Keep false unless Chromium sandbox is unavailable
```

### Key Parameter Details

| Section | Parameter | Default | Description |
| :--- | :--- | :--- | :--- |
| **account** | `login_mode` | `auto` | `cookie_only` bypasses credential entry. `credentials` skips cookie checks. `auto` tries cookies first, then credentials. |
| **engine** | `type` | `auto` | `maia` forces policy-only `nodes=1`; `lc0` forces time-based search; `auto` checks the weights filename for "maia". |
| **humanizer** | `rating_mimic` | `1800` | Adjusts blunder selection criteria and search depth constraints to match target play strength. |
| **server** | `max_context_games`| `3` | Periodically refreshes the browser window to flush cached assets and prevent Chromium memory growth. |
| **server** | `worker_timeout_seconds` | `7200` | Prevents a stuck worker subprocess from blocking the main listener forever. |
| **server** | `browser_no_sandbox` | `false` | Enables Chromium `--no-sandbox` only when explicitly required. Do not use it while running as root. |

---

## Installation and Setup

### 1. System Dependencies
Install Python 3.10+, Chromium web browser, and Lc0.

```bash
# Clone the repository
git clone https://github.com/MuajAmin/chess.com-bot-host.git
cd chess.com-bot-host

# Install requirements
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure Environment

Copy the default configuration template:

```bash
cp config.yaml.example config.yaml
```

Modify the parameters in `config.yaml` to match your account and engine installation pathways.
For secrets, prefer environment variables such as `${CHESS_BOT_PASSWORD}` and keep `config.yaml` and `session_cookies.json` readable only by the bot user.

### 3. Run

Initialize the orchestrator framework:

```bash
python -m bot.main
```

---

## Operational Tips

### VPS Deployment (Running in the Background)
To run the bot persistently on a Linux server, it is recommended to use `systemd` or `screen`:

Run the service as a dedicated non-root user. The included `setup_vps.sh` creates a `bot` service user, applies a restrictive umask, and avoids Chromium `--no-sandbox` by default.

```bash
# Running in screen
screen -S chess-bot
python -m bot.main
# Press Ctrl + A, then D to detach
```

### Webhook Alerts Format
- **Discord:** Simply paste your Discord Channel Webhook URL into `webhook_url`.
- **Telegram:** Use the format `https://api.telegram.org/bot<token>/sendMessage?chat_id=<id>` containing your bot token and target chat ID.

---

> [!WARNING]
> This project is designed strictly for research, testing, and educational purposes. Automating account activity on Chess.com is a violation of their Terms of Service and will lead to permanent account suspension. Use at your own discretion.
