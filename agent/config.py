"""
agent/config.py

All configuration, constants, and environment variable loading.
Swap inference backends here by changing CLAUDE_CLI_COMMAND or adding
an INFERENCE_BACKEND env var pointing to an Ollama endpoint.
"""

import os
from pathlib import Path


# ---------------------------------------------------------------------------
# Directory layout — derived from project root
# ---------------------------------------------------------------------------

PROJECT_DIR  = Path(__file__).parent.parent
TMP_DIR      = PROJECT_DIR / "tmp"
AGENTS_DIR   = PROJECT_DIR / "agents"
INCOMING_DIR = PROJECT_DIR / "incoming"
DATA_DIR     = PROJECT_DIR / "data"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_CLAUDE_COMMAND        = "claude"
DEFAULT_DB_PATH               = str(DATA_DIR / "agent_state.db")
DEFAULT_POLL_TIMEOUT          = 25
DEFAULT_REQUEST_TIMEOUT       = 30
DEFAULT_CLAUDE_CHAT_TIMEOUT   = 120
DEFAULT_CLAUDE_AGENT_TIMEOUT  = 1800   # 30 minutes
DEFAULT_MAX_HISTORY_MESSAGES  = 24
DEFAULT_CHUNK_SIZE             = 3500
CONFIRMATION_TIMEOUT_SECONDS  = 60
ONE_OFF_RESULT_PREVIEW_CHARS  = 300

MAX_TELEGRAM_FILE_BYTES       = 50 * 1024 * 1024   # 50 MB send limit
MAX_TELEGRAM_RECEIVE_BYTES    = 20 * 1024 * 1024   # 20 MB receive limit


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class ConfigError(ValueError):
    pass


class AgentConfig:
    """
    Loads all configuration from environment variables.

    Required:
        TELEGRAM_BOT_TOKEN
        TELEGRAM_CHAT_ID

    Optional (defaults shown):
        CLAUDE_CLI_COMMAND      = claude
        AGENT_DB_PATH           = data/agent_state.db
        CLAUDE_CHAT_TIMEOUT     = 120
        CLAUDE_AGENT_TIMEOUT    = 1800
        CLAUDE_MAX_HISTORY_MESSAGES = 24
        TELEGRAM_CHUNK_SIZE     = 3500
        TELEGRAM_REQUEST_TIMEOUT = 30
        TELEGRAM_POLL_TIMEOUT   = 25
        LOG_LEVEL               = INFO

    Future inference swap:
        INFERENCE_BACKEND       = ollama
        OLLAMA_BASE_URL         = http://10.0.0.x:11434
        OLLAMA_MODEL            = qwen2.5:7b
    """

    def __init__(self) -> None:
        self.telegram_bot_token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.telegram_chat_id_raw = os.getenv("TELEGRAM_CHAT_ID", "")
        self.claude_command       = os.getenv("CLAUDE_CLI_COMMAND", DEFAULT_CLAUDE_COMMAND)
        self.db_path              = os.getenv("AGENT_DB_PATH", DEFAULT_DB_PATH)
        self.request_timeout      = int(os.getenv("TELEGRAM_REQUEST_TIMEOUT", str(DEFAULT_REQUEST_TIMEOUT)))
        self.poll_timeout         = int(os.getenv("TELEGRAM_POLL_TIMEOUT", str(DEFAULT_POLL_TIMEOUT)))
        self.claude_chat_timeout  = int(os.getenv("CLAUDE_CHAT_TIMEOUT", str(DEFAULT_CLAUDE_CHAT_TIMEOUT)))
        self.claude_agent_timeout = int(os.getenv("CLAUDE_AGENT_TIMEOUT", str(DEFAULT_CLAUDE_AGENT_TIMEOUT)))
        self.max_history          = int(os.getenv("CLAUDE_MAX_HISTORY_MESSAGES", str(DEFAULT_MAX_HISTORY_MESSAGES)))
        self.chunk_size           = int(os.getenv("TELEGRAM_CHUNK_SIZE", str(DEFAULT_CHUNK_SIZE)))
        self.ask_human_path       = str(PROJECT_DIR / "scripts" / "ask_human.py")
        self.ask_human_sock_path  = str(TMP_DIR / "ask_human.sock")

        # Future: inference backend selection
        self.inference_backend    = os.getenv("INFERENCE_BACKEND", "claude_cli")
        self.ollama_base_url      = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        self.ollama_model         = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")

        missing = []
        if not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.telegram_chat_id_raw:
            missing.append("TELEGRAM_CHAT_ID")
        if missing:
            raise ConfigError(f"Missing required env vars: {', '.join(missing)}")

        try:
            self.telegram_chat_id = int(self.telegram_chat_id_raw)
        except ValueError as exc:
            raise ConfigError("TELEGRAM_CHAT_ID must be an integer") from exc
