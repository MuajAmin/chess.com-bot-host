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
| **Memory Isolation** | CDP subprocess structure | Game operations run inside a subprocess. Prevents Chromium and engine memory fragmentation. |
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

## System Requirements

- Python 3.10 or higher
- Linux/Ubuntu VPS or Windows Host environment
- Chromium Web Browser (via Playwright)
- Lc0 Engine Binary with a compatible Neural Network weights file

---

## Quick Start

### 1. Installation

Clone the repository and install all required libraries:

```bash
git clone https://github.com/MuajAmin/chess.com-bot-host.git
cd chess.com-bot-host
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure Environment

Copy the default configuration template:

```bash
cp config.yaml.example config.yaml
```

Modify the parameters in `config.yaml` to match your account and engine installation pathways.

```yaml
account:
  username: "your_chess_username"
  password: "your_password"
  login_mode: "auto"           # auto, cookie_only, or credentials

engine:
  type: "auto"                 # auto, maia, or lc0
  path: "/usr/local/bin/lc0"
  weights: "/home/bot/weights/maia-1900.pb.gz"

notifications:
  webhook_url: ""              # Optional Discord or Telegram webhook endpoint
```

### 3. Run

Initialize the orchestrator framework:

```bash
python -m bot.main
```

---

> [!WARNING]
> This project is designed strictly for research, testing, and educational purposes. Automating account activity on Chess.com is a violation of their Terms of Service and will lead to permanent account suspension. Use at your own discretion.