# Raspberry Pi Agent

A self-hosted autonomous AI agent framework designed for Raspberry Pi. Controlled via Telegram, supports concurrent named agents with persistent memory, isolated workspaces, and scheduled runs. Built with a swap-ready inference layer targeting full local LLM support on ARM hardware.

---

## What it does

- **Chat mode** — conversational interface with full awareness of all past agent activity
- **One-off agent tasks** — give Claude a goal, it runs autonomously using bash, browser automation, file I/O, and asks you when it needs input. Multiple tasks can run concurrently.
- **Named persistent agents** — reusable agents with isolated workspaces, persistent knowledge bases, and scheduled runs
- **Bidirectional Telegram communication** — send files, receive results, get notified when agents need your input mid-task

---

## Architecture

```
raspberry-pi-agent/
  agent/
    config.py        ← All configuration and constants
    state.py         ← Async SQLite state store (aiosqlite)
    tasks.py         ← Async task runners (OneOffTask, NamedAgentTask)
    prompts.py       ← All system prompts and prompt builders
    commands.py      ← All /command handlers
    scheduler.py     ← Cron scheduling helpers
    workspace.py     ← Named agent workspace and knowledge base management
    claude.py        ← Inference interface (swap-ready for SDK or Ollama)
  telegram/
    client.py        ← Async Telegram Bot API client (aiohttp)
  scripts/
    ask_human.py     ← Human-in-the-loop tool Claude calls mid-task (Unix socket client)
  tests/
    test_state.py
    test_scheduler.py
    test_workspace.py
    test_prompts.py
  agent.py           ← Entry point
  requirements.txt
```

### Key design decisions

**Async throughout** — the entire stack uses `asyncio`. The Telegram client uses `aiohttp`, the state store uses `aiosqlite`, and tasks run as `asyncio.Task` objects rather than threads. The polling loop stays responsive even during long agent runs.

**Swap-ready inference layer** — `agent/claude.py` is the only file that needs to change when switching inference backends. The public interface is a single `async get_response(prompt, config, agent_mode)` function. Stubs for the Anthropic SDK and Ollama remote inference are included and commented out.

**Two-layer agent memory** — named agents have both SQLite run history (queryable from chat) and a persistent `knowledge_base.json` on disk (read and written by the agent each run). The knowledge base uses a fixed JSON scaffold, with pre-run backup and post-run validation.

**Isolated workspaces** — each named agent has its own directory with a defined structure (`tools/`, `data/`, `outputs/`, `screenshots/`). One-off tasks write to a shared `tmp/` directory that is wiped after confirmed delivery.

**Concurrent one-off tasks** — multiple `/agent` tasks can run simultaneously. Each is tracked by task ID so any individual task can be cancelled with `/cancel <id>`.

**Unix socket for ask_human** — `scripts/ask_human.py` connects to the main bot process via a Unix socket rather than making independent Telegram API calls. This avoids race conditions where `ask_human` could intercept messages intended for the bot, and removes the `requests` sync dependency.

---

## Setup

### Prerequisites

- Raspberry Pi 4B (or any Linux server)
- Python 3.12+
- Claude CLI installed and authenticated (`claude --version`)
- A Telegram bot token and your chat ID

### Install

```bash
git clone https://github.com/youruser/raspberry-pi-agent.git
cd raspberry-pi-agent

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, and CLAUDE_CLI_COMMAND
```

### Run

```bash
python agent.py
```

### Run as a systemd service (persistent, starts on boot)

