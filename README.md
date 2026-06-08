# SuperAgent — AI Digital Workforce

One command. 250 AI agents. Each with their own computer.

SuperAgent is a production-grade AI workforce infrastructure where a single Hermes orchestrator manages up to 250 parallel worker agents, each running in an isolated desktop environment. Give Hermes one goal — it breaks it down, assigns work to all agents simultaneously, and synthesizes everything back into one final answer.

---

## What This Is

You type one command. Hermes (Nous Research Hermes-3-70B) reads it, decides how to split the work, spawns up to 250 Docker containers each with a full KasmVNC desktop, assigns one subtask per agent, and every agent works in parallel like a human employee at a computer. When they are all done, Hermes reads every result and writes you one synthesized final answer.

Every agent can do everything a human can do on a computer — browse the web, log into sites, handle 2FA, solve captchas, download files, fill forms, run terminal commands, use desktop apps, copy and paste, manage files, and more.

---

## Architecture
You
└── One command
└── Hermes Orchestrator (NousResearch/Hermes-3-Llama-3.1-70B)
├── Decomposes goal into N parallel subtasks
├── Spawns N isolated Docker containers
├── Assigns one subtask per agent
├── Monitors all agents, reassigns failures
│
├── Agent 001 — KasmVNC Desktop
│    ├── Playwright browser
│    ├── Login + 2FA (TOTP, email, SMS, OAuth)
│    ├── File operations
│    ├── OCR + vision
│    └── Any human desktop action
│
├── Agent 002 through Agent 250 (identical)
│
└── Hermes aggregates all results
└── One final answer back to you

---

## Features

### Hermes Orchestrator
- Powered by NousResearch/Hermes-3-Llama-3.1-70B via Together AI or local Ollama
- Decomposes any goal into parallel subtasks automatically
- Assigns tasks to agents based on availability
- Monitors all agents every 5 seconds
- Auto-reassigns tasks from failed or stuck agents
- Hierarchical result aggregation handles up to 250 agent outputs
- Graceful shutdown with task state preservation

### Agent Desktop Infrastructure
- Each agent gets a fully isolated KasmVNC desktop container
- Desktop API server on port 8000 per agent
- KasmVNC stream on port 6901 per agent
- FFmpeg HLS 4K stream fallback on port 7080 per agent
- Containers spawn and die dynamically via Docker Python SDK
- Persistent named volumes per agent survive restarts
- Hard limit of 250 containers enforced

### Desktop Control
Every agent can do all of these via REST API:
- Screenshot (PNG)
- Mouse click, double click, drag, scroll
- Keyboard typing and key combinations
- Clipboard copy, paste, read
- Run shell commands, read stdout and stderr
- Launch and close desktop applications
- Focus windows by title
- List all open windows and running processes
- Send desktop notifications
- Upload and download files

### Browser Automation
- Headed Chromium browser visible on KasmVNC desktop
- Navigate to any URL
- Click elements by natural language description using vision model
- Fill forms by field label
- Scroll pages
- Wait for elements to appear
- Extract text from any region via OCR
- Download files to agent local storage
- Get all visible page text

### Login and Authentication
- Automatic login to any site — finds form, fills credentials, submits
- TOTP 2FA — generates code via pyotp, types automatically
- Email OTP — connects via IMAP, waits for code, extracts and types it
- SMS OTP — polls webhook every 3 seconds up to 60 seconds
- Captcha handling — 2captcha API with human escalation fallback
- Google and GitHub OAuth popup flows
- Session saving and restoring via cookie persistence
- Automatic session validity check before each task

### Task Database
- SQLite with full task lifecycle tracking
- Tables for tasks, agents, results, and sessions
- Task states: pending, running, done, failed
- Auto-retry up to 3 times on failure
- Priority queue — urgent tasks execute first
- Full workforce status dashboard query
- Agent heartbeat tracking with 30 second timeout detection

