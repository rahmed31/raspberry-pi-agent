"""
agent/commands.py

All Telegram /command handlers.
Each handler is an async method on the CommandHandler class,
injected with state, telegram client, and active task registry.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from agent.config import AGENTS_DIR, INCOMING_DIR, AgentConfig
from agent.scheduler import install_cron, parse_schedule, remove_cron
from agent.workspace import agent_workspace, workspace_tree
from telegram.client import FileTooLargeError, TelegramClient

if TYPE_CHECKING:
    from agent.state import StateStore
    from agent.tasks import NamedAgentTask, OneOffTask

LOGGER = logging.getLogger(__name__)

CONFIRMATION_TIMEOUT = 60  # seconds


class CommandHandler:
    """
    Handles all /command routing and execution.
    Owns the active task registry and confirmation flow state.
    """

    def __init__(
        self,
        config: AgentConfig,
        state: "StateStore",
        telegram: TelegramClient,
    ) -> None:
        self.config   = config
        self.state    = state
        self.telegram = telegram

        # Active task registry
        self._active_one_off_tasks: Dict[str, "OneOffTask"]   = {}  # keyed by task_id
        self._active_named: Dict[str, "NamedAgentTask"]       = {}  # keyed by agent name

        # ask_human IPC — set by the socket server, resolved by handle_update
        self._pending_ask_human_reply: Optional[asyncio.Future] = None

        # Confirmation flow
        self._pending_confirmation: Optional[Dict[str, Any]]  = None
        self._confirmation_expires: float                      = 0.0

    # ------------------------------------------------------------------ #
    #  Public entry points                                                 #
    # ------------------------------------------------------------------ #

    async def handle_update(self, update: Dict[str, Any]) -> None:
        """Route an incoming Telegram update."""
        from agent.tasks import NamedAgentTask, OneOffTask

        chat_id    = TelegramClient.extract_chat_id(update)
        message_id = TelegramClient.extract_message_id(update)
        username   = TelegramClient.extract_username(update)

        if chat_id is None:
            return

        if int(chat_id) != self.config.telegram_chat_id:
            LOGGER.warning("Rejected unauthorized chat_id=%s", chat_id)
            try:
                await self.telegram.send_message(text="Unauthorized.", chat_id=int(chat_id))
            except Exception:
                pass
            return

        # Check for incoming file first
        file_info = TelegramClient.extract_incoming_file(update)
        if file_info:
            await self._handle_incoming_file(int(chat_id), message_id, file_info)
            return

        text = TelegramClient.extract_text(update)
        if not text or not text.strip():
            return

        text = text.strip()
        LOGGER.info("Message from %s: %s", username, text[:80])

        # /cancel always works even while ask_human is waiting
        if text.lower().startswith("/cancel"):
            await self._dispatch_command(int(chat_id), message_id, text)
            return

        # ask_human intercept — route next message to waiting Claude tool call
        if self._pending_ask_human_reply and not self._pending_ask_human_reply.done():
            self._pending_ask_human_reply.set_result(text)
            await self._send(int(chat_id), message_id, "Reply sent to Claude.")
            return

        # Confirmation flow takes priority over everything
        if self._pending_confirmation:
            if time.time() > self._confirmation_expires:
                self._pending_confirmation = None
                await self._send(int(chat_id), message_id, "Confirmation timed out. Action cancelled.")
                return
            if text.lower() in ("yes", "y"):
                action = self._pending_confirmation["action"]
                self._pending_confirmation = None
                await self._execute_confirmed_action(int(chat_id), message_id, action)
                return
            elif text.lower() in ("no", "n"):
                self._pending_confirmation = None
                await self._send(int(chat_id), message_id, "Action cancelled.")
                return

        if text.startswith("/"):
            await self._dispatch_command(int(chat_id), message_id, text)
        else:
            await self._handle_chat(int(chat_id), message_id, text)

    def one_off_is_alive(self) -> bool:
        return any(t.is_alive() for t in self._active_one_off_tasks.values())

    def named_running(self) -> List[str]:
        return [n for n, t in self._active_named.items() if t.is_alive()]

    async def register_ask_human_question(self, question: str) -> asyncio.Future:
        """
        Called by the Unix socket server when ask_human.py connects.
        Sends the question to Telegram and returns a Future that resolves
        when the user replies (via handle_update priority routing).
        """
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_ask_human_reply = fut
        await self.telegram.send_message(
            text=f"Claude needs your input:\n\n{question}",
            chat_id=self.config.telegram_chat_id,
        )
        return fut

    # ------------------------------------------------------------------ #
    #  Command dispatch                                                    #
    # ------------------------------------------------------------------ #

    async def _dispatch_command(
        self, chat_id: int, message_id: Optional[int], text: str
    ) -> None:
        parts   = text.split(maxsplit=1)
        command = parts[0].lower().split("@")[0]
        args    = parts[1].strip() if len(parts) > 1 else ""

        dispatch = {
            "/help":             self._cmd_help,
            "/status":           self._cmd_status,
            "/reset":            self._cmd_reset,
            "/agent":            self._cmd_agent,
            "/task":             self._cmd_task,
            "/cancel":           self._cmd_cancel,
            "/agent:create":     self._cmd_agent_create,
            "/agent:run":        self._cmd_agent_run,
            "/agent:list":       self._cmd_agent_list,
            "/agent:history":    self._cmd_agent_history,
            "/agent:logs":       self._cmd_agent_logs,
            "/agent:knowledge":  self._cmd_agent_knowledge,
            "/agent:files":      self._cmd_agent_files,
            "/agent:file":       self._cmd_agent_file,
            "/agent:schedule":   self._cmd_agent_schedule,
            "/agent:unschedule": self._cmd_agent_unschedule,
            "/agent:delete":     self._cmd_agent_delete,
            "/memory:clear":     self._cmd_memory_clear,
        }

        handler = dispatch.get(command)
        if handler:
            await handler(chat_id, message_id, args)
        else:
            await self._handle_chat(chat_id, message_id, text)

    # ------------------------------------------------------------------ #
    #  General commands                                                    #
    # ------------------------------------------------------------------ #

    async def _cmd_help(self, chat_id: int, message_id: Optional[int], _: str) -> None:
        await self._send(chat_id, message_id, (
            "Commands:\n\n"
            "General:\n"
            "/help — this message\n"
            "/status — runtime info\n"
            "/reset — clear chat history\n\n"
            "One-off tasks:\n"
            "/agent <goal> — run autonomous task (multiple can run concurrently)\n"
            "/task <id> — fetch full result of a task\n"
            "/cancel — cancel all currently running tasks and agents\n"
            "/cancel <task_id> — cancel a specific running one-off task\n"
            "/cancel <agent_name> — stop the current run only (agent + schedule preserved)\n\n"
            "Named agents:\n"
            "/agent:create <n> - <description>\n"
            "/agent:run <n>\n"
            "/agent:list\n"
            "/agent:history <n>\n"
            "/agent:logs <n> [count]\n"
            "/agent:knowledge <n>\n"
            "/agent:files <n>\n"
            "/agent:file <n> <filepath>\n"
            "/agent:schedule <n> daily 8am\n"
            "/agent:schedule <n> weekly monday 9am\n"
            "/agent:unschedule <n>\n"
            "/agent:delete <n>\n\n"
            "Memory:\n"
            "/memory:clear task <id>\n"
            "/memory:clear tasks\n"
            "/memory:clear agent <n>\n"
            "/memory:clear run <run_id>\n"
            "/memory:clear all\n\n"
            "Files:\n"
            "Send any file to save it to incoming/\n"
            "Add caption 'save to <path>' to specify destination\n\n"
            "Any plain message = chat with Claude."
        ))

    async def _cmd_status(self, chat_id: int, message_id: Optional[int], _: str) -> None:
        agents     = await self.state.get_all_named_agents()
        total_runs = 0
        for a in agents:
            total_runs += await self.state.count_agent_runs(a["name"])

        one_off_count  = len(await self.state.get_all_tasks(limit=999999))
        chat_count     = await self.state.count_messages(chat_id)
        running_one_offs = [tid for tid, t in self._active_one_off_tasks.items() if t.is_alive()]
        named_running    = self.named_running()

        one_off_status = (
            f"running ({', '.join(running_one_offs)})" if running_one_offs else "idle"
        )

        await self._send(chat_id, message_id, (
            f"One-off agent: {one_off_status}\n"
            f"Named agents running: {', '.join(named_running) or 'none'}\n"
            f"One-off tasks stored: {one_off_count}\n"
            f"Named agents: {len(agents)}\n"
            f"Named agent runs stored: {total_runs}\n"
            f"Chat messages: {chat_count}\n"
            f"DB size: {await self.state.get_db_size()}\n"
            f"DB path: {self.config.db_path}"
        ))

    async def _cmd_reset(self, chat_id: int, message_id: Optional[int], _: str) -> None:
        await self.state.reset_chat(chat_id)
        await self._send(chat_id, message_id, "Chat history cleared.")

    # ------------------------------------------------------------------ #
    #  One-off agent commands                                              #
    # ------------------------------------------------------------------ #

    async def _cmd_agent(self, chat_id: int, message_id: Optional[int], args: str) -> None:
        if not args:
            await self._send(chat_id, message_id, "Usage: /agent <your goal here>")
            return
        await self._launch_one_off(chat_id, message_id, args)

    async def _cmd_task(self, chat_id: int, message_id: Optional[int], args: str) -> None:
        if not args:
            await self._send(chat_id, message_id, "Usage: /task <id>")
            return
        task = await self.state.get_task(args.strip().lower())
        if not task:
            await self._send(chat_id, message_id, f"No task with ID `{args.strip()}`.")
            return
        full = task["result_full"] or task["result_preview"] or "(no result stored)"
        await self._send(chat_id, message_id, (
            f"Task `{task['task_id']}` — {task['status']}\n"
            f"Goal: {task['goal']}\n"
            f"Started: {task['started_at'][:16]}\n"
            f"Finished: {(task['finished_at'] or 'n/a')[:16]}\n\n"
            f"{full}"
        ))

    async def _cmd_cancel(self, chat_id: int, message_id: Optional[int], args: str) -> None:
        # NOTE: Scheduled runs (cron) are separate OS processes and cannot be cancelled here.
        # To kill a stuck scheduled run, SSH to the Pi and kill the agent_main.py process.

        # Prune dead entries before working with the registries
        self._active_one_off_tasks = {tid: t for tid, t in self._active_one_off_tasks.items() if t.is_alive()}
        self._active_named = {n: t for n, t in self._active_named.items() if t.is_alive()}

        # Resolve any pending ask_human Future so the socket server exits cleanly
        def _resolve_ask_human() -> None:
            if self._pending_ask_human_reply and not self._pending_ask_human_reply.done():
                self._pending_ask_human_reply.set_result("(task cancelled)")
                self._pending_ask_human_reply = None

        target = args.strip().lower() if args else ""

        if target:
            # Targeted cancel by task_id, agent name, or run_id
            if target in self._active_one_off_tasks:
                _resolve_ask_human()
                self._active_one_off_tasks[target].cancel()
                await self._send(chat_id, message_id, f"Cancellation requested for one-off task `{target}`.")
                return
            if target in self._active_named:
                _resolve_ask_human()
                run_id = self._active_named[target].run_id
                self._active_named[target].cancel()
                await self._send(chat_id, message_id, f"Cancellation requested for agent `{target}` run `{run_id}`.")
                return
            for name, task in self._active_named.items():
                if task.run_id == target:
                    _resolve_ask_human()
                    task.cancel()
                    await self._send(chat_id, message_id, f"Cancellation requested for agent `{name}` run `{target}`.")
                    return
            await self._send(chat_id, message_id, f"No running task matching `{target}`.")
            return

        # Cancel all running tasks
        cancelled = []
        for tid, task in list(self._active_one_off_tasks.items()):
            task.cancel()
            cancelled.append(f"one-off task `{tid}`")
        for name, task in list(self._active_named.items()):
            run_id = task.run_id
            task.cancel()
            cancelled.append(f"named agent `{name}` run `{run_id}`")

        if cancelled:
            _resolve_ask_human()
            await self._send(chat_id, message_id, f"Cancellation requested for: {', '.join(cancelled)}")
        else:
            await self._send(chat_id, message_id, "No tasks currently running.")

    # ------------------------------------------------------------------ #
    #  Named agent commands                                                #
    # ------------------------------------------------------------------ #

    async def _cmd_agent_create(
        self, chat_id: int, message_id: Optional[int], args: str
    ) -> None:
        if " - " not in args:
            await self._send(chat_id, message_id, "Usage: /agent:create <n> - <description>")
            return
        name, description = args.split(" - ", 1)
        name        = name.strip().lower().replace(" ", "_")
        description = description.strip()
        if await self.state.get_named_agent(name):
            await self._send(chat_id, message_id,
                f"Agent `{name}` already exists. Use /agent:run {name} to run it.")
            return
        await self.state.create_named_agent(name, description)
        agent_workspace(name)
        await self._send(chat_id, message_id, (
            f"Agent `{name}` created.\n"
            f"Purpose: {description}\n"
            f"Workspace: {AGENTS_DIR}/{name}/\n\n"
            f"Run it with: /agent:run {name}"
        ))

    async def _cmd_agent_run(
        self, chat_id: int, message_id: Optional[int], args: str
    ) -> None:
        if not args:
            await self._send(chat_id, message_id, "Usage: /agent:run <n>")
            return
        name  = args.strip().lower()
        agent = await self.state.get_named_agent(name)
        if not agent:
            await self._send(chat_id, message_id,
                f"No agent named `{name}`. Use /agent:list to see all agents.")
            return
        await self._launch_named_agent(chat_id, message_id, name, agent["description"])

    async def _cmd_agent_list(
        self, chat_id: int, message_id: Optional[int], _: str
    ) -> None:
        agents = await self.state.get_all_named_agents()
        if not agents:
            await self._send(chat_id, message_id,
                "No named agents yet. Create one with /agent:create")
            return
        lines = ["Named Agents:\n"]
        for a in agents:
            run_count = await self.state.count_agent_runs(a["name"])
            last_run  = a["last_run_at"][:16] if a["last_run_at"] else "never"
            schedule  = f"\n  Schedule: {a['cron_schedule']}" if a["cron_schedule"] else ""
            running   = (
                " [RUNNING]"
                if a["name"] in self._active_named and self._active_named[a["name"]].is_alive()
                else ""
            )
            lines.append(
                f"• {a['name']}{running}\n"
                f"  {a['description']}\n"
                f"  Runs: {run_count} | Last: {last_run}{schedule}"
            )
        await self._send(chat_id, message_id, "\n".join(lines))

    async def _cmd_agent_history(
        self, chat_id: int, message_id: Optional[int], args: str
    ) -> None:
        if not args:
            await self._send(chat_id, message_id, "Usage: /agent:history <n>")
            return
        name  = args.strip().lower()
        agent = await self.state.get_named_agent(name)
        if not agent:
            await self._send(chat_id, message_id, f"No agent named `{name}`.")
            return
        runs = await self.state.get_agent_runs(name)
        if not runs:
            await self._send(chat_id, message_id, f"Agent `{name}` has no runs yet.")
            return
        lines = [f"History for `{name}` ({len(runs)} runs):\n"]
        for r in runs:
            icon     = "✓" if r["status"] == "completed" else "✗" if r["status"] == "failed" else "⏱" if r["status"] == "timeout" else "~"
            duration = ""
            if r["finished_at"] and r["started_at"]:
                try:
                    start    = datetime.fromisoformat(r["started_at"])
                    end      = datetime.fromisoformat(r["finished_at"])
                    duration = f" | {int((end - start).total_seconds())}s"
                except Exception:
                    pass
            snippet  = (r["result"] or "")[:150]
            ellipsis = "..." if r["result"] and len(r["result"]) > 150 else ""
            lines.append(
                f"[{icon}] run:{r['run_id']} | {r['started_at'][:16]}{duration}\n"
                f"  {snippet}{ellipsis}"
            )
        await self._send(chat_id, message_id, "\n".join(lines))

    async def _cmd_agent_logs(
        self, chat_id: int, message_id: Optional[int], args: str
    ) -> None:
        parts2 = args.split(maxsplit=1)
        name   = parts2[0].strip().lower() if parts2 else ""
        try:
            n = int(parts2[1].strip()) if len(parts2) > 1 else 5
        except ValueError:
            n = 5
        if not name:
            await self._send(chat_id, message_id, "Usage: /agent:logs <n> [count]")
            return
        agent = await self.state.get_named_agent(name)
        if not agent:
            await self._send(chat_id, message_id, f"No agent named `{name}`.")
            return
        all_runs = await self.state.get_agent_runs(name)
        runs = all_runs[:n]
        if not runs:
            await self._send(chat_id, message_id, f"Agent `{name}` has no runs yet.")
            return
        lines = [f"Last {min(n, len(runs))} runs for `{name}`:\n"]
        for r in runs:
            icon     = "✓" if r["status"] == "completed" else "✗" if r["status"] == "failed" else "⏱" if r["status"] == "timeout" else "~"
            duration = ""
            if r["finished_at"] and r["started_at"]:
                try:
                    start    = datetime.fromisoformat(r["started_at"])
                    end      = datetime.fromisoformat(r["finished_at"])
                    duration = f" | {int((end - start).total_seconds())}s"
                except Exception:
                    pass
            snippet  = (r["result"] or "")[:300]
            ellipsis = "..." if r["result"] and len(r["result"]) > 300 else ""
            lines.append(
                f"[{icon}] run:{r['run_id']} | {r['started_at'][:16]}{duration}\n"
                f"  {snippet}{ellipsis}"
            )
        await self._send(chat_id, message_id, "\n".join(lines))

    async def _cmd_agent_knowledge(
        self, chat_id: int, message_id: Optional[int], args: str
    ) -> None:
        if not args:
            await self._send(chat_id, message_id, "Usage: /agent:knowledge <n>")
            return
        name  = args.strip().lower()
        agent = await self.state.get_named_agent(name)
        if not agent:
            await self._send(chat_id, message_id, f"No agent named `{name}`.")
            return
        kb_path = AGENTS_DIR / name / "knowledge_base.json"
        if not kb_path.exists():
            await self._send(chat_id, message_id, f"No knowledge base found for `{name}`.")
            return
        try:
            kb    = json.loads(kb_path.read_text())
            lines = [f"Knowledge Base: `{name}`\n"]
            lines.append(f"Purpose: {kb.get('purpose') or '(not set)'}")
            lines.append(f"Last updated: {kb.get('last_updated') or '(never)'}")
            lines.append(f"Run count: {kb.get('run_count', 0)}")

            knowledge = kb.get("knowledge", {})
            if knowledge.get("key_findings"):
                lines.append("\nKey Findings:")
                for item in knowledge["key_findings"]:
                    lines.append(f"  • {item}")
            if knowledge.get("patterns_observed"):
                lines.append("\nPatterns Observed:")
                for item in knowledge["patterns_observed"]:
                    lines.append(f"  • {item}")
            if knowledge.get("successful_approaches"):
                lines.append("\nSuccessful Approaches:")
                for item in knowledge["successful_approaches"]:
                    lines.append(f"  • {item}")
            if knowledge.get("failed_approaches"):
                lines.append("\nFailed Approaches:")
                for item in knowledge["failed_approaches"]:
                    lines.append(f"  • {item}")
            if knowledge.get("open_questions"):
                lines.append("\nOpen Questions:")
                for item in knowledge["open_questions"]:
                    lines.append(f"  • {item}")

            state = kb.get("state", {})
            lines.append(f"\nCurrent Status: {state.get('current_status') or '(not set)'}")
            lines.append(f"Last Action: {state.get('last_action') or '(not set)'}")
            if state.get("next_steps"):
                lines.append("Next Steps:")
                for step in state["next_steps"]:
                    lines.append(f"  • {step}")

            resources = kb.get("resources", {})
            if resources.get("tools_built"):
                lines.append("\nTools Built:")
                for tool in resources["tools_built"]:
                    lines.append(f"  • {tool}")

            run_log = kb.get("run_log", [])
            if run_log:
                lines.append(f"\nLast 3 Run Log Entries:")
                for entry in run_log[-3:]:
                    lines.append(
                        f"  [{entry.get('run_id', '?')}] "
                        f"{str(entry.get('timestamp', ''))[:16]} — "
                        f"{str(entry.get('outcome', ''))[:100]}"
                    )

            await self._send(chat_id, message_id, "\n".join(lines))
        except Exception as exc:
            await self._send(chat_id, message_id, f"Could not read knowledge base: {exc}")

    async def _cmd_agent_files(
        self, chat_id: int, message_id: Optional[int], args: str
    ) -> None:
        if not args:
            await self._send(chat_id, message_id, "Usage: /agent:files <n>")
            return
        name  = args.strip().lower()
        agent = await self.state.get_named_agent(name)
        if not agent:
            await self._send(chat_id, message_id, f"No agent named `{name}`.")
            return
        tree = workspace_tree(name)
        await self._send(chat_id, message_id, f"Workspace for `{name}`:\n\n{tree}")

    async def _cmd_agent_file(
        self, chat_id: int, message_id: Optional[int], args: str
    ) -> None:
        parts2 = args.split(maxsplit=1)
        if len(parts2) < 2:
            await self._send(chat_id, message_id, "Usage: /agent:file <n> <filepath>")
            return
        name, filepath = parts2[0].strip().lower(), parts2[1].strip()
        agent = await self.state.get_named_agent(name)
        if not agent:
            await self._send(chat_id, message_id, f"No agent named `{name}`.")
            return
        workspace = AGENTS_DIR / name
        target    = (workspace / filepath).resolve()
        try:
            target.relative_to(workspace.resolve())
        except ValueError:
            await self._send(chat_id, message_id, "Access denied: path is outside agent workspace.")
            return
        if not target.exists():
            await self._send(chat_id, message_id, f"File not found: `{filepath}`")
            return
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
            await self._send(chat_id, message_id, f"`{filepath}`:\n\n{content}")
        except Exception as exc:
            await self._send(chat_id, message_id, f"Could not read file: {exc}")

    async def _cmd_agent_schedule(
        self, chat_id: int, message_id: Optional[int], args: str
    ) -> None:
        parts2 = args.split(maxsplit=1)
        if len(parts2) < 2:
            await self._send(chat_id, message_id,
                "Usage: /agent:schedule <n> daily 8am\n"
                "  or:  /agent:schedule <n> weekly monday 9am")
            return
        name, schedule_str = parts2[0].strip().lower(), parts2[1].strip()
        agent = await self.state.get_named_agent(name)
        if not agent:
            await self._send(chat_id, message_id, f"No agent named `{name}`.")
            return
        cron_expr = parse_schedule(schedule_str)
        if not cron_expr:
            await self._send(chat_id, message_id,
                "Could not parse schedule. Examples:\n"
                "  daily 8am\n  daily 6:30pm\n  weekly monday 9am\n  weekly fri 5pm")
            return
        try:
            from agent.config import PROJECT_DIR
            agent_main = str(PROJECT_DIR / "agent_main.py")
            job_id = install_cron(name, cron_expr, agent_main)
            await self.state.update_named_agent_schedule(name, schedule_str, job_id)
            await self._send(chat_id, message_id,
                f"Agent `{name}` scheduled: {schedule_str}\n(cron: {cron_expr})")
        except RuntimeError as exc:
            await self._send(chat_id, message_id, f"Failed to schedule: {exc}")

    async def _cmd_agent_unschedule(
        self, chat_id: int, message_id: Optional[int], args: str
    ) -> None:
        if not args:
            await self._send(chat_id, message_id, "Usage: /agent:unschedule <n>")
            return
        name  = args.strip().lower()
        agent = await self.state.get_named_agent(name)
        if not agent:
            await self._send(chat_id, message_id, f"No agent named `{name}`.")
            return
        if not agent["cron_job_id"]:
            await self._send(chat_id, message_id, f"Agent `{name}` has no schedule.")
            return
        remove_cron(agent["cron_job_id"])
        await self.state.update_named_agent_schedule(name, None, None)
        await self._send(chat_id, message_id, f"Schedule removed from agent `{name}`.")

    async def _cmd_agent_delete(
        self, chat_id: int, message_id: Optional[int], args: str
    ) -> None:
        if not args:
            await self._send(chat_id, message_id, "Usage: /agent:delete <n>")
            return
        name  = args.strip().lower()
        agent = await self.state.get_named_agent(name)
        if not agent:
            await self._send(chat_id, message_id, f"No agent named `{name}`.")
            return
        run_count = await self.state.count_agent_runs(name)
        workspace = AGENTS_DIR / name
        ws_note   = f"\nWorkspace `{workspace}` will also be deleted." if workspace.exists() else ""
        await self._request_confirmation(
            chat_id, message_id,
            prompt=(
                f"This will permanently delete agent `{name}`, "
                f"all {run_count} run records, and its workspace directory.{ws_note}\n"
                f"Reply yes/y to confirm or no/n to cancel."
            ),
            action={"type": "delete_agent", "name": name},
        )

    # ------------------------------------------------------------------ #
    #  Memory clear commands                                               #
    # ------------------------------------------------------------------ #

    async def _cmd_memory_clear(
        self, chat_id: int, message_id: Optional[int], args: str
    ) -> None:
        parts = args.strip().split(maxsplit=1)
        if not parts:
            await self._send(chat_id, message_id, (
                "Usage:\n"
                "/memory:clear task <id>\n"
                "/memory:clear tasks\n"
                "/memory:clear agent <n>\n"
                "/memory:clear run <run_id>\n"
                "/memory:clear all"
            ))
            return

        subcommand = parts[0].lower()
        subargs    = parts[1].strip() if len(parts) > 1 else ""

        if subcommand == "task":
            if not subargs:
                await self._send(chat_id, message_id, "Usage: /memory:clear task <id>")
                return
            task = await self.state.get_task(subargs)
            if not task:
                await self._send(chat_id, message_id, f"No task with ID `{subargs}`.")
                return
            await self._request_confirmation(
                chat_id, message_id,
                prompt=(
                    f"This will permanently delete task `{subargs}`\n"
                    f"Goal: {task['goal'][:80]}\n"
                    f"Reply yes/y to confirm."
                ),
                action={"type": "clear_task", "task_id": subargs},
            )

        elif subcommand == "tasks":
            tasks = await self.state.get_all_tasks(limit=999999)
            await self._request_confirmation(
                chat_id, message_id,
                prompt=f"This will permanently delete all {len(tasks)} one-off task records.\nReply yes/y to confirm.",
                action={"type": "clear_tasks"},
            )

        elif subcommand == "agent":
            if not subargs:
                await self._send(chat_id, message_id, "Usage: /memory:clear agent <n>")
                return
            name  = subargs.lower()
            agent = await self.state.get_named_agent(name)
            if not agent:
                await self._send(chat_id, message_id, f"No agent named `{name}`.")
                return
            run_count = await self.state.count_agent_runs(name)
            await self._request_confirmation(
                chat_id, message_id,
                prompt=(
                    f"This will permanently delete all {run_count} run records for agent `{name}`.\n"
                    f"The agent itself and its workspace will NOT be deleted.\n"
                    f"Reply yes/y to confirm."
                ),
                action={"type": "clear_agent_runs", "name": name},
            )

        elif subcommand == "run":
            if not subargs:
                await self._send(chat_id, message_id, "Usage: /memory:clear run <run_id>")
                return
            run = await self.state.get_agent_run(subargs)
            if not run:
                await self._send(chat_id, message_id, f"No run with ID `{subargs}`.")
                return
            await self._request_confirmation(
                chat_id, message_id,
                prompt=(
                    f"This will permanently delete run `{subargs}` from agent `{run['agent_name']}`.\n"
                    f"Reply yes/y to confirm."
                ),
                action={"type": "clear_run", "run_id": subargs},
            )

        elif subcommand == "all":
            tasks = await self.state.get_all_tasks(limit=999999)
            async with self.state.db.execute("SELECT COUNT(*) FROM named_agent_runs") as cur:
                runs = (await cur.fetchone())[0]
            msgs  = await self.state.count_messages(chat_id)
            await self._request_confirmation(
                chat_id, message_id,
                prompt=(
                    f"This will permanently delete:\n"
                    f"- {len(tasks)} one-off task records\n"
                    f"- {runs} named agent run records\n"
                    f"- {msgs} chat messages\n\n"
                    f"Named agents and their workspaces will NOT be deleted.\n"
                    f"Reply yes/y to confirm."
                ),
                action={"type": "clear_all"},
            )

        else:
            await self._send(chat_id, message_id, f"Unknown clear target: `{subcommand}`")

    # ------------------------------------------------------------------ #
    #  Confirmation flow                                                   #
    # ------------------------------------------------------------------ #

    async def _request_confirmation(
        self,
        chat_id: int,
        message_id: Optional[int],
        prompt: str,
        action: Dict[str, Any],
    ) -> None:
        self._pending_confirmation = {"chat_id": chat_id, "action": action}
        self._confirmation_expires = time.time() + CONFIRMATION_TIMEOUT
        await self._send(chat_id, message_id, prompt)

    async def _execute_confirmed_action(
        self,
        chat_id: int,
        message_id: Optional[int],
        action: Dict[str, Any],
    ) -> None:
        atype = action["type"]

        if atype == "delete_agent":
            name  = action["name"]
            agent = await self.state.get_named_agent(name)
            if agent and agent["cron_job_id"]:
                remove_cron(agent["cron_job_id"])
            await self.state.delete_named_agent(name)
            workspace = AGENTS_DIR / name
            ws_note = ""
            if workspace.exists():
                try:
                    shutil.rmtree(workspace)
                    ws_note = f"\nWorkspace `{workspace}` deleted."
                except Exception as exc:
                    ws_note = f"\nWarning: could not delete workspace: {exc}"
            await self._send(chat_id, message_id,
                f"Agent `{name}` and all its run history deleted.{ws_note}")

        elif atype == "clear_task":
            await self.state.delete_task(action["task_id"])
            await self._send(chat_id, message_id, f"Task `{action['task_id']}` deleted.")

        elif atype == "clear_tasks":
            count = await self.state.delete_all_tasks()
            await self._send(chat_id, message_id, f"Deleted {count} one-off task records.")

        elif atype == "clear_agent_runs":
            count = await self.state.delete_agent_runs(action["name"])
            await self._send(chat_id, message_id,
                f"Deleted {count} run records for agent `{action['name']}`.")

        elif atype == "clear_run":
            await self.state.delete_agent_run(action["run_id"])
            await self._send(chat_id, message_id, f"Run `{action['run_id']}` deleted.")

        elif atype == "clear_all":
            counts = await self.state.clear_all()
            await self._send(chat_id, message_id, (
                f"Cleared:\n"
                f"- {counts['tasks']} one-off task records\n"
                f"- {counts['runs']} named agent run records\n"
                f"- {counts['messages']} chat messages"
            ))

    # ------------------------------------------------------------------ #
    #  Incoming file handler                                               #
    # ------------------------------------------------------------------ #

    async def _handle_incoming_file(
        self,
        chat_id: int,
        message_id: Optional[int],
        file_info: Dict[str, Any],
    ) -> None:
        caption   = file_info.get("caption") or ""
        file_name = file_info["file_name"]

        save_match = re.search(r"save to\s+(\S+)", caption, re.IGNORECASE)
        if save_match:
            custom_path = os.path.expanduser(save_match.group(1).strip())
            destination = Path(custom_path)
            if not destination.suffix:
                destination = destination / file_name
        else:
            destination = INCOMING_DIR / file_name

        try:
            saved_path = await self.telegram.download_file(
                file_info["file_id"], str(destination)
            )
            await self._send(chat_id, message_id, (
                f"File received: `{file_name}`\n"
                f"Type: {file_info['file_type']}\n"
                f"Saved to: `{saved_path}`"
            ))
        except FileTooLargeError as exc:
            await self._send(chat_id, message_id, f"File too large: {exc}")
        except Exception as exc:
            LOGGER.exception("Failed to download file %s", file_name)
            await self._send(chat_id, message_id, (
                f"Failed to save `{file_name}` to `{destination}`.\n"
                f"Error: {exc}\n\n"
                f"Where would you like me to save it instead? Reply with a path."
            ))

    # ------------------------------------------------------------------ #
    #  Chat mode                                                           #
    # ------------------------------------------------------------------ #

    async def _handle_chat(
        self, chat_id: int, message_id: Optional[int], text: str
    ) -> None:
        from agent.claude import InferenceError, get_response
        from agent.prompts import build_chat_prompt

        await self.state.append_message(chat_id, "user", text)
        history         = await self.state.get_history(chat_id, limit=self.config.max_history)
        context_summary = await self.state.build_agent_context_summary()

        await self.telegram.send_chat_action(chat_id)

        prompt = build_chat_prompt(history, context_summary)

        try:
            reply = await get_response(prompt, self.config, agent_mode=False)
        except InferenceError as exc:
            LOGGER.exception("Inference error in chat")
            await self._send(chat_id, message_id, f"Claude error: {exc}")
            return

        await self.state.append_message(chat_id, "assistant", reply)
        await self._send(chat_id, message_id, reply)

    # ------------------------------------------------------------------ #
    #  Task launchers                                                      #
    # ------------------------------------------------------------------ #

    async def _launch_one_off(
        self, chat_id: int, message_id: Optional[int], goal: str
    ) -> None:
        from agent.tasks import OneOffTask

        # Prune dead entries before adding a new one
        self._active_one_off_tasks = {tid: t for tid, t in self._active_one_off_tasks.items() if t.is_alive()}

        task_id = await self.state.generate_task_id()
        task    = OneOffTask(
            task_id=task_id,
            goal=goal,
            chat_id=chat_id,
            config=self.config,
            state=self.state,
            telegram=self.telegram,
        )
        self._active_one_off_tasks[task_id] = task
        task.start()
        await self._send(chat_id, message_id, (
            f"Agent started. Task ID: `{task_id}`\n"
            f"Goal: {goal}\n\n"
            f"Working..."
        ))

    async def _launch_named_agent(
        self, chat_id: int, message_id: Optional[int], name: str, description: str
    ) -> None:
        from agent.tasks import NamedAgentTask

        # Prune dead entries before checking for conflicts
        self._active_named = {n: t for n, t in self._active_named.items() if t.is_alive()}

        existing = self._active_named.get(name)
        if existing and existing.is_alive():
            await self._send(chat_id, message_id,
                f"Agent `{name}` is already running (run `{existing.run_id}`). "
                f"Use /cancel {name} to stop it first.")
            return

        run_id  = await self.state.generate_run_id()
        history = await self._build_run_history(name)
        task    = NamedAgentTask(
            agent_name=name,
            run_id=run_id,
            description=description,
            run_history=history,
            chat_id=chat_id,
            config=self.config,
            state=self.state,
            telegram=self.telegram,
        )
        self._active_named[name] = task
        task.start()
        await self._send(chat_id, message_id,
            f"Agent `{name}` started. Run ID: `{run_id}`\n\nWorking...")

    async def _build_run_history(self, agent_name: str) -> str:
        runs = await self.state.get_agent_runs(agent_name)
        if not runs:
            return ""
        parts = [f"Past runs for agent '{agent_name}':"]
        for r in runs:
            snippet  = (r["result"] or "")[:1500]
            ellipsis = "..." if r["result"] and len(r["result"]) > 1500 else ""
            parts.append(
                f"\n[{r['status']}] run:{r['run_id']} at {r['started_at'][:16]}\n"
                f"Result: {snippet}{ellipsis}"
            )
        return "\n".join(parts)

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    async def _send(
        self, chat_id: int, message_id: Optional[int], text: str
    ) -> None:
        try:
            await self.telegram.send_long_message(
                text=text,
                chat_id=chat_id,
                reply_to_message_id=message_id,
                chunk_size=self.config.chunk_size,
            )
        except TelegramAPIError:
            LOGGER.exception("Failed to send message")