```ini
# /etc/systemd/system/pi-agent.service
[Unit]
Description=Raspberry Pi Agent
After=network-online.target
Wants=network-online.target
Requires=network-online.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/home/youruser/raspberry-pi-agent
EnvironmentFile=/home/youruser/raspberry-pi-agent/.env
Environment=CLAUDE_CLI_COMMAND=/home/youruser/.nvm/versions/node/v24.14.0/bin/claude
ExecStart=/home/youruser/raspberry-pi-agent/venv/bin/python3 /home/youruser/raspberry-pi-agent/agent.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

> **Security note:** Use `EnvironmentFile=` to load secrets from your `.env` file rather than inlining them as `Environment=` directives in the unit file. Unit files are readable by any user who can run `systemctl cat`, and are stored unencrypted in `/etc/systemd/system/`.

```bash
sudo systemctl daemon-reload
sudo systemctl enable pi-agent
sudo systemctl start pi-agent
```

### Run tests

```bash
pytest
```

---

## Commands

### General
| Command | Description |
|---|---|
| `/help` | List all commands |
| `/status` | Runtime info — active tasks, DB size, message count |
| `/reset` | Clear chat conversation history |

### One-off tasks
| Command | Description |
|---|---|
| `/agent <goal>` | Run an autonomous agent task (multiple can run concurrently) |
| `/task <id>` | Fetch the full result of a completed task |
| `/cancel` | Cancel all currently running tasks and agents |
| `/cancel <task_id>` | Cancel a specific running one-off task |
| `/cancel <agent_name>` | Stop the current run only (agent + schedule preserved) |

### Named persistent agents
| Command | Description |
|---|---|
| `/agent:create <n> - <description>` | Create a named agent |
| `/agent:run <n>` | Run a named agent |
| `/agent:list` | List all named agents |
| `/agent:history <n>` | Full run history for an agent |
| `/agent:logs <n> [count]` | Recent run activity (default 5) |
| `/agent:knowledge <n>` | View the agent's knowledge base |
| `/agent:files <n>` | Workspace file tree |
| `/agent:file <n> <filepath>` | Read a file from the agent's workspace |
| `/agent:schedule <n> daily 8am` | Schedule daily runs |
| `/agent:schedule <n> weekly monday 9am` | Schedule weekly runs |
| `/agent:unschedule <n>` | Remove schedule |
| `/agent:delete <n>` | Delete agent, history, and workspace |

### Memory management
| Command | Description |
|---|---|
| `/memory:clear task <id>` | Delete a specific task record |
| `/memory:clear tasks` | Delete all task records |
| `/memory:clear agent <n>` | Delete all run history for an agent |
| `/memory:clear run <run_id>` | Delete a specific run record |
| `/memory:clear all` | Delete all records and chat history |

### File transfer
Send any file to the bot to save it to `incoming/`. Add a caption `save to /custom/path` to specify a destination.

---

## Named agent workspace structure

Each named agent gets an isolated workspace:

```
agents/<name>/
  tools/              ← Reusable modules the agent builds over time
  data/
    raw/              ← Unprocessed input
    processed/        ← Cleaned/analyzed output
  outputs/            ← Reports and exports
  screenshots/        ← Browser screenshots
  knowledge_base.json ← Persistent memory — read and updated every run
  insights.md         ← Human-readable conclusions — append only
  README.md           ← Agent documents what it has built
