from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from music_event_bot.config import Settings
from music_event_bot.discovery.base import DiscoveryWindow
from music_event_bot.domain.models import DiscoveredEvent, ScoreResult, TasteProfile
from music_event_bot.services.orchestration import DiscoveryOrchestrator
from music_event_bot.storage.repositories import EventRepository


class CapturingSource:
    name = "capture"

    def __init__(self) -> None:
        self.window: DiscoveryWindow | None = None

    async def discover(self, window: DiscoveryWindow) -> list[Any]:
        self.window = window
        return []


class StaticSource:
    def __init__(
        self,
        name: str,
        events: list[DiscoveredEvent],
        *,
        requires_affinity: bool = False,
    ) -> None:
        self.name = name
        self.events = events
        self.requires_affinity = requires_affinity

    async def discover(self, window: DiscoveryWindow) -> list[DiscoveredEvent]:
        return self.events


class RecordingRepository:
    def __init__(self) -> None:
        self.upserts: list[tuple[DiscoveredEvent, ScoreResult]] = []

    async def upsert_discovered(
        self, event: DiscoveredEvent, score: ScoreResult
    ) -> SimpleNamespace:
        self.upserts.append((event, score))
        return SimpleNamespace(created=True, source_created=True)

    async def record_job_run(self, *args: Any, **kwargs: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_orchestrator_uses_one_to_180_day_window() -> None:
    settings = Settings(_env_file=None)
    source = CapturingSource()
    repository = cast(
        EventRepository,
        SimpleNamespace(record_job_run=lambda *args, **kwargs: _completed()),
    )
    orchestrator = DiscoveryOrchestrator(settings, repository, [source], TasteProfile())

    before = datetime.now(settings.timezone)
    await orchestrator.run()
    after = datetime.now(settings.timezone)

    assert source.window is not None
    assert before + timedelta(days=1) <= source.window.starts_at <= after + timedelta(days=1)
    assert before + timedelta(days=180) <= source.window.ends_at <= after + timedelta(days=180)


@pytest.mark.asyncio
async def test_affinity_gate_applies_only_to_opted_in_sources() -> None:
    settings = Settings(_env_file=None)
    unrelated = DiscoveredEvent(
        source_name="fixture-feed",
        source_event_id="unrelated-local",
        title="Unrelated Local Listing",
        venue_latitude=settings.home_point.latitude,
        venue_longitude=settings.home_point.longitude,
    )

    feed_repository = RecordingRepository()
    feed_source = StaticSource("fixture-feed", [unrelated])
    feed_orchestrator = DiscoveryOrchestrator(
        settings,
        cast(EventRepository, feed_repository),
        [feed_source],
        TasteProfile(),
    )
    feed_summary = await feed_orchestrator.run()

    assert feed_summary.created == 1
    assert feed_summary.ignored_below_score == 0
    assert len(feed_repository.upserts) == 1

    ticketmaster_repository = RecordingRepository()
    ticketmaster_source = StaticSource(
        "ticketmaster",
        [unrelated],
        requires_affinity=True,
    )
    ticketmaster_orchestrator = DiscoveryOrchestrator(
        settings,
        cast(EventRepository, ticketmaster_repository),
        [ticketmaster_source],
        TasteProfile(),
    )
    ticketmaster_summary = await ticketmaster_orchestrator.run()

    assert ticketmaster_summary.created == 0
    assert ticketmaster_summary.ignored_below_score == 1
    assert ticketmaster_repository.upserts == []


async def _completed() -> None:
    return None
