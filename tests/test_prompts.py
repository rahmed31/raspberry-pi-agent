"""
tests/test_prompts.py

Tests for prompt builders — verify correct string injection
and structural integrity of generated prompts.
"""

import pytest
from agent.prompts import (
    build_chat_prompt,
    build_named_agent_prompt,
    build_one_off_prompt,
)


# ---------------------------------------------------------------------------
# build_chat_prompt
# ---------------------------------------------------------------------------

def test_chat_prompt_contains_system():
    result = build_chat_prompt([], "")
    assert "System:" in result
    assert "Assistant:" in result


def test_chat_prompt_injects_history():
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    result = build_chat_prompt(history, "")
    assert "User:\nhello" in result
    assert "Assistant:\nhi there" in result


def test_chat_prompt_injects_context_summary():
    result = build_chat_prompt([], "Server Activity Context: some data")
    assert "Server Activity Context" in result


def test_chat_prompt_no_context_when_empty():
    result = build_chat_prompt([], "")
    assert "Server Activity Context:" not in result


def test_chat_prompt_ends_with_assistant_cue():
    result = build_chat_prompt([], "")
    assert result.strip().endswith("Reply to the user's latest message directly.")


def test_chat_prompt_injects_agents_dir():
    result = build_chat_prompt([], "")
    # Should not contain literal placeholder
    assert "{agents_dir}" not in result


def test_chat_prompt_injects_incoming_dir():
    result = build_chat_prompt([], "")
    assert "{incoming_dir}" not in result


# ---------------------------------------------------------------------------
# build_one_off_prompt
# ---------------------------------------------------------------------------

def test_one_off_prompt_contains_goal():
    result = build_one_off_prompt("check disk usage", "/path/to/ask_human.py")
    assert "check disk usage" in result


def test_one_off_prompt_injects_ask_human_path():
    result = build_one_off_prompt("goal", "/path/to/ask_human.py")
    assert "/path/to/ask_human.py" in result
    assert "{ask_human_path}" not in result


def test_one_off_prompt_injects_tmp_dir():
    result = build_one_off_prompt("goal", "/path/to/ask_human.py")
    assert "{tmp_dir}" not in result


def test_one_off_prompt_injects_incoming_dir():
    result = build_one_off_prompt("goal", "/path/to/ask_human.py")
    assert "{incoming_dir}" not in result


def test_one_off_prompt_mentions_python3():
    result = build_one_off_prompt("goal", "/ask_human.py")
    assert "python3" in result
    # Should not suggest bare 'python' command
    assert "python " not in result.replace("python3", "")


def test_one_off_prompt_mentions_puppeteer():
    result = build_one_off_prompt("goal", "/ask_human.py")
    assert "puppeteer" in result.lower()


def test_one_off_prompt_mentions_credentials():
    result = build_one_off_prompt("goal", "/ask_human.py")
    assert "~/.credentials/" in result


# ---------------------------------------------------------------------------
# build_named_agent_prompt
# ---------------------------------------------------------------------------

def test_named_agent_prompt_contains_agent_name(tmp_path, monkeypatch):
    import agent.workspace as wm
    import agent.config as cfg
    monkeypatch.setattr(wm, "AGENTS_DIR", tmp_path)
    monkeypatch.setattr(cfg, "AGENTS_DIR", tmp_path)

    result = build_named_agent_prompt("job_hunter", "/ask_human.py", "")
    assert "job_hunter" in result


def test_named_agent_prompt_injects_workspace_dir(tmp_path, monkeypatch):
    import agent.workspace as wm
    import agent.config as cfg
    monkeypatch.setattr(wm, "AGENTS_DIR", tmp_path)
    monkeypatch.setattr(cfg, "AGENTS_DIR", tmp_path)

    result = build_named_agent_prompt("job_hunter", "/ask_human.py", "")
    assert "{workspace_dir}" not in result
    assert str(tmp_path) in result


def test_named_agent_prompt_injects_run_history(tmp_path, monkeypatch):
    import agent.workspace as wm
    import agent.config as cfg
    monkeypatch.setattr(wm, "AGENTS_DIR", tmp_path)
    monkeypatch.setattr(cfg, "AGENTS_DIR", tmp_path)

    result = build_named_agent_prompt("job_hunter", "/ask_human.py", "past run data here")
    assert "past run data here" in result


def test_named_agent_prompt_no_history_section_when_empty(tmp_path, monkeypatch):
    import agent.workspace as wm
    import agent.config as cfg
    monkeypatch.setattr(wm, "AGENTS_DIR", tmp_path)
    monkeypatch.setattr(cfg, "AGENTS_DIR", tmp_path)

    result = build_named_agent_prompt("job_hunter", "/ask_human.py", "")
    assert "Past run history" not in result


def test_named_agent_prompt_mentions_knowledge_base(tmp_path, monkeypatch):
    import agent.workspace as wm
    import agent.config as cfg
    monkeypatch.setattr(wm, "AGENTS_DIR", tmp_path)
    monkeypatch.setattr(cfg, "AGENTS_DIR", tmp_path)

    result = build_named_agent_prompt("job_hunter", "/ask_human.py", "")
    assert "knowledge_base.json" in result


def test_named_agent_prompt_mentions_coding_standards(tmp_path, monkeypatch):
    import agent.workspace as wm
    import agent.config as cfg
    monkeypatch.setattr(wm, "AGENTS_DIR", tmp_path)
    monkeypatch.setattr(cfg, "AGENTS_DIR", tmp_path)

    result = build_named_agent_prompt("job_hunter", "/ask_human.py", "")
    assert "tools/" in result
    assert "README.md" in result
