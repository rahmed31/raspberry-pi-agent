"""
agent/workspace.py

Named agent workspace management.
Handles directory creation, file tree generation,
knowledge base scaffolding, backup, and validation.
"""

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from agent.config import AGENTS_DIR

# ---------------------------------------------------------------------------
# Knowledge base scaffold
# ---------------------------------------------------------------------------

KNOWLEDGE_BASE_SCAFFOLD = {
    "agent_name": "",
    "purpose": "",
    "last_updated": "",
    "run_count": 0,
    "knowledge": {
        "key_findings": [],
        "patterns_observed": [],
        "successful_approaches": [],
        "failed_approaches": [],
        "open_questions": [],
    },
    "state": {
        "last_action": "",
        "current_status": "",
        "next_steps": [],
    },
    "resources": {
        "tools_built": [],
        "data_files": [],
        "external_services_used": [],
    },
    "run_log": [],
}

REQUIRED_KB_KEYS = {
    "agent_name", "purpose", "last_updated", "run_count",
    "knowledge", "state", "resources", "run_log",
}


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------

def ensure_dirs() -> None:
    """Create all required top-level directories."""
    from agent.config import TMP_DIR, INCOMING_DIR, DATA_DIR
    for d in [TMP_DIR, AGENTS_DIR, INCOMING_DIR, DATA_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def agent_workspace(agent_name: str) -> Path:
    """
    Ensure a named agent's workspace exists with the correct structure.
    Creates subdirectories, README.md, knowledge_base.json, and insights.md
    on first call. Safe to call on every run.
    """
    ws = AGENTS_DIR / agent_name

    for sub in ["tools", "data/raw", "data/processed", "outputs", "screenshots"]:
        (ws / sub).mkdir(parents=True, exist_ok=True)

    if not (ws / "README.md").exists():
        (ws / "README.md").write_text(
            f"# Agent: {agent_name}\n\n"
            f"## Purpose\n(agent will document its purpose here)\n\n"
            f"## Tools\n(agent will document tools here)\n\n"
            f"## Run History\n(agent will update this after each run)\n"
        )

    kb_path = ws / "knowledge_base.json"
    if not kb_path.exists():
        scaffold = dict(KNOWLEDGE_BASE_SCAFFOLD)
        scaffold["agent_name"] = agent_name
        kb_path.write_text(json.dumps(scaffold, indent=2))

    if not (ws / "insights.md").exists():
        (ws / "insights.md").write_text(
            f"# Insights: {agent_name}\n\n"
            f"(agent appends new insights here after each run)\n"
        )

    return ws


def workspace_tree(agent_name: str) -> str:
    """
    Build a formatted file tree string for a named agent workspace.
    Includes file sizes and last-modified timestamps.
    """
    ws = AGENTS_DIR / agent_name
    if not ws.exists():
        return "(workspace not yet created)"

    lines = []
    for path in sorted(ws.rglob("*")):
        if path.name == "__pycache__" or ".pyc" in path.name:
            continue
        rel    = path.relative_to(ws)
        depth  = len(rel.parts) - 1
        indent = "  " * depth
        if path.is_dir():
            lines.append(f"{indent}{rel.name}/")
        else:
            try:
                size  = path.stat().st_size
                mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                if size < 1024:
                    size_str = f"{size}B"
                elif size < 1024 * 1024:
                    size_str = f"{size // 1024}KB"
                else:
                    size_str = f"{size // (1024 * 1024)}MB"
                lines.append(f"{indent}{rel.name} ({size_str}, {mtime})")
            except Exception:
                lines.append(f"{indent}{rel.name}")

    return "\n".join(lines) if lines else "(empty workspace)"


def wipe_tmp() -> None:
    """Delete all contents of tmp/ but keep the directory itself."""
    from agent.config import TMP_DIR
    if TMP_DIR.exists():
        for item in TMP_DIR.iterdir():
            try:
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Knowledge base helpers
# ---------------------------------------------------------------------------

def backup_knowledge_base(agent_name: str) -> None:
    """
    Copy knowledge_base.json → knowledge_base.backup.json before each run.
    Only backs up if the current file is valid JSON.
    """
    kb_path     = AGENTS_DIR / agent_name / "knowledge_base.json"
    backup_path = AGENTS_DIR / agent_name / "knowledge_base.backup.json"

    if not kb_path.exists():
        return

    try:
        data = json.loads(kb_path.read_text())
        if isinstance(data, dict):
            backup_path.write_text(json.dumps(data, indent=2))
    except Exception:
        # Don't overwrite a good backup with a bad file
        pass


def validate_knowledge_base(agent_name: str) -> Tuple[bool, Optional[str]]:
    """
    Validate knowledge_base.json before a run.
    Attempts to restore from backup if corrupted.
    Recreates from scaffold if both are bad.

    Returns:
        (ok: bool, warning_message: Optional[str])
    """
    ws          = AGENTS_DIR / agent_name
    kb_path     = ws / "knowledge_base.json"
    backup_path = ws / "knowledge_base.backup.json"

    def _is_valid(path: Path) -> bool:
        try:
            data = json.loads(path.read_text())
            return isinstance(data, dict) and REQUIRED_KB_KEYS.issubset(data.keys())
        except Exception:
            return False

    if kb_path.exists() and _is_valid(kb_path):
        return True, None

    # Try backup
    if backup_path.exists() and _is_valid(backup_path):
        shutil.copy(backup_path, kb_path)
        return True, (
            f"Knowledge base for `{agent_name}` was corrupted — "
            f"restored from backup successfully."
        )

    # Recreate from scaffold
    scaffold = dict(KNOWLEDGE_BASE_SCAFFOLD)
    scaffold["agent_name"] = agent_name
    kb_path.write_text(json.dumps(scaffold, indent=2))
    return True, (
        f"Knowledge base for `{agent_name}` and its backup were both corrupted — "
        f"reset to empty scaffold. Previous knowledge has been lost."
    )


def validate_knowledge_base_post_run(
    agent_name: str, pre_run_count: int
) -> Optional[str]:
    """
    Validate knowledge_base.json after a run.
    Checks structure integrity and that Claude updated it correctly.
    If invalid, restores from backup and warns.

    Returns warning message if there was a problem, None if all good.
    """
    ws          = AGENTS_DIR / agent_name
    kb_path     = ws / "knowledge_base.json"
    backup_path = ws / "knowledge_base.backup.json"

    try:
        data = json.loads(kb_path.read_text())
    except Exception:
        if backup_path.exists():
            shutil.copy(backup_path, kb_path)
            return (
                f"⚠️ Agent `{agent_name}` wrote invalid JSON to knowledge base — "
                f"restored from pre-run backup."
            )
        return (
            f"⚠️ Agent `{agent_name}` wrote invalid JSON to knowledge base "
            f"and no backup was available."
        )

    # Check required keys still present
    if not REQUIRED_KB_KEYS.issubset(data.keys()):
        if backup_path.exists():
            shutil.copy(backup_path, kb_path)
            return (
                f"⚠️ Agent `{agent_name}` removed required keys from knowledge base — "
                f"restored from pre-run backup."
            )

    # Check run_count incremented
    new_count = data.get("run_count", 0)
    if new_count <= pre_run_count:
        return (
            f"⚠️ Agent `{agent_name}` did not increment run_count in knowledge base "
            f"(expected >{pre_run_count}, got {new_count}). Knowledge base may not have been updated."
        )

    # Check run_log has a new entry
    run_log = data.get("run_log", [])
    if len(run_log) == 0:
        return (
            f"⚠️ Agent `{agent_name}` did not append to run_log in knowledge base."
        )

    return None
