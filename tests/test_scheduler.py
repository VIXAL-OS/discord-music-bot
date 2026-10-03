from __future__ import annotations

import pytest

from music_event_bot.config import Settings
from music_event_bot.services.scheduler import DISCOVERY_MAX_RETRIES, BotScheduler


async def _noop() -> None:
    return None


def _scheduler(discover) -> BotScheduler:
    scheduler = BotScheduler(Settings(_env_file=None))
    scheduler.configure(discover, _noop, _noop)
    return scheduler


@pytest.mark.asyncio
async def test_failed_discovery_schedules_a_retry() -> None:
    calls = 0

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("database is locked")

    scheduler = _scheduler(flaky)
    await scheduler.run_discovery()

    assert calls == 1
    retry = scheduler.scheduler.get_job("discovery-retry")
    assert retry is not None
    assert retry.kwargs == {"attempt": 1}


@pytest.mark.asyncio
async def test_discovery_gives_up_after_the_last_retry() -> None:
    async def broken() -> None:
        raise RuntimeError("database is locked")

    scheduler = _scheduler(broken)
    await scheduler.run_discovery(attempt=DISCOVERY_MAX_RETRIES)

    assert scheduler.scheduler.get_job("discovery-retry") is None


@pytest.mark.asyncio
async def test_successful_discovery_schedules_nothing() -> None:
    scheduler = _scheduler(_noop)
    await scheduler.run_discovery()

    assert scheduler.scheduler.get_job("discovery-retry") is None
    assert scheduler.scheduler.get_job("discovery") is not None
