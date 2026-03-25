#!/usr/bin/env python3
"""
agent.py — Entry point for the Raspberry Pi Agent.

Usage:
    python agent.py                        # normal operation
    python agent.py --scheduled-agent <n>  # run a scheduled named agent

Environment variables (required):
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

See agent/config.py for all optional environment variables.
"""

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from agent.commands import CommandHandler
from agent.config import AgentConfig, ConfigError
from agent.state import StateStore
from agent.workspace import ensure_dirs
from telegram.client import TelegramAPIError, TelegramClient

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
LOGGER = logging.getLogger("agent_main")


# ---------------------------------------------------------------------------
# ask_human Unix socket server
# ---------------------------------------------------------------------------

async def _handle_ask_human_conn(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    handler: CommandHandler,
) -> None:
    """Handle one ask_human.py connection: read question, wait for reply, send back."""
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=10)
        question = line.decode().strip()
        if not question:
            writer.close()
            return

        fut = await handler.register_ask_human_question(question)

        try:
            reply = await asyncio.wait_for(fut, timeout=600)
        except asyncio.TimeoutError:
            reply = "(no reply received within timeout)"
            try:
                await handler.telegram.send_message(
                    "No reply received within 10 minutes. Claude has continued without your input."
                )
            except Exception:
                pass
        except asyncio.CancelledError:
            reply = "(task cancelled)"

        writer.write((reply + "\n").encode())
        await writer.drain()
    except Exception:
        LOGGER.exception("Error in ask_human socket handler")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def start_ask_human_server(
    handler: CommandHandler, config: AgentConfig
) -> asyncio.AbstractServer:
    """Start the Unix socket server for ask_human IPC."""
    sock_path = config.ask_human_sock_path
    # Ensure tmp/ exists — on a fresh clone it may not yet exist if ensure_dirs()
    # was skipped (e.g. scheduled-agent path) or the directory was manually deleted.
    Path(sock_path).parent.mkdir(parents=True, exist_ok=True)
    # Remove stale socket file left over from a previous run
    try:
        Path(sock_path).unlink()
    except FileNotFoundError:
        pass

    server = await asyncio.start_unix_server(
        lambda r, w: _handle_ask_human_conn(r, w, handler),
        path=sock_path,
    )
    LOGGER.info("ask_human socket server listening at %s", sock_path)
    return server


# ---------------------------------------------------------------------------
# Scheduled agent runner (invoked by cron)
# ---------------------------------------------------------------------------

async def run_scheduled_agent(agent_name: str, config: AgentConfig) -> None:
    """
    Called by cron via: python agent_main.py --scheduled-agent <n>
    Runs a named agent once and exits.
    """
    from agent.tasks import NamedAgentTask

    store = StateStore(config.db_path)
    await store.init()

    async with TelegramClient(
        bot_token=config.telegram_bot_token,
        default_chat_id=config.telegram_chat_id,
        request_timeout=config.request_timeout,
    ) as telegram:
        agent = await store.get_named_agent(agent_name)
        if not agent:
            LOGGER.error("Scheduled agent not found: %s", agent_name)
            await store.close()
            return

        # Build run history
        run_id = await store.generate_run_id()
        runs   = await store.get_agent_runs(agent_name)
        parts  = [f"Past runs for agent '{agent_name}':"]
        for r in runs:
            snippet  = (r["result"] or "")[:1500]
            ellipsis = "..." if r["result"] and len(r["result"]) > 1500 else ""
            parts.append(
                f"\n[{r['status']}] run:{r['run_id']} at {r['started_at'][:16]}\n"
                f"Result: {snippet}{ellipsis}"
            )
        history = "\n".join(parts) if len(parts) > 1 else ""

        task = NamedAgentTask(
            agent_name=agent_name,
            run_id=run_id,
            description=agent["description"],
            run_history=history,
            chat_id=config.telegram_chat_id,
            config=config,
            state=store,
            telegram=telegram,
        )
        task.start()
        if task._task:
            await task._task

    await store.close()


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------

