import asyncio

import pytest

from services.refresh_manager import RefreshManager


@pytest.fixture(autouse=True)
def _reset_global_active():
    # The global single-flight slot is class-level; reset it around each test.
    RefreshManager._global_active = None
    yield
    RefreshManager._global_active = None


@pytest.mark.asyncio
async def test_only_one_crawl_runs_globally():
    started = {"a": asyncio.Event()}
    release = asyncio.Event()
    a_calls, b_calls = [], []

    async def slow_a():
        a_calls.append(1)
        started["a"].set()
        await release.wait()
        return {"ok": True}

    async def sync_b():
        b_calls.append(1)
        return {"ok": True}

    a = RefreshManager("a", slow_a, cooldown_seconds=0)
    b = RefreshManager("b", sync_b, cooldown_seconds=0)

    assert a.trigger_if_needed(stale=True) is True
    await started["a"].wait()
    # b must NOT start while a's crawl is running (global single-flight)
    assert b.trigger_if_needed(stale=True) is False
    assert b_calls == []
    # once a finishes, the global slot frees and b can run
    release.set()
    await asyncio.sleep(0.01)
    assert RefreshManager._global_active is None
    assert b.trigger_if_needed(stale=True) is True
    await asyncio.sleep(0.01)
    assert b_calls == [1]


@pytest.mark.asyncio
async def test_fresh_data_does_not_trigger():
    calls = []

    async def sync():
        calls.append(1)
        return {"ok": True}

    mgr = RefreshManager("test", sync)
    assert mgr.trigger_if_needed(stale=False) is False
    await asyncio.sleep(0.01)
    assert calls == []


@pytest.mark.asyncio
async def test_stale_data_triggers_background_sync():
    calls = []

    async def sync():
        calls.append(1)
        return {"ok": True}

    mgr = RefreshManager("test", sync)
    assert mgr.trigger_if_needed(stale=True) is True
    await asyncio.sleep(0.01)
    assert calls == [1]


@pytest.mark.asyncio
async def test_no_concurrent_refreshes():
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_sync():
        started.set()
        await release.wait()
        return {"ok": True}

    mgr = RefreshManager("test", slow_sync, cooldown_seconds=0)
    assert mgr.trigger_if_needed(stale=True) is True
    await started.wait()
    # second trigger while running must be rejected
    assert mgr.trigger_if_needed(stale=True) is False
    assert mgr.is_running is True
    release.set()
    await asyncio.sleep(0.01)
    assert mgr.is_running is False


@pytest.mark.asyncio
async def test_cooldown_blocks_rapid_retriggers():
    calls = []

    async def sync():
        calls.append(1)
        return None  # simulated failed crawl

    mgr = RefreshManager("test", sync, cooldown_seconds=3600)
    assert mgr.trigger_if_needed(stale=True) is True
    await asyncio.sleep(0.01)
    # crawl failed, but cooldown must prevent an immediate retry storm
    assert mgr.trigger_if_needed(stale=True) is False
    assert calls == [1]


@pytest.mark.asyncio
async def test_sync_exception_does_not_break_manager():
    async def bad_sync():
        raise RuntimeError("boom")

    mgr = RefreshManager("test", bad_sync, cooldown_seconds=0)
    assert mgr.trigger_if_needed(stale=True) is True
    await asyncio.sleep(0.01)
    assert mgr.is_running is False
    # manager still usable afterwards
    assert mgr.trigger_if_needed(stale=True) is True


@pytest.mark.asyncio
async def test_shutdown_cancels_running_task():
    release = asyncio.Event()

    async def slow_sync():
        await release.wait()

    mgr = RefreshManager("test", slow_sync)
    mgr.trigger_if_needed(stale=True)
    await asyncio.sleep(0.01)
    await mgr.shutdown()
    assert mgr.is_running is False