### Shared Storage
- Shared volume mounted in every container at /shared
- Agents write results, read each other's outputs
- File sharing between agents
- Agent inbox system for direct agent-to-agent messaging
- Hermes reads all results from shared storage for aggregation

### Provider Support
- Anthropic Claude (claude-opus-4-5, vision capable)
- OpenAI GPT-4o
- Groq
- Mistral
- Gemini
- DeepSeek
- OpenRouter
- Fireworks
- Moonshot
- HuggingFace
- Qwen
- Ollama (local, llava for vision)
- OS-Atlas (visual grounding)
- All swappable at runtime via config

### Cost Tracking
- Token usage tracked per agent per task
- Cost estimated against price table of 22 models
- Per-agent and total workforce cost breakdown
- Daily cost summaries in logs

### Memory and Sessions
- SQLite memory with FTS5 full text search
- Store and recall information across task restarts
- Session persistence saves full agent state to disk
- Agent resumes exactly where it left off after container restart

### Monitoring and Logging
- Watchdog heartbeat per agent
- Auto-restart on crash with session reload
- All errors logged to logs/errors.log with full traceback
- All activity logged to logs/activity.log
- Daily log rotation, 7 days retention
- Escalation webhook for human intervention on blockers

---

## Quick Start

### Requirements
- Docker and Docker Compose
- Python 3.11+
- Together AI API key (for Hermes) or local Ollama
- Anthropic or OpenAI API key (for worker vision)

### Setup

```bash
git clone https://github.com/yourusername/superagent
cd superagent
cp .env.example .env
# Edit .env and add your API keys
docker compose up
```

### Run your first workforce command

```python
import asyncio
from hermes.orchestrator import HermesOrchestrator

async def main():
    hermes = HermesOrchestrator()
    result = await hermes.run(
        "Research the top 10 AI startups founded in 2024, find their website, founding team, and funding amount"
    )
    print(result.summary)

asyncio.run(main())
```

### Or via HTTP API

```bash
curl -X POST http://localhost:9000/run \
  -H "Content-Type: application/json" \
  -d '{"command": "Research the top 10 AI startups founded in 2024"}'
```

---

## Environment Variables

```bash
# LLM Providers
TOGETHER_API_KEY=          # For Hermes via Together AI
ANTHROPIC_API_KEY=         # For worker vision model
OPENAI_API_KEY=            # Optional OpenAI workers
OLLAMA_BASE_URL=http://localhost:11434  # For local Hermes

# Models
HERMES_MODEL=NousResearch/Hermes-3-Llama-3.1-70B
WORKER_VISION_MODEL=claude-opus-4-5

# Authentication helpers
TWOCAPTCHA_API_KEY=        # For captcha solving
SMTP_HOST=                 # For email OTP checking
SMTP_USER=
SMTP_PASS=
ESCALATION_WEBHOOK=        # Webhook URL for human escalation

# Infrastructure
MAX_AGENTS=250
AGENT_BASE_DESKTOP_PORT=8000
AGENT_BASE_VNC_PORT=6901
AGENT_BASE_STREAM_PORT=7080
SHARED_PATH=./shared
DB_PATH=./data/superagent.db
LOG_PATH=./logs
```

---

