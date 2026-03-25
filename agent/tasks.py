"""
agent/tasks.py

Async task runners for one-off and named persistent agents.
Each task runs as an asyncio Task (not a thread).
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from agent.claude import InferenceError, get_response
from agent.config import AGENTS_DIR, AgentConfig
from agent.prompts import build_named_agent_prompt, build_one_off_prompt
from agent.config import TMP_DIR
from agent.workspace import (
    backup_knowledge_base,
    validate_knowledge_base,
    validate_knowledge_base_post_run,
    wipe_tmp,
)

if TYPE_CHECKING:
    from agent.state import StateStore
    from telegram.client import TelegramClient

LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# One-off task
# ---------------------------------------------------------------------------

class OneOffTask:
    """
    Runs a single autonomous agent task and cleans up tmp/ after delivery.
    """

    def __init__(
        self,
        task_id: str,
        goal: str,
        chat_id: int,
        config: AgentConfig,
        state: "StateStore",
        telegram: "TelegramClient",
    ) -> None:
        self.task_id   = task_id
        self.goal      = goal
        self.chat_id   = chat_id
        self.config    = config
        self.state     = state
        self.telegram  = telegram
        self._cancel   = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"one_off_{self.task_id}")

    def cancel(self) -> None:
        self._cancel.set()
        if self._task:
            self._task.cancel()

    def is_alive(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _send(self, text: str) -> bool:
        try:
            await self.telegram.send_long_message(
                text=text, chat_id=self.chat_id, chunk_size=self.config.chunk_size
            )
            return True
        except Exception:
            LOGGER.exception("Failed to send one-off task message")
            return False

    async def _run(self) -> None:
        LOGGER.info("One-off task %s started: %s", self.task_id, self.goal)
        TMP_DIR.mkdir(parents=True, exist_ok=True)

        await self.state.create_task(self.task_id, self.goal)

        if self._cancel.is_set():
            await self.state.finish_task(self.task_id, "Cancelled before start.", "cancelled")
            await self._send(f"Task `{self.task_id}` cancelled before it could start.")
            return

        prompt = build_one_off_prompt(self.goal, self.config.ask_human_path)

        try:
            result = await get_response(prompt, self.config, agent_mode=True)
            status = "cancelled" if self._cancel.is_set() else "completed"
            await self.state.finish_task(self.task_id, result, status)
            prefix = (
                f"Task `{self.task_id}` cancelled. Partial result"
                if status == "cancelled"
                else f"Task `{self.task_id}` finished"
            )
            sent = await self._send(f"{prefix}.\n\n{result}")
            if sent:
                wipe_tmp()
            else:
                LOGGER.warning("Skipping tmp wipe — delivery failed for task %s", self.task_id)

        except InferenceError as exc:
            if "timed out" in str(exc).lower():
                await self.state.finish_task(self.task_id, str(exc), "timeout")
                sent = await self._send(
                    f"⏱ Task `{self.task_id}` timed out after "
                    f"{self.config.claude_agent_timeout // 60} minutes. "
                    f"Partial work may have been done. Status: timeout."
                )
            else:
                await self.state.finish_task(self.task_id, str(exc), "failed")
                sent = await self._send(f"Task `{self.task_id}` failed: {exc}")
            if sent:
                wipe_tmp()

        except asyncio.CancelledError:
            await self.state.finish_task(self.task_id, "Cancelled.", "cancelled")
            await self._send(f"Task `{self.task_id}` was cancelled.")
            wipe_tmp()

        except Exception as exc:
            LOGGER.exception("Unexpected error in task %s", self.task_id)
            await self.state.finish_task(self.task_id, str(exc), "failed")
            sent = await self._send(f"Task `{self.task_id}` unexpected error: {exc}")
            if sent:
                wipe_tmp()


# ---------------------------------------------------------------------------
# Named agent task
# ---------------------------------------------------------------------------

class NamedAgentTask:
    """
    Runs a named persistent agent with workspace and knowledge base management.
    """

    def __init__(
        self,
        agent_name: str,
        run_id: str,
        description: str,
        run_history: str,
        chat_id: int,
        config: AgentConfig,
        state: "StateStore",
        telegram: "TelegramClient",
    ) -> None:
        self.agent_name  = agent_name
        self.run_id      = run_id
        self.description = description
        self.run_history = run_history
        self.chat_id     = chat_id
        self.config      = config
        self.state       = state
        self.telegram    = telegram
        self._cancel     = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.create_task(
            self._run(), name=f"named_{self.agent_name}_{self.run_id}"
        )

    def cancel(self) -> None:
        self._cancel.set()
        if self._task:
            self._task.cancel()

    def is_alive(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _send(self, text: str) -> None:
        try:
            await self.telegram.send_long_message(
                text=text, chat_id=self.chat_id, chunk_size=self.config.chunk_size
            )
        except Exception:
            LOGGER.exception("Failed to send named agent message")

    async def _run(self) -> None:
        LOGGER.info("Named agent %s run %s started", self.agent_name, self.run_id)

        await self.state.create_agent_run(self.run_id, self.agent_name)

        if self._cancel.is_set():
            await self.state.finish_agent_run(self.run_id, "Cancelled before start.", "cancelled")
            await self._send(
                f"Agent `{self.agent_name}` run `{self.run_id}` cancelled before start."
            )
            return

        # Backup knowledge base before run
        backup_knowledge_base(self.agent_name)

        # Validate knowledge base — restore from backup or scaffold if corrupted
        kb_valid, kb_warning = validate_knowledge_base(self.agent_name)
        if kb_warning:
            await self._send(f"⚠️ {kb_warning}")
        if not kb_valid:
            await self.state.finish_agent_run(
                self.run_id, "Knowledge base validation failed.", "failed"
            )
            await self._send(
                f"Agent `{self.agent_name}` run `{self.run_id}` failed: "
                f"could not initialize knowledge base."
            )
            return

        # Capture pre-run run_count for post-run validation
        pre_run_count = 0
        try:
            kb_path = AGENTS_DIR / self.agent_name / "knowledge_base.json"
            kb_data = json.loads(kb_path.read_text())
            pre_run_count = kb_data.get("run_count", 0)
        except Exception:
            pass

        prompt = build_named_agent_prompt(
            agent_name=self.agent_name,
            ask_human_path=self.config.ask_human_path,
            run_history=self.run_history,
        )

        try:
            result = await get_response(prompt, self.config, agent_mode=True)
            status = "cancelled" if self._cancel.is_set() else "completed"
            await self.state.finish_agent_run(self.run_id, result, status)

            # Validate knowledge base post-run
            kb_post_warning = validate_knowledge_base_post_run(
                self.agent_name, pre_run_count
            )
            if kb_post_warning:
                await self._send(kb_post_warning)

            prefix = (
                f"Agent `{self.agent_name}` run `{self.run_id}` cancelled. Partial result"
                if status == "cancelled"
                else f"Agent `{self.agent_name}` run `{self.run_id}` finished"
            )
            await self._send(f"{prefix}.\n\n{result}")

        except InferenceError as exc:
            if "timed out" in str(exc).lower():
                await self.state.finish_agent_run(self.run_id, str(exc), "timeout")
                await self._send(
                    f"⏱ Agent `{self.agent_name}` run `{self.run_id}` timed out after "
                    f"{self.config.claude_agent_timeout // 60} minutes. "
                    f"Knowledge base was not updated this run — pre-run backup is preserved."
                )
            else:
                await self.state.finish_agent_run(self.run_id, str(exc), "failed")
                await self._send(
                    f"Agent `{self.agent_name}` run `{self.run_id}` failed: {exc}"
                )

        except asyncio.CancelledError:
            await self.state.finish_agent_run(self.run_id, "Cancelled.", "cancelled")
            await self._send(
                f"Agent `{self.agent_name}` run `{self.run_id}` was cancelled."
            )

        except Exception as exc:
            LOGGER.exception(
                "Unexpected error in named agent %s run %s", self.agent_name, self.run_id
            )
            await self.state.finish_agent_run(self.run_id, str(exc), "failed")
            await self._send(
                f"Agent `{self.agent_name}` run `{self.run_id}` unexpected error: {exc}"
            )
