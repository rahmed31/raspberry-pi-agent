"""
tests/test_state.py

Tests for StateStore — all database operations, ID generation, edge cases.
Uses an in-memory SQLite database so no disk I/O and no cleanup needed.
"""

import asyncio
import pytest
import pytest_asyncio

from agent.state import StateStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def store():
    """Fresh in-memory StateStore for each test."""
    s = StateStore(":memory:")
    await s.init()
    yield s
    await s.close()


# ---------------------------------------------------------------------------
# KV / offset
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_offset_none_initially(store):
    assert await store.get_offset() is None


@pytest.mark.asyncio
async def test_offset_set_and_get(store):
    await store.set_offset(42)
    assert await store.get_offset() == 42


@pytest.mark.asyncio
async def test_offset_overwrite(store):
    await store.set_offset(1)
    await store.set_offset(999)
    assert await store.get_offset() == 999


# ---------------------------------------------------------------------------
# Chat messages
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_append_and_get_history(store):
    await store.append_message(1, "user", "hello")
    await store.append_message(1, "assistant", "hi there")
    history = await store.get_history(1, limit=10)
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "hello"
    assert history[1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_history_limit(store):
    for i in range(10):
        await store.append_message(1, "user", f"msg {i}")
    history = await store.get_history(1, limit=3)
    assert len(history) == 3


@pytest.mark.asyncio
async def test_history_isolated_by_chat_id(store):
    await store.append_message(1, "user", "chat 1")
    await store.append_message(2, "user", "chat 2")
    h1 = await store.get_history(1, limit=10)
    h2 = await store.get_history(2, limit=10)
    assert len(h1) == 1
    assert len(h2) == 1
    assert h1[0]["content"] == "chat 1"
    assert h2[0]["content"] == "chat 2"


@pytest.mark.asyncio
async def test_reset_chat(store):
    await store.append_message(1, "user", "hello")
    await store.reset_chat(1)
    history = await store.get_history(1, limit=10)
    assert len(history) == 0


@pytest.mark.asyncio
async def test_count_messages(store):
    assert await store.count_messages(1) == 0
    await store.append_message(1, "user", "hello")
    await store.append_message(1, "assistant", "hi")
    assert await store.count_messages(1) == 2


# ---------------------------------------------------------------------------
# One-off tasks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_task_id_unique(store):
    ids = set()
    for _ in range(20):
        tid = await store.generate_task_id()
        assert tid not in ids
        ids.add(tid)
        await store.create_task(tid, f"goal {tid}")


@pytest.mark.asyncio
async def test_task_id_length(store):
    tid = await store.generate_task_id()
    assert len(tid) == 4
    assert tid.isalnum()


@pytest.mark.asyncio
async def test_create_and_get_task(store):
    await store.create_task("abcd", "test goal")
    task = await store.get_task("abcd")
    assert task is not None
    assert task["goal"] == "test goal"
    assert task["status"] == "running"


@pytest.mark.asyncio
async def test_finish_task_completed(store):
    await store.create_task("abcd", "test goal")
    await store.finish_task("abcd", "done result", "completed")
    task = await store.get_task("abcd")
    assert task["status"] == "completed"
    assert task["result_full"] == "done result"
    assert task["finished_at"] is not None


@pytest.mark.asyncio
async def test_finish_task_preview_truncation(store):
    await store.create_task("abcd", "goal")
    long_result = "x" * 500
    await store.finish_task("abcd", long_result, "completed")
    task = await store.get_task("abcd")
    assert len(task["result_preview"]) <= 303  # 300 + "..."
    assert task["result_full"] == long_result


@pytest.mark.asyncio
async def test_finish_task_timeout_status(store):
    await store.create_task("abcd", "goal")
    await store.finish_task("abcd", "partial", "timeout")
    task = await store.get_task("abcd")
    assert task["status"] == "timeout"


@pytest.mark.asyncio
async def test_get_task_not_found(store):
    result = await store.get_task("xxxx")
    assert result is None


@pytest.mark.asyncio
async def test_delete_task(store):
    await store.create_task("abcd", "goal")
    await store.delete_task("abcd")
    assert await store.get_task("abcd") is None


@pytest.mark.asyncio
async def test_delete_all_tasks(store):
    await store.create_task("aa11", "goal 1")
    await store.create_task("bb22", "goal 2")
    count = await store.delete_all_tasks()
    assert count == 2
    tasks = await store.get_all_tasks()
    assert len(tasks) == 0


@pytest.mark.asyncio
async def test_get_all_tasks_ordered_by_date(store):
    await store.create_task("aa11", "first")
    await store.create_task("bb22", "second")
    tasks = await store.get_all_tasks()
    assert tasks[0]["task_id"] == "bb22"  # most recent first


# ---------------------------------------------------------------------------
# Named agents
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_and_get_named_agent(store):
    await store.create_named_agent("job_hunter", "find jobs")
    agent = await store.get_named_agent("job_hunter")
    assert agent is not None
    assert agent["description"] == "find jobs"
    assert agent["last_run_at"] is None


@pytest.mark.asyncio
async def test_get_named_agent_not_found(store):
    assert await store.get_named_agent("nonexistent") is None


@pytest.mark.asyncio
async def test_get_all_named_agents(store):
    await store.create_named_agent("agent_a", "desc a")
    await store.create_named_agent("agent_b", "desc b")
    agents = await store.get_all_named_agents()
    assert len(agents) == 2


@pytest.mark.asyncio
async def test_update_named_agent_schedule(store):
    await store.create_named_agent("job_hunter", "find jobs")
    await store.update_named_agent_schedule("job_hunter", "daily 9am", "claude_agent_job_hunter")
    agent = await store.get_named_agent("job_hunter")
    assert agent["cron_schedule"] == "daily 9am"
    assert agent["cron_job_id"] == "claude_agent_job_hunter"


@pytest.mark.asyncio
async def test_delete_named_agent_cascades_runs(store):
    await store.create_named_agent("job_hunter", "find jobs")
    run_id = await store.generate_run_id()
    await store.create_agent_run(run_id, "job_hunter")
    await store.delete_named_agent("job_hunter")
    assert await store.get_named_agent("job_hunter") is None
    runs = await store.get_agent_runs("job_hunter")
    assert len(runs) == 0


# ---------------------------------------------------------------------------
# Named agent runs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_run_id_unique(store):
    await store.create_named_agent("test_agent", "test")
    ids = set()
    for _ in range(20):
        rid = await store.generate_run_id()
        assert rid not in ids
        ids.add(rid)
        await store.create_agent_run(rid, "test_agent")


@pytest.mark.asyncio
async def test_run_id_length(store):
    rid = await store.generate_run_id()
    assert len(rid) == 6
    assert rid.isalnum()


@pytest.mark.asyncio
async def test_create_and_finish_agent_run(store):
    await store.create_named_agent("job_hunter", "find jobs")
    run_id = await store.generate_run_id()
    await store.create_agent_run(run_id, "job_hunter")

    run = await store.get_agent_run(run_id)
    assert run["status"] == "running"
    assert run["finished_at"] is None

    await store.finish_agent_run(run_id, "found 3 jobs", "completed")
    run = await store.get_agent_run(run_id)
    assert run["status"] == "completed"
    assert run["result"] == "found 3 jobs"
    assert run["finished_at"] is not None


@pytest.mark.asyncio
async def test_create_run_updates_last_run_at(store):
    await store.create_named_agent("job_hunter", "find jobs")
    agent = await store.get_named_agent("job_hunter")
    assert agent["last_run_at"] is None

    run_id = await store.generate_run_id()
    await store.create_agent_run(run_id, "job_hunter")
    agent = await store.get_named_agent("job_hunter")
    assert agent["last_run_at"] is not None


@pytest.mark.asyncio
async def test_count_agent_runs(store):
    await store.create_named_agent("job_hunter", "find jobs")
    assert await store.count_agent_runs("job_hunter") == 0

    for _ in range(3):
        rid = await store.generate_run_id()
        await store.create_agent_run(rid, "job_hunter")

    assert await store.count_agent_runs("job_hunter") == 3


@pytest.mark.asyncio
async def test_delete_agent_runs(store):
    await store.create_named_agent("job_hunter", "find jobs")
    for _ in range(3):
        rid = await store.generate_run_id()
        await store.create_agent_run(rid, "job_hunter")

    count = await store.delete_agent_runs("job_hunter")
    assert count == 3
    assert await store.count_agent_runs("job_hunter") == 0


@pytest.mark.asyncio
async def test_clear_all(store):
    await store.create_task("abcd", "goal")
    await store.create_named_agent("agent_a", "desc")
    rid = await store.generate_run_id()
    await store.create_agent_run(rid, "agent_a")
    await store.append_message(1, "user", "hello")

    counts = await store.clear_all()
    assert counts["tasks"] == 1
    assert counts["runs"] == 1
    assert counts["messages"] == 1

    assert len(await store.get_all_tasks()) == 0
    assert len(await store.get_agent_runs("agent_a")) == 0
    assert len(await store.get_history(1, 10)) == 0
    # Named agents themselves should NOT be deleted
    assert await store.get_named_agent("agent_a") is not None