```

### Knowledge base schema

The knowledge base uses a fixed JSON scaffold. The agent fills in values but never changes the top-level structure:

```json
{
  "agent_name": "",
  "purpose": "",
  "last_updated": "",
  "run_count": 0,
  "knowledge": {
    "key_findings": [],
    "patterns_observed": [],
    "successful_approaches": [],
    "failed_approaches": [],
    "open_questions": []
  },
  "state": {
    "last_action": "",
    "current_status": "",
    "next_steps": []
  },
  "resources": {
    "tools_built": [],
    "data_files": [],
    "external_services_used": []
  },
  "run_log": [],
  "run_log_archive": []
}
```

### Knowledge base compaction

As an agent accumulates runs, `run_log` grows by one entry per run. To prevent this from consuming excessive context window tokens, compaction runs automatically after every successful run once `run_log` exceeds 20 entries:

- The **10 most recent** entries are kept verbatim in `run_log`
- Older entries (typically 11 per compaction event) are summarised into a single prose paragraph by the LLM and appended to `run_log_archive` as one object per compaction call
- The archive schema: `{ "compacted_at": "...", "runs_covered": [...], "summary": "prose paragraph" }`
- If the LLM call fails, compaction falls back to a mechanical concatenation — no run is ever blocked
- No data is deleted — the archive accumulates over the agent's lifetime
- The agent is instructed to read both `run_log` and `run_log_archive` at the start of each run
- A Telegram notification is sent when compaction fires

---

## Swapping the inference backend

All inference is isolated in `agent/claude.py`. The public interface is unchanged regardless of backend:

```python
async def get_response(prompt: str, config: AgentConfig, agent_mode: bool) -> str
```

Set the `INFERENCE_BACKEND` env var to select a backend.

> **Note:** Several config variables and env vars are currently named after Claude (`CLAUDE_CLI_COMMAND`, `CLAUDE_CHAT_TIMEOUT`, `CLAUDE_AGENT_TIMEOUT`, `claude_command`, etc.). These will need to be renamed when introducing a non-Claude backend. That rework is intentionally deferred — the naming will be revisited alongside the tool loop and prompt changes described in the [Going fully local](#going-fully-local) section.

### Anthropic Python SDK

Uncomment and implement `_run_anthropic_sdk()` in `agent/claude.py`. Set `INFERENCE_BACKEND=anthropic_sdk` in your `.env`. Requires `pip install anthropic` and `ANTHROPIC_API_KEY`.

### Ollama (remote GPU inference)

Run Ollama on a machine with a GPU on the same network:

```bash
# On the GPU machine
ollama serve
ollama pull qwen2.5:7b
```

Uncomment `_run_ollama()` in `agent/claude.py`. Set in `.env`:

```
INFERENCE_BACKEND=ollama
OLLAMA_BASE_URL=http://10.0.0.x:11434
OLLAMA_MODEL=qwen2.5:7b
```

The Pi continues to run all orchestration, tool execution, and memory management. Only the inference step moves to the GPU machine.

---

## Going fully local

This section documents what is required to remove the dependency on Claude CLI entirely and run the agent with a local or self-hosted LLM.

### The core problem: where agentic capabilities come from

When running with Claude CLI (`--dangerously-skip-permissions`), the CLI itself is the agent runtime. It handles the full tool use loop: it interprets the model's tool calls, executes bash commands, reads and writes files, browses the web, and feeds results back to the model — repeating until the task is done. Your Python code hands it a prompt and receives final output. The tool loop is opaque.

When you swap to a local LLM (Ollama or similar), you get a single inference call — the model returns text and nothing runs. The system prompt instructs the model to call bash commands and use Puppeteer, but no one executes them.

**To go fully local, you must build the tool execution loop yourself in `agent/claude.py`.**

### Step 1: Choose and run a local LLM

On a machine with a GPU (can be the same Pi or a separate machine on your network):

```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Pull a capable model — Qwen2.5 and Mistral are good starting points
ollama pull qwen2.5:14b
ollama serve
```

Models that support function calling / tool use natively are required for a proper agent loop. Confirmed options (as of 2025):
- `qwen2.5:14b` — strong tool use, good instruction following
- `mistral-nemo` — fast, decent tool use
- `llama3.1:8b` — capable but weaker tool use than Qwen

Verify it's reachable from the Pi:
```bash
curl http://<gpu-machine-ip>:11434/api/tags
```

### Step 2: Implement the tool loop in agent/claude.py

The Ollama stub currently makes a single `POST /api/generate` call and returns. You need to replace this with a loop that:

1. Sends the prompt to the model
2. Parses the model's response for tool calls
3. Executes the tool locally (bash subprocess, file read/write, HTTP request)
4. Appends the tool result to the conversation
5. Calls the model again with the updated context
6. Repeats until the model signals it is done (no more tool calls)

Ollama supports OpenAI-compatible tool use via `/api/chat` with a `tools` parameter. The rough structure:

```python
async def _run_ollama(prompt: str, config: AgentConfig, agent_mode: bool) -> str:
    tools = _define_tools() if agent_mode else []
    messages = [{"role": "user", "content": prompt}]

    async with aiohttp.ClientSession() as session:
        while True:
            payload = {
                "model": config.ollama_model,
                "messages": messages,
                "tools": tools,
                "stream": False,
            }
            async with session.post(f"{config.ollama_base_url}/api/chat", json=payload) as resp:
                data = await resp.json()

            message = data["message"]
            messages.append(message)

            if not message.get("tool_calls"):
                return message["content"]  # done

            for call in message["tool_calls"]:
                result = await _execute_tool(call["function"]["name"], call["function"]["arguments"])
                messages.append({"role": "tool", "content": result})