async def _polling_loop(
    config: AgentConfig,
    store: StateStore,
    handler: CommandHandler,
    telegram: TelegramClient,
) -> None:
    """Inner polling coroutine — runs alongside the ask_human socket server."""
    offset: "int | None" = await store.get_offset()
    LOGGER.info("Polling for messages.")

    while True:
        try:
            updates = await telegram.get_updates(
                offset=offset, timeout=config.poll_timeout
            )
            new_offset = offset
            for update in updates:
                update_id = int(update["update_id"])
                if await store.is_update_processed(update_id):
                    LOGGER.warning("Skipping already-processed update_id=%s", update_id)
                    new_offset = update_id + 1
                    continue
                await store.mark_update_processed(update_id)
                asyncio.create_task(handler.handle_update(update))
                new_offset = update_id + 1
            if new_offset is not None and new_offset != offset:
                offset = new_offset
                await store.set_offset(new_offset)

        except asyncio.CancelledError:
            LOGGER.info("Polling loop cancelled.")
            break
        except TelegramAPIError:
            LOGGER.exception("Telegram API error — retrying in 5s")
            await asyncio.sleep(5)
        except Exception:
            LOGGER.exception("Unhandled polling error — retrying in 3s")
            await asyncio.sleep(3)


async def run_forever(config: AgentConfig) -> None:
    """Main async event loop — polls Telegram and dispatches updates."""
    ensure_dirs()

    store = StateStore(config.db_path)
    await store.init()
    await store.trim_processed_updates()

    loop = asyncio.get_running_loop()

    def _handle_sigterm() -> None:
        LOGGER.info("SIGTERM received — shutting down gracefully.")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    loop.add_signal_handler(signal.SIGTERM, _handle_sigterm)

    async with TelegramClient(
        bot_token=config.telegram_bot_token,
        default_chat_id=config.telegram_chat_id,
        request_timeout=config.request_timeout,
    ) as telegram:
        # Notify about tasks that were running when the bot last stopped
        stale_tasks = await store.get_stale_running_tasks()
        stale_runs  = await store.get_stale_running_agent_runs()
        if stale_tasks or stale_runs:
            lines = ["Bot restarted. The following tasks were interrupted and did not complete:"]
            for t in stale_tasks:
                lines.append(f"  \u2022 One-off task `{t['task_id']}`: {t['goal'][:60]}")
                await store.finish_task(t["task_id"], "Bot restarted; task did not complete.", "failed")
            for r in stale_runs:
                lines.append(f"  \u2022 Agent `{r['agent_name']}` run `{r['run_id']}`")
                await store.finish_agent_run(r["run_id"], "Bot restarted; run did not complete.", "failed")
            lines.append("Use /agent or /agent:run to retry manually.")
            try:
                await telegram.send_message("\n".join(lines))
            except Exception:
                LOGGER.exception("Failed to send stale-task notification")

        await telegram.delete_webhook(drop_pending_updates=False)
        me = await telegram.get_me()
        LOGGER.info("Bot online: @%s", me.get("result", {}).get("username", "unknown"))

        handler = CommandHandler(config=config, state=store, telegram=telegram)

        ask_human_server = await start_ask_human_server(handler, config)

        try:
            async with ask_human_server:
                await _polling_loop(config, store, handler, telegram)
        finally:
            # Clean up socket file on shutdown
            try:
                Path(config.ask_human_sock_path).unlink()
            except FileNotFoundError:
                pass

    await store.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        config = AgentConfig()
    except ConfigError as exc:
        LOGGER.error("Config error: %s", exc)
        return 1

    if "--scheduled-agent" in sys.argv:
        idx = sys.argv.index("--scheduled-agent")
        if idx + 1 < len(sys.argv):
            asyncio.run(run_scheduled_agent(sys.argv[idx + 1], config))
        return 0

    try:
        asyncio.run(run_forever(config))
    except KeyboardInterrupt:
        LOGGER.info("Interrupted.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
