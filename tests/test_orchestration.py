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

    async def dedupe_tour_events(self, *args: Any) -> int:
        return 0

    async def published_with_closer_pending(self, *args: Any) -> list[tuple[str, str]]:
        return []


@pytest.mark.asyncio
async def test_trusted_venue_bypasses_affinity_gate(complete_event) -> None:
    from dataclasses import replace as dc_replace

    settings = Settings(_env_file=None, trusted_venues="Mr Smalls, Poetry Lounge")
    trusted_show = dc_replace(
        complete_event,
        source_event_id="smalls-1",
        title="Man Man with Death Valley Girls",
        venue="Mr Smalls Theatre",
    )
    unrelated_show = dc_replace(
        complete_event,
        source_event_id="elsewhere-1",
        title="Unrelated Arena Act",
        venue="Enormodome",
    )
    source = StaticSource(
        "ticketmaster", [trusted_show, unrelated_show], requires_affinity=True
    )
    repository = RecordingRepository()
    orchestrator = DiscoveryOrchestrator(
        settings, cast(EventRepository, repository), [source], TasteProfile()
    )

    summary = await orchestrator.run()

    assert summary.created == 1
    assert summary.ignored_below_score == 1
    stored_event, stored_score = repository.upserts[0]
    assert stored_event.title == "Man Man with Death Valley Girls"
    assert "trusted venue: Mr Smalls Theatre" in stored_score.reasons


@pytest.mark.asyncio
async def test_orchestrator_uses_one_to_180_day_window() -> None:
    settings = Settings(_env_file=None)
    source = CapturingSource()
    repository = cast(EventRepository, RecordingRepository())
    orchestrator = DiscoveryOrchestrator(settings, repository, [source], TasteProfile())

    before = datetime.now(settings.timezone)
    await orchestrator.run()
    after = datetime.now(settings.timezone)

    assert source.window is not None
    assert before + timedelta(days=1) <= source.window.starts_at <= after + timedelta(days=1)
    assert before + timedelta(days=180) <= source.window.ends_at <= after + timedelta(days=180)


@pytest.mark.asyncio
async def test_affinity_gate_applies_to_every_source() -> None:
    """Curated feeds are gated too; only taste-matching events get through."""
    settings = Settings(_env_file=None)
    unrelated = DiscoveredEvent(
        source_name="fixture-feed",
        source_event_id="unrelated-local",
        title="Unrelated Local Listing",
        venue_latitude=settings.home_point.latitude,
        venue_longitude=settings.home_point.longitude,
    )
    matching = DiscoveredEvent(
        source_name="fixture-feed",
        source_event_id="matching-local",
        title="Grindcore Night",
        genres=("grindcore",),
        venue_latitude=settings.home_point.latitude,
        venue_longitude=settings.home_point.longitude,
    )
    profile = TasteProfile(genres=("grindcore",))

    for requires_affinity in (False, True):
        repository = RecordingRepository()
        source = StaticSource(
            "fixture-feed", [unrelated, matching], requires_affinity=requires_affinity
        )
        orchestrator = DiscoveryOrchestrator(
            settings, cast(EventRepository, repository), [source], profile
        )
        summary = await orchestrator.run()

        assert summary.created == 1
        assert summary.ignored_below_score == 1
        stored_event, stored_score = repository.upserts[0]
        assert stored_event.title == "Grindcore Night"
        curated_reasons = [r for r in stored_score.reasons if r.startswith("curated source")]
        assert bool(curated_reasons) is not requires_affinity
