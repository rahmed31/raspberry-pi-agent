"""
agent/state.py

Async SQLite state store using aiosqlite.
Manages all persistent data: chat history, one-off tasks,
named agents and their run history.
"""

import random
import string
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

from agent.config import ONE_OFF_RESULT_PREVIEW_CHARS, AGENTS_DIR
from agent.workspace import workspace_tree


class StateStore:
    """
    Async SQLite wrapper for all agent state.

    Usage:
        store = StateStore("data/agent_state.db")
        await store.init()
        ...
        await store.close()
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        """Open connection and initialise schema."""
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._init_schema()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("StateStore not initialised — call await store.init() first")
        return self._db

    # ------------------------------------------------------------------ #
    #  Schema                                                              #
    # ------------------------------------------------------------------ #

    async def _init_schema(self) -> None:
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS kv (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chat_messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER NOT NULL,
                role       TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content    TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS one_off_tasks (
                task_id        TEXT PRIMARY KEY,
                goal           TEXT NOT NULL,
                result_preview TEXT,
                result_full    TEXT,
                status         TEXT NOT NULL DEFAULT 'running',
                started_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at    TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS named_agents (
                name          TEXT PRIMARY KEY,
                description   TEXT NOT NULL,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_run_at   TIMESTAMP,
                cron_schedule TEXT,
                cron_job_id   TEXT
            );

            CREATE TABLE IF NOT EXISTS named_agent_runs (
                run_id      TEXT PRIMARY KEY,
                agent_name  TEXT NOT NULL,
                result      TEXT,
                status      TEXT NOT NULL DEFAULT 'running',
                started_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP,
                FOREIGN KEY (agent_name) REFERENCES named_agents(name)
            );

            CREATE TABLE IF NOT EXISTS processed_updates (
                update_id    INTEGER PRIMARY KEY,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await self.db.commit()

    # ------------------------------------------------------------------ #
    #  KV / offset                                                         #
    # ------------------------------------------------------------------ #

    async def get_offset(self) -> Optional[int]:
        async with self.db.execute(
            "SELECT value FROM kv WHERE key = 'telegram_offset'"
        ) as cur:
            row = await cur.fetchone()
        return int(row["value"]) if row else None

    async def set_offset(self, offset: int) -> None:
        await self.db.execute(
            """
            INSERT INTO kv (key, value) VALUES ('telegram_offset', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(offset),),
        )
        await self.db.commit()

    # ------------------------------------------------------------------ #
    #  Processed updates (deduplication)                                  #
    # ------------------------------------------------------------------ #

    async def is_update_processed(self, update_id: int) -> bool:
        async with self.db.execute(
            "SELECT 1 FROM processed_updates WHERE update_id = ?", (update_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def mark_update_processed(self, update_id: int) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO processed_updates (update_id) VALUES (?)", (update_id,)
        )
        await self.db.commit()

    async def trim_processed_updates(self, max_age_days: int = 7) -> None:
        await self.db.execute(
            "DELETE FROM processed_updates WHERE processed_at < datetime('now', ?)",
            (f"-{max_age_days} days",),
        )
        await self.db.commit()

    # ------------------------------------------------------------------ #
    #  Stale task detection (for startup notification)                    #
    # ------------------------------------------------------------------ #

    async def get_stale_running_tasks(self) -> List[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT * FROM one_off_tasks WHERE status = 'running' ORDER BY started_at"
        ) as cur:
            return await cur.fetchall()

    async def get_stale_running_agent_runs(self) -> List[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT * FROM named_agent_runs WHERE status = 'running' ORDER BY started_at"
        ) as cur:
            return await cur.fetchall()

    # ------------------------------------------------------------------ #
    #  Chat messages                                                       #
    # ------------------------------------------------------------------ #

    async def append_message(self, chat_id: int, role: str, content: str) -> None:
        await self.db.execute(
            "INSERT INTO chat_messages (chat_id, role, content) VALUES (?, ?, ?)",
            (chat_id, role, content),
        )
        await self.db.commit()

    async def get_history(self, chat_id: int, limit: int) -> List[Dict[str, str]]:
        async with self.db.execute(
            """
            SELECT role, content FROM chat_messages
            WHERE chat_id = ? ORDER BY id DESC LIMIT ?
            """,
            (chat_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    async def reset_chat(self, chat_id: int) -> None:
        await self.db.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
        await self.db.commit()

    async def count_messages(self, chat_id: int) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) AS c FROM chat_messages WHERE chat_id = ?", (chat_id,)
        ) as cur:
            row = await cur.fetchone()
        return int(row["c"])

    # ------------------------------------------------------------------ #
    #  One-off tasks                                                       #
    # ------------------------------------------------------------------ #

    async def generate_task_id(self) -> str:
        async with self.db.execute("SELECT task_id FROM one_off_tasks") as cur:
            existing = {r[0] for r in await cur.fetchall()}
        while True:
            tid = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
            if tid not in existing:
                return tid

    async def create_task(self, task_id: str, goal: str) -> None:
        await self.db.execute(
            "INSERT INTO one_off_tasks (task_id, goal, status) VALUES (?, ?, 'running')",
            (task_id, goal),
        )
        await self.db.commit()

    async def finish_task(self, task_id: str, result: str, status: str) -> None:
        preview = result[:ONE_OFF_RESULT_PREVIEW_CHARS]
        if len(result) > ONE_OFF_RESULT_PREVIEW_CHARS:
            preview += "..."
        await self.db.execute(
            """
            UPDATE one_off_tasks
            SET result_preview = ?, result_full = ?, status = ?, finished_at = CURRENT_TIMESTAMP
            WHERE task_id = ?
            """,
            (preview, result, status, task_id),
        )
        await self.db.commit()

    async def get_task(self, task_id: str) -> Optional[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT * FROM one_off_tasks WHERE task_id = ?", (task_id,)
        ) as cur:
            return await cur.fetchone()

    async def get_all_tasks(self, limit: int = 50) -> List[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT * FROM one_off_tasks ORDER BY rowid DESC LIMIT ?", (limit,)
        ) as cur:
            return await cur.fetchall()

    async def delete_task(self, task_id: str) -> None:
        await self.db.execute("DELETE FROM one_off_tasks WHERE task_id = ?", (task_id,))
        await self.db.commit()

    async def delete_all_tasks(self) -> int:
        async with self.db.execute("SELECT COUNT(*) FROM one_off_tasks") as cur:
            row = await cur.fetchone()
            count = row[0]
        await self.db.execute("DELETE FROM one_off_tasks")
        await self.db.commit()
        return count

    # ------------------------------------------------------------------ #
    #  Named agents                                                        #
    # ------------------------------------------------------------------ #

    async def create_named_agent(self, name: str, description: str) -> None:
        await self.db.execute(
            "INSERT INTO named_agents (name, description) VALUES (?, ?)",
            (name, description),
        )
        await self.db.commit()

    async def get_named_agent(self, name: str) -> Optional[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT * FROM named_agents WHERE name = ?", (name,)
        ) as cur:
            return await cur.fetchone()

    async def get_all_named_agents(self) -> List[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT * FROM named_agents ORDER BY created_at DESC"
        ) as cur:
            return await cur.fetchall()

    async def update_named_agent_schedule(
        self, name: str, cron_schedule: Optional[str], cron_job_id: Optional[str]
    ) -> None:
        await self.db.execute(
            "UPDATE named_agents SET cron_schedule = ?, cron_job_id = ? WHERE name = ?",
            (cron_schedule, cron_job_id, name),
        )
        await self.db.commit()

    async def delete_named_agent(self, name: str) -> None:
        await self.db.execute("DELETE FROM named_agent_runs WHERE agent_name = ?", (name,))
        await self.db.execute("DELETE FROM named_agents WHERE name = ?", (name,))
        await self.db.commit()

    # ------------------------------------------------------------------ #
    #  Named agent runs                                                    #
    # ------------------------------------------------------------------ #

    async def generate_run_id(self) -> str:
        async with self.db.execute("SELECT run_id FROM named_agent_runs") as cur:
            existing = {r[0] for r in await cur.fetchall()}
        while True:
            rid = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
            if rid not in existing:
                return rid

    async def create_agent_run(self, run_id: str, agent_name: str) -> None:
        await self.db.execute(
            "INSERT INTO named_agent_runs (run_id, agent_name, status) VALUES (?, ?, 'running')",
            (run_id, agent_name),
        )
        await self.db.execute(
            "UPDATE named_agents SET last_run_at = CURRENT_TIMESTAMP WHERE name = ?",
            (agent_name,),
        )
        await self.db.commit()

    async def finish_agent_run(self, run_id: str, result: str, status: str) -> None:
        await self.db.execute(
            """
            UPDATE named_agent_runs
            SET result = ?, status = ?, finished_at = CURRENT_TIMESTAMP
            WHERE run_id = ?
            """,
            (result, status, run_id),
        )
        await self.db.commit()

    async def get_agent_runs(self, agent_name: str) -> List[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT * FROM named_agent_runs WHERE agent_name = ? ORDER BY started_at DESC",
            (agent_name,),
        ) as cur:
            return await cur.fetchall()

    async def get_agent_run(self, run_id: str) -> Optional[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT * FROM named_agent_runs WHERE run_id = ?", (run_id,)
        ) as cur:
            return await cur.fetchone()

    async def delete_agent_runs(self, agent_name: str) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) FROM named_agent_runs WHERE agent_name = ?", (agent_name,)
        ) as cur:
            row = await cur.fetchone()
            count = row[0]
        await self.db.execute("DELETE FROM named_agent_runs WHERE agent_name = ?", (agent_name,))
        await self.db.commit()
        return count

    async def delete_agent_run(self, run_id: str) -> None:
        await self.db.execute("DELETE FROM named_agent_runs WHERE run_id = ?", (run_id,))
        await self.db.commit()

    async def clear_all(self) -> Dict[str, int]:
        async with self.db.execute("SELECT COUNT(*) FROM one_off_tasks") as cur:
            tasks = (await cur.fetchone())[0]
        async with self.db.execute("SELECT COUNT(*) FROM named_agent_runs") as cur:
            runs = (await cur.fetchone())[0]
        async with self.db.execute("SELECT COUNT(*) FROM chat_messages") as cur:
            msgs = (await cur.fetchone())[0]
        await self.db.executescript("""
            DELETE FROM one_off_tasks;
            DELETE FROM named_agent_runs;
            DELETE FROM chat_messages;
        """)
        await self.db.commit()
        return {"tasks": tasks, "runs": runs, "messages": msgs}

    async def get_db_size(self) -> str:
        try:
            size = Path(self.db_path).stat().st_size
            if size < 1024:
                return f"{size} B"
            elif size < 1024 * 1024:
                return f"{size / 1024:.1f} KB"
            else:
                return f"{size / (1024 * 1024):.1f} MB"
        except Exception:
            return "unknown"

    async def count_agent_runs(self, agent_name: str) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) FROM named_agent_runs WHERE agent_name = ?", (agent_name,)
        ) as cur:
            row = await cur.fetchone()
        return row[0]

    # ------------------------------------------------------------------ #
    #  Chat context summary                                                #
    # ------------------------------------------------------------------ #

    async def build_agent_context_summary(self) -> str:
        """
        Build a summary of all agent activity injected into every chat prompt.
        Includes task previews from SQLite and workspace file trees for named agents.
        """
        parts = []

        tasks = await self.get_all_tasks(limit=20)
        if tasks:
            parts.append("=== Recent One-Off Tasks ===")
            for t in tasks:
                icon    = "✓" if t["status"] == "completed" else "✗" if t["status"] == "failed" else "~"
                preview = t["result_preview"] or ""
                parts.append(
                    f"[{icon}] ID:{t['task_id']} | {t['started_at'][:16]} | {t['goal'][:80]}\n"
                    f"  Preview: {preview}"
                )

        agents = await self.get_all_named_agents()
        if agents:
            parts.append("\n=== Named Agents ===")
            for a in agents:
                run_count = await self.count_agent_runs(a["name"])
                last_run  = a["last_run_at"][:16] if a["last_run_at"] else "never"
                schedule  = f" | schedule: {a['cron_schedule']}" if a["cron_schedule"] else ""
                parts.append(
                    f"Agent: {a['name']} | runs: {run_count} | last: {last_run}{schedule}\n"
                    f"  Purpose: {a['description']}"
                )

                runs = await self.get_agent_runs(a["name"])
                for r in runs[:5]:
                    icon    = "✓" if r["status"] == "completed" else "✗" if r["status"] == "failed" else "~"
                    snippet = (r["result"] or "")[:200]
                    ellipsis = "..." if r["result"] and len(r["result"]) > 200 else ""
                    parts.append(
                        f"  [{icon}] run:{r['run_id']} | {r['started_at'][:16]}\n"
                        f"    {snippet}{ellipsis}"
                    )

                tree = workspace_tree(a["name"])
                if tree and tree != "(workspace not yet created)":
                    parts.append(f"  Workspace files:\n{tree}")

        return "\n".join(parts) if parts else ""
