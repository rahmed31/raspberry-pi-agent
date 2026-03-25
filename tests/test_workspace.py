"""
tests/test_workspace.py

Tests for workspace helpers — creation, tree generation,
knowledge base scaffolding, backup, and validation.
"""

import json
import pytest
from pathlib import Path

from agent.workspace import (
    KNOWLEDGE_BASE_SCAFFOLD,
    REQUIRED_KB_KEYS,
    agent_workspace,
    backup_knowledge_base,
    validate_knowledge_base,
    validate_knowledge_base_post_run,
    workspace_tree,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ws(tmp_path, monkeypatch):
    """
    Patch AGENTS_DIR to a temp directory so tests don't touch the real filesystem.
    Returns the temp agents dir.
    """
    import agent.workspace as wm
    import agent.config as cfg

    monkeypatch.setattr(wm, "AGENTS_DIR", tmp_path)
    monkeypatch.setattr(cfg, "AGENTS_DIR", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# agent_workspace
# ---------------------------------------------------------------------------

def test_agent_workspace_creates_dirs(ws):
    path = agent_workspace("test_agent")
    assert (path / "tools").is_dir()
    assert (path / "data" / "raw").is_dir()
    assert (path / "data" / "processed").is_dir()
    assert (path / "outputs").is_dir()
    assert (path / "screenshots").is_dir()


def test_agent_workspace_creates_readme(ws):
    path = agent_workspace("test_agent")
    readme = path / "README.md"
    assert readme.exists()
    assert "test_agent" in readme.read_text()


def test_agent_workspace_creates_knowledge_base(ws):
    path = agent_workspace("test_agent")
    kb_path = path / "knowledge_base.json"
    assert kb_path.exists()
    kb = json.loads(kb_path.read_text())
    assert kb["agent_name"] == "test_agent"
    assert REQUIRED_KB_KEYS.issubset(kb.keys())


def test_agent_workspace_creates_insights(ws):
    path = agent_workspace("test_agent")
    assert (path / "insights.md").exists()


def test_agent_workspace_idempotent(ws):
    """Calling twice should not overwrite existing files."""
    path = agent_workspace("test_agent")
    kb_path = path / "knowledge_base.json"

    # Modify knowledge base
    kb = json.loads(kb_path.read_text())
    kb["purpose"] = "test purpose"
    kb_path.write_text(json.dumps(kb))

    # Call again — should not overwrite
    agent_workspace("test_agent")
    kb2 = json.loads(kb_path.read_text())
    assert kb2["purpose"] == "test purpose"


def test_knowledge_base_scaffold_has_required_keys():
    assert REQUIRED_KB_KEYS.issubset(KNOWLEDGE_BASE_SCAFFOLD.keys())


# ---------------------------------------------------------------------------
# workspace_tree
# ---------------------------------------------------------------------------

def test_workspace_tree_nonexistent(ws):
    result = workspace_tree("nonexistent_agent")
    assert result == "(workspace not yet created)"


def test_workspace_tree_empty(ws):
    path = agent_workspace("test_agent")
    # Remove all files to test empty
    for f in path.rglob("*"):
        if f.is_file():
            f.unlink()
    result = workspace_tree("test_agent")
    # Should have directories listed
    assert isinstance(result, str)


def test_workspace_tree_shows_files(ws):
    path = agent_workspace("test_agent")
    (path / "tools" / "browser.js").write_text("// browser tool")
    result = workspace_tree("test_agent")
    assert "browser.js" in result


# ---------------------------------------------------------------------------
# backup_knowledge_base
# ---------------------------------------------------------------------------

def test_backup_creates_backup_file(ws):
    path = agent_workspace("test_agent")
    backup_knowledge_base("test_agent")
    assert (path / "knowledge_base.backup.json").exists()


def test_backup_copies_content(ws):
    path = agent_workspace("test_agent")
    kb_path = path / "knowledge_base.json"
    kb = json.loads(kb_path.read_text())
    kb["purpose"] = "test purpose"
    kb_path.write_text(json.dumps(kb))

    backup_knowledge_base("test_agent")

    backup = json.loads((path / "knowledge_base.backup.json").read_text())
    assert backup["purpose"] == "test purpose"


def test_backup_does_not_overwrite_good_backup_with_bad_kb(ws):
    path = agent_workspace("test_agent")

    # Create a good backup
    good_backup = {"agent_name": "test_agent", "purpose": "good backup"}
    (path / "knowledge_base.backup.json").write_text(json.dumps(good_backup))

    # Corrupt the main knowledge base
    (path / "knowledge_base.json").write_text("not valid json {{{")

    # Backup should NOT overwrite the good backup with invalid content
    backup_knowledge_base("test_agent")
    backup = json.loads((path / "knowledge_base.backup.json").read_text())
    assert backup["purpose"] == "good backup"


# ---------------------------------------------------------------------------
# validate_knowledge_base
# ---------------------------------------------------------------------------

def test_validate_valid_kb(ws):
    agent_workspace("test_agent")
    ok, warning = validate_knowledge_base("test_agent")
    assert ok is True
    assert warning is None


def test_validate_corrupt_kb_restores_from_backup(ws):
    path = agent_workspace("test_agent")

    # Create valid backup
    kb = json.loads((path / "knowledge_base.json").read_text())
    (path / "knowledge_base.backup.json").write_text(json.dumps(kb))

    # Corrupt main file
    (path / "knowledge_base.json").write_text("not json")

    ok, warning = validate_knowledge_base("test_agent")
    assert ok is True
    assert warning is not None
    assert "restored from backup" in warning

    # Main file should now be valid JSON
    restored = json.loads((path / "knowledge_base.json").read_text())
    assert REQUIRED_KB_KEYS.issubset(restored.keys())


def test_validate_both_corrupt_recreates_scaffold(ws):
    path = agent_workspace("test_agent")
    (path / "knowledge_base.json").write_text("bad json")
    (path / "knowledge_base.backup.json").write_text("also bad")

    ok, warning = validate_knowledge_base("test_agent")
    assert ok is True
    assert warning is not None
    assert "reset to empty scaffold" in warning

    # Should now have a valid scaffold
    kb = json.loads((path / "knowledge_base.json").read_text())
    assert REQUIRED_KB_KEYS.issubset(kb.keys())


# ---------------------------------------------------------------------------
# validate_knowledge_base_post_run
# ---------------------------------------------------------------------------

def test_post_run_valid_update(ws):
    path = agent_workspace("test_agent")
    kb   = json.loads((path / "knowledge_base.json").read_text())
    kb["run_count"] = 1
    kb["run_log"]   = [{"run_id": "abc123", "timestamp": "2026-01-01", "outcome": "done", "key_learnings": []}]
    (path / "knowledge_base.json").write_text(json.dumps(kb))

    warning = validate_knowledge_base_post_run("test_agent", pre_run_count=0)
    assert warning is None


def test_post_run_run_count_not_incremented(ws):
    path = agent_workspace("test_agent")
    kb   = json.loads((path / "knowledge_base.json").read_text())
    kb["run_count"] = 0  # same as before
    kb["run_log"]   = [{"run_id": "abc123", "timestamp": "2026-01-01", "outcome": "done", "key_learnings": []}]
    (path / "knowledge_base.json").write_text(json.dumps(kb))

    warning = validate_knowledge_base_post_run("test_agent", pre_run_count=0)
    assert warning is not None
    assert "run_count" in warning


def test_post_run_empty_run_log(ws):
    path = agent_workspace("test_agent")
    kb   = json.loads((path / "knowledge_base.json").read_text())
    kb["run_count"] = 1
    kb["run_log"]   = []
    (path / "knowledge_base.json").write_text(json.dumps(kb))

    warning = validate_knowledge_base_post_run("test_agent", pre_run_count=0)
    assert warning is not None
    assert "run_log" in warning


def test_post_run_invalid_json_restores_backup(ws):
    path = agent_workspace("test_agent")

    # Create a valid backup
    kb = json.loads((path / "knowledge_base.json").read_text())
    (path / "knowledge_base.backup.json").write_text(json.dumps(kb))

    # Claude writes invalid JSON
    (path / "knowledge_base.json").write_text("invalid {{{")

    warning = validate_knowledge_base_post_run("test_agent", pre_run_count=0)
    assert warning is not None
    assert "invalid JSON" in warning

    # Main file should be restored from backup
    restored = json.loads((path / "knowledge_base.json").read_text())
    assert REQUIRED_KB_KEYS.issubset(restored.keys())