## Project Structure
SuperAgent/
│
├── hermes/                         # Hermes orchestrator — the brain
│   ├── orchestrator.py             # Decomposes goals, assigns tasks, aggregates results
│   └── __main__.py                 # HTTP API server on port 9000
│
├── infrastructure/                 # Shared backbone for all agents
│   ├── container_manager.py        # Spawns and kills Docker containers via Docker SDK
│   ├── task_db.py                  # SQLite — tracks every task, agent, and result
│   ├── shared_storage.py           # Shared volume — agents read and write results here
│   └── logging.py                  # Centralized logging for the whole system
│
├── worker/                         # What each agent can do
│   ├── browser.py                  # Playwright — browse, click, fill forms, download
│   └── auth.py                     # Login, TOTP, email OTP, SMS OTP, OAuth, sessions
│
├── superagent/                     # Core agent runtime
│   ├── agent.py                    # SuperAgent class — wires everything together
│   ├── config.py                   # AgentConfig — all settings in one dataclass
│   ├── loop.py                     # Main agent loop — screenshot, act, check, repeat
│   ├── actions.py                  # Action types, parser, and executor
│   ├── providers.py                # All LLM providers — Anthropic, OpenAI, Ollama, etc
│   ├── desktop_api.py              # HTTP client — talks to desktop_server inside container
│   ├── stream.py                   # KasmVNC detector and FFmpeg HLS fallback
│   ├── memory.py                   # SQLite memory with FTS5 full text search
│   ├── cost_tracker.py             # Tracks tokens used and cost per agent per task
│   ├── queue.py                    # Priority task queue
│   ├── session.py                  # Saves and loads agent session state to disk
│   ├── monitor.py                  # Watchdog — detects stuck or crashed agents
│   ├── escalation.py               # Sends webhook when agent needs human help
│   ├── ocr.py                      # Reads text from screenshots via Tesseract or EasyOCR
│   ├── grounding.py                # Finds coordinates of UI elements on screen
│   ├── scheduler.py                # Schedules recurring or delayed tasks
│   └── verification.py             # Handles human verification steps in agent tasks
│
├── container/                      # Runs inside every agent Docker container
│   ├── desktop_server.py           # Flask REST API — screenshot, click, type, browser, files
│   └── start.sh                    # Startup script — launches KasmVNC, Flask API, HLS stream
│
├── tests/
│   ├── test_production.py          # Full 250-agent workforce integration tests
│   └── test_live.py                # Single agent live container tests
│
├── Dockerfile                      # Builds the agent container image
├── Dockerfile.hermes               # Builds the Hermes orchestrator image
├── docker-compose.yml              # Runs the full stack — Hermes, nginx, shared storage
├── nginx.conf                      # Routes /agent/{id}/vnc desktop and stream per agent
├── requirements.txt                # All Python dependencies
└── .env.example                    # Every environment variable with descriptions
---

## How It Works

**Step 1 — You give one command**
"Find the LinkedIn profile, company size, and latest funding for 250 YC companies"

**Step 2 — Hermes decomposes**

Hermes reads the goal and creates 250 subtasks, one per company. Each subtask is self-contained with clear instructions and expected output format.

**Step 3 — Containers spawn**

ContainerManager spawns 250 Docker containers in parallel. Each gets its own KasmVNC desktop, Flask API server, and HLS stream. Each container waits until healthy before accepting tasks.

**Step 4 — Agents execute in parallel**

Every agent receives its subtask. Each one opens a browser, navigates to LinkedIn, logs in using saved session or fresh credentials, searches for the company, extracts the data, and writes the result to shared storage.

**Step 5 — Hermes aggregates**

When all agents complete, Hermes reads every result from shared storage and synthesizes one comprehensive final answer — a structured dataset of all 250 companies with the requested information.

**Step 6 — Result returned to you**

One clean final answer, structured data, and any files the agents downloaded, all available immediately.

---

## Agent Desktop Stream

Every agent desktop is viewable live in your browser:

- KasmVNC: `http://localhost:{6901 + agent_id}`
- HLS Stream: `http://localhost:{7080 + agent_id}/index.m3u8`
- Desktop API: `http://localhost:{8000 + agent_id}`

Via nginx proxy:
- `http://localhost/agent/{id}/vnc/`
- `http://localhost/agent/{id}/stream/`
- `http://localhost/agent/{id}/desktop/`

---

## Reference Projects

Built with inspiration from:
- [trycua/cua](https://github.com/trycua/cua) — Computer use agent framework
- [e2b-dev/open-computer-use](https://github.com/e2b-dev/open-computer-use) — Open computer use
- [agiresearch/AIOS](https://github.com/agiresearch/AIOS) — AI operating system
- [kasmtech/KasmVNC](https://github.com/kasmtech/KasmVNC) — Browser-based VNC

---

## License

MIT
