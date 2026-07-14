# Chess.com Lc0 Bot (Host Edition)

An automated Chess.com companion bot utilizing Leela Chess Zero (Lc0). Designed with an on-demand, process-isolated architecture to run stably on low-resource VPS setups (e.g., 1 Core CPU, 1GB RAM) without memory leaks.

---

## Key Features

- **On-Demand UCI Engine Lifecycle** - Starts Lc0 only when a game begins and terminates the engine process immediately upon game end, keeping idle RAM usage minimal.
- **CDP Process Isolation** - Spawns a dedicated game worker subprocess connecting to the host browser context via Chrome DevTools Protocol (CDP). All Lc0 and Python chess engine allocations are fully reclaimed by the operating system after each game.
- **Dynamic Humanizer** - Implements Gaussian-delay models calibrated using time control constraints (Bullet, Blitz, Rapid, Classical) and remaining clock ratios. Simulates flagging urgency, blunder rates under pressure, and selective premoving.
- **Multi-layered Board Parsing** - Primary board state reconstruction is powered by move-list SAN replay, ensuring perfect tracking of castling rights, en-passant states, and clock counters. Falls back to internal JavaScript state and DOM attributes. Avoids fragile React fiber walking.
- **Stealth and Persistence** - Uses customized Playwright headers, browser footprint masking, and cookie/localStorage synchronization to persist active sessions and minimize login verification challenges. Supports credential-less runtime initialization.
- **Asynchronous Webhook Notifications** - Features non-blocking integration with Discord and Telegram webhooks using `httpx` to publish logs, status updates, and game summaries without blocking active threads.

---

## Directory Structure

```
├── bot/
│   ├── main.py              # Orchestrator (Browser persistence, challenge monitoring, worker spawning)
│   ├── game_worker.py       # Isolated worker (Engine uci control, game loops, click executions)
│   ├── board_parser.py      # DOM state translation & move list history compilation
│   ├── session_manager.py   # Stealth chromium instances & credential storage state manager
│   ├── challenge_listener.py# Challenge state checkers & accept workflows
│   ├── humanizer.py         # Delay calculations, blunder injections, and speed parameters
│   ├── move_maker.py        # Natural Bézier mouse path movements and click timers
│   ├── lc0_engine.py        # UCI engine communication handler (optimized for Maia nodes=1)
│   ├── notifier.py          # Asynchronous Telegram and Discord message publishers
│   └── config.py            # Configuration loading, merging, and validations
```

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

### 2. Configuration
Copy the configuration template and populate it with your environment values:

```bash
cp config.yaml.example config.yaml
nano config.yaml
```

**Example minimal `config.yaml`:**
```yaml
account:
  username: "your_chess_username"
  password: "your_password"
  login_mode: "auto"           # Options: auto, cookie_only, credentials

engine:
  type: "auto"                 # Options: auto, maia, lc0
  path: "/usr/local/bin/lc0"
  weights: "/home/bot/weights/maia-1900.pb.gz"

notifications:
  webhook_url: "https://discord.com/api/webhooks/..." # Leave blank to disable
```

### 3. Execution
Start the orchestrator process:

```bash
python -m bot.main
```

---

## Disclaimer

This project is intended strictly for educational, research, and non-commercial purposes. Automating gameplay on Chess.com violates their Terms of Service and will result in permanent account termination. Use at your own discretion.