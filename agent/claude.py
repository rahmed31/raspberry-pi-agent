"""
agent/claude.py

Inference interface — the ONLY file that needs to change when swapping
inference backends (Claude CLI → Anthropic SDK → Ollama).

Current backend: Claude CLI via asyncio subprocess.

To swap to Anthropic SDK:
    Replace _run_claude_cli() with an aiohttp/anthropic client call.
    Keep the public interface: async get_response(prompt, agent_mode) -> str

To swap to Ollama (remote GPU inference):
    Replace _run_claude_cli() with an aiohttp POST to OLLAMA_BASE_URL.
    Keep the same public interface.
"""

import asyncio
import logging
import shlex
import os
from typing import Optional

from agent.config import AgentConfig

LOGGER = logging.getLogger(__name__)


class InferenceError(RuntimeError):
    """Raised when the inference backend fails."""
    pass


async def get_response(
    prompt: str,
    config: AgentConfig,
    agent_mode: bool = False,
) -> str:
    """
    Get a response from the configured inference backend.

    Public interface — stable across backend swaps.

    Args:
        prompt:     The full prompt string (system + history + goal).
        config:     AgentConfig — provides command, timeouts, backend selection.
        agent_mode: If True, enables tool use / dangerous permissions.

    Returns:
        The model's response as a string.

    Raises:
        InferenceError: On any backend failure.
    """
    backend = config.inference_backend.lower()

    if backend == "claude_cli":
        return await _run_claude_cli(prompt, config, agent_mode)

    # ---------------------------------------------------------------------------
    # Future backends — uncomment and implement when ready
    # ---------------------------------------------------------------------------

    # elif backend == "anthropic_sdk":
    #     return await _run_anthropic_sdk(prompt, config, agent_mode)

    # elif backend == "ollama":
    #     return await _run_ollama(prompt, config)

    else:
        raise InferenceError(
            f"Unknown inference backend: '{backend}'. "
            f"Valid options: claude_cli. "
            f"Set INFERENCE_BACKEND env var."
        )


# ---------------------------------------------------------------------------
# Claude CLI backend
# ---------------------------------------------------------------------------

async def _run_claude_cli(
    prompt: str,
    config: AgentConfig,
    agent_mode: bool,
) -> str:
    cmd = shlex.split(config.claude_command)
    if not cmd:
        raise InferenceError("CLAUDE_CLI_COMMAND is empty")

    flags = ["--print"]
    if agent_mode:
        flags.append("--dangerously-skip-permissions")

    timeout = config.claude_agent_timeout if agent_mode else config.claude_chat_timeout

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            *flags,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=prompt.encode()),
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise InferenceError(f"Claude CLI not found: {cmd[0]}") from exc
    except asyncio.TimeoutError as exc:
        try:
            proc.kill()
        except Exception:
            pass
        raise InferenceError(f"Claude CLI timed out after {timeout}s") from exc
    except asyncio.CancelledError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        raise

    stdout = (stdout_bytes or b"").decode().strip()
    stderr = (stderr_bytes or b"").decode().strip()

    if proc.returncode != 0:
        raise InferenceError(
            f"Claude CLI exited {proc.returncode}. stderr={stderr or '(empty)'}"
        )
    if not stdout:
        raise InferenceError(
            f"Claude CLI returned empty output. stderr={stderr or '(empty)'}"
        )

    return stdout


# ---------------------------------------------------------------------------
# Anthropic SDK backend (stub — implement when migrating)
# ---------------------------------------------------------------------------

# async def _run_anthropic_sdk(
#     prompt: str,
#     config: AgentConfig,
#     agent_mode: bool,
# ) -> str:
#     """
#     Direct Anthropic API call with tool use support.
#     Requires: pip install anthropic
#     Set ANTHROPIC_API_KEY env var.
#     """
#     import anthropic
#     client = anthropic.AsyncAnthropic()
#     # Build messages from prompt...
#     # Define tools if agent_mode...
#     # Run tool loop...
#     raise NotImplementedError("Anthropic SDK backend not yet implemented")


# ---------------------------------------------------------------------------
# Ollama backend (stub — implement for remote GPU inference)
# ---------------------------------------------------------------------------

# async def _run_ollama(
#     prompt: str,
#     config: AgentConfig,
# ) -> str:
#     """
#     Ollama inference via HTTP API.
#     Pi calls Windows GPU machine at OLLAMA_BASE_URL.
#     """
#     import aiohttp
#     url = f"{config.ollama_base_url}/api/generate"
#     payload = {
#         "model": config.ollama_model,
#         "prompt": prompt,
#         "stream": False,
#     }
#     async with aiohttp.ClientSession() as session:
#         async with session.post(url, json=payload) as resp:
#             data = await resp.json()
#             return data["response"]