```

### Step 3: Define local tools

This is the main development effort. Each tool the agent currently relies on via Claude CLI must be explicitly defined and implemented in Python.

The minimum viable tool set for the current agent use cases:

| Tool | Description | Implementation |
|---|---|---|
| `bash` | Run shell commands | `asyncio.create_subprocess_shell` |
| `read_file` | Read a file from disk | `Path.read_text()` |
| `write_file` | Write a file to disk | `Path.write_text()` |
| `list_directory` | List files in a path | `os.listdir()` or `Path.iterdir()` |
| `http_get` | Fetch a URL | `aiohttp.ClientSession.get()` |
| `ask_human` | Request input from operator | Existing Unix socket mechanism |

Browser/Puppeteer automation is harder — you have two options:
- **Keep Puppeteer**: define a `run_puppeteer_script` tool that writes a JS file and executes it via `node`
- **Switch to Playwright Python**: gives you a native Python API, easier to call from the tool loop

Tools should be defined in a new file `agent/tools.py` as a list of JSON schemas (matching the OpenAI tool format that Ollama accepts) alongside the corresponding Python execution functions.

### Step 4: Update the system prompts

The current system prompts in `agent/prompts.py` are written for Claude CLI — they describe tools in natural language and reference Claude-specific behaviours. When running a local LLM with an explicit tool schema, the prompts should:

- Remove instructions about tool names and syntax (the model sees the schema directly)
- Focus on goals, workspace structure, knowledge base format, and output expectations
- Be shorter — local models have smaller effective context windows

### What you are not replacing

The following components are model-agnostic and require no changes:

- Telegram bot (`telegram/client.py`)
- State store (`agent/state.py`)
- Workspace and knowledge base (`agent/workspace.py`)
- Scheduling (`agent/scheduler.py`)
- Task runners (`agent/tasks.py`)
- All `/commands` (`agent/commands.py`)
- `ask_human` Unix socket mechanism

The entire orchestration layer stays intact. Only `agent/claude.py` and `agent/prompts.py` need work.

---

## Security

- Only the configured `TELEGRAM_CHAT_ID` can interact with the bot
- UFW firewall should be enabled with SSH restricted to local network or Tailscale
- Agent runs with `--dangerously-skip-permissions` — it has full bash access
- Credentials stored in `~/.credentials/<sitename>` with `chmod 600`
- Never commit `.env` or `~/.credentials/` to version control

---

## Running tests

```bash
# All tests
pytest

# With coverage
pytest --cov=agent --cov=telegram --cov-report=term-missing

# Specific module
pytest tests/test_state.py -v
```

---

## Roadmap

- [ ] Implement tool loop in `agent/claude.py` for local LLM support
- [ ] Add `agent/tools.py` with explicit tool definitions (bash, file I/O, HTTP, browser)
- [ ] Migrate system prompts in `agent/prompts.py` for local LLM tool schema format
- [ ] Ollama remote inference integration (complete `_run_ollama()`)
- [ ] Anthropic SDK backend (complete `_run_anthropic_sdk()`)
- [ ] Health check HTTP endpoint
- [ ] Structured JSON logging
- [ ] Graceful SIGTERM handling
