# Chess.com Lc0 Bot (Host Edition)

A high-performance, automated Chess.com companion utilizing the Leela Chess Zero (Lc0) engine. This implementation uses a process-isolated architecture specifically optimized for low-specification VPS environments (e.g., 1 Core CPU, 1GB RAM) to sustain continuous play without memory leaks.

---

## Process Isolation Flow

Here is the architectural workflow of the host and game-specific subprocess communication:

<div align="center">
  <svg viewBox="0 0 800 280" width="100%" height="auto" xmlns="http://www.w3.org/2000/svg">
    <style>
      .card {
        fill: #161b22;
        stroke: #30363d;
        stroke-width: 2;
        rx: 10px;
      }
      .card-accent {
        fill: #0d1117;
        stroke: #58a6ff;
        stroke-width: 2;
        rx: 10px;
        filter: drop-shadow(0 0 4px rgba(88, 166, 255, 0.25));
      }
      .title-text {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
        font-size: 14px;
        font-weight: 600;
        fill: #c9d1d9;
      }
      .desc-text {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
        font-size: 11px;
        fill: #8b949e;
      }
      .flow-line {
        stroke: #30363d;
        stroke-width: 2;
        fill: none;
      }
      .active-line {
        stroke: #58a6ff;
        stroke-width: 2;
        stroke-dasharray: 6, 4;
        animation: flow 1.5s linear infinite;
        fill: none;
      }
      .engine-line {
        stroke: #ff7b72;
        stroke-width: 2;
        stroke-dasharray: 6, 4;
        animation: flow 1.5s linear infinite;
        fill: none;
      }
      .glow-node {
        fill: #58a6ff;
        animation: pulse 2s ease-in-out infinite;
      }
      .warning-node {
        fill: #ff7b72;
        animation: pulse-warn 2s ease-in-out infinite;
      }
      @keyframes flow {
        to {
          stroke-dashoffset: -20;
        }
      }
      @keyframes pulse {
        0%, 100% {
          r: 4px;
          filter: drop-shadow(0 0 2px rgba(88, 166, 255, 0.6));
        }
        50% {
          r: 6px;
          filter: drop-shadow(0 0 8px rgba(88, 166, 255, 1));
        }
      }
      @keyframes pulse-warn {
        0%, 100% {
          r: 4px;
          filter: drop-shadow(0 0 2px rgba(255, 123, 114, 0.6));
        }
        50% {
          r: 6px;
          filter: drop-shadow(0 0 8px rgba(255, 123, 114, 1));
        }
      }
    </style>

    <!-- Host Process Card -->
    <g transform="translate(40, 40)">
      <rect width="220" height="180" class="card" />
      <text x="15" y="30" class="title-text">Host Process (main.py)</text>
      <text x="15" y="60" class="desc-text">• Manages Browser Context</text>
      <text x="15" y="80" class="desc-text">• Session Persistence (Cookies)</text>
      <text x="15" y="100" class="desc-text">• Polls Chess.com Challenges</text>
      <text x="15" y="120" class="desc-text">• Webhook Status Notifier</text>
      <text x="15" y="150" class="desc-text" fill="#58a6ff" style="font-weight:bold;">Status: Listening...</text>
      <circle cx="200" cy="148" class="glow-node" />
    </g>

    <!-- Subprocess Card -->
    <g transform="translate(540, 40)">
      <rect width="220" height="180" class="card-accent" />
      <text x="15" y="30" class="title-text" fill="#58a6ff">Game Worker (game_worker.py)</text>
      <text x="15" y="60" class="desc-text">• Launches Lc0 UCI Engine</text>
      <text x="15" y="80" class="desc-text">• Operates Game Loop</text>
      <text x="15" y="100" class="desc-text">• Clock-Aware Humanizer</text>
      <text x="15" y="120" class="desc-text">• Mouse Clicks & Movements</text>
      <text x="15" y="150" class="desc-text" fill="#ff7b72" style="font-weight:bold;">Isolated Environment</text>
      <circle cx="200" cy="148" class="warning-node" />
    </g>

    <!-- Middle Connector: Spawn Process -->
    <path d="M 260,80 L 540,80" class="flow-line" />
    <polygon points="535,76 545,80 535,84" fill="#30363d" />
    <text x="350" y="70" class="desc-text" style="font-weight:bold;">1. Spawns Subprocess</text>

    <!-- Middle Connector: CDP Channel -->
    <path d="M 540,130 L 260,130" class="active-line" />
    <polygon points="265,126 255,130 265,134" fill="#58a6ff" />
    <text x="325" y="120" class="desc-text" fill="#58a6ff" style="font-weight:bold;">2. CDP WebSocket Protocol</text>

    <!-- Bottom Connector: Terminate & Reclaim -->
    <path d="M 540,180 L 260,180" class="engine-line" />
    <polygon points="265,176 255,180 265,184" fill="#ff7b72" />
    <text x="310" y="170" class="desc-text" fill="#ff7b72" style="font-weight:bold;">3. OS Frees Process & RAM Heap</text>
  </svg>
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