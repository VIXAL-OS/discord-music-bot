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
async def test_out_of_radius_events_are_dropped_for_every_source(complete_event) -> None:
    """The radius filter used to live in the Ticketmaster parser only.

    Every other source -- ICS, RSS, Squarespace, arcane, AXS/TicketWeb -- ingested
    at any distance. Note this only bites past max_travel_radius_miles (350 by
    default), which is wide enough to admit New York at 308 mi; the default is a
    separate calibration question from where the check lives.
    """
    from dataclasses import replace as dc_replace

    settings = Settings(_env_file=None, preferred_genres="indie")
    nearby = dc_replace(
        complete_event,
        source_event_id="near-1",
        title="Nearby Show",
        venue_latitude=40.4406,  # Pittsburgh
        venue_longitude=-79.9959,
    )
    chicago = dc_replace(
        complete_event,
        source_event_id="far-1",
        title="Chicago Show",
        venue_latitude=41.8781,  # ~416 mi from home
        venue_longitude=-87.6298,
    )
    # An ICS feed, not Ticketmaster: previously nothing here checked distance.
    source = StaticSource("calendar", [nearby, chicago], requires_affinity=True)
    repository = RecordingRepository()
    orchestrator = DiscoveryOrchestrator(
        settings, cast(EventRepository, repository), [source], TasteProfile(genres=("indie",))
    )

    summary = await orchestrator.run()

    assert summary.ignored_out_of_radius == 1
    assert [event.title for event, _ in repository.upserts] == ["Nearby Show"]


@pytest.mark.asyncio
async def test_events_without_coordinates_survive_the_radius_filter(complete_event) -> None:
    """DIY listings routinely carry no coordinates; absent geography is not distance."""
    from dataclasses import replace as dc_replace

    settings = Settings(_env_file=None, preferred_genres="indie")
    unlocated = dc_replace(complete_event, source_event_id="diy-1", title="Basement Show")
    assert unlocated.venue_latitude is None
    source = StaticSource("calendar", [unlocated], requires_affinity=True)
    repository = RecordingRepository()
    orchestrator = DiscoveryOrchestrator(
        settings, cast(EventRepository, repository), [source], TasteProfile(genres=("indie",))
    )

    summary = await orchestrator.run()

    assert summary.ignored_out_of_radius == 0
    assert [event.title for event, _ in repository.upserts] == ["Basement Show"]


@pytest.mark.asyncio
async def test_blocklist_rejects_any_act_on_the_bill(complete_event, tmp_path) -> None:
    """A blocked opener keeps the whole show out, not just a blocked headliner."""
    import json
    from dataclasses import replace as dc_replace

    from music_event_bot.domain.blocklist import Blocklist

    path = tmp_path / "blocked-artists.json"
    path.write_text(
        json.dumps([{"name": "Blocked Act", "reason": "operator's reason"}]),
        encoding="utf-8",
    )
    blocklist = Blocklist.load(path)

    settings = Settings(_env_file=None, preferred_genres="indie")
    as_support = dc_replace(
        complete_event,
        source_event_id="bill-1",
        title="Headline Act, Blocked Act, Third Act",
        artist="Headline Act",
        artists=("Headline Act", "Blocked Act", "Third Act"),
    )
    clean = dc_replace(complete_event, source_event_id="bill-2", title="Unrelated Show")
    source = StaticSource("calendar", [as_support, clean], requires_affinity=True)
    repository = RecordingRepository()
    orchestrator = DiscoveryOrchestrator(
        settings,
        cast(EventRepository, repository),
        [source],
        TasteProfile(genres=("indie",)),
        blocklist=blocklist,
    )

    summary = await orchestrator.run()

    assert summary.blocked_artists == 1
    assert [event.title for event, _ in repository.upserts] == ["Unrelated Show"]


def test_blocklist_title_fallback_only_without_a_lineup() -> None:
    """Title matching is a last resort: it also catches unrelated marketing copy."""
    from music_event_bot.domain.blocklist import Blocklist

    entries = Blocklist(entries={"blocked act": ("Blocked Act", "operator's reason")})

    no_lineup = DiscoveredEvent(
        source_name="t",
        source_event_id="1",
        title="Blocked Act and friends",
        artist=None,
        artists=(),
    )
    assert entries.match(no_lineup) is not None

    # Source gave a lineup that does not include the blocked act; the title
    # mentioning it (a support-act rumour, a venue's copy) must not block.
    with_lineup = DiscoveredEvent(
        source_name="t",
        source_event_id="2",
        title="Blocked Act tribute night",
        artist="Some Cover Band",
        artists=("Some Cover Band",),
    )
    assert entries.match(with_lineup) is None


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


def test_address_book_fills_secret_location_series() -> None:
    from music_event_bot.services.orchestration import _apply_address_book

    book = {"hot mass": "Hot Mass, Pittsburgh, PA (address emailed to ticketholders)"}
    party = DiscoveredEvent(
        source_name="ics",
        source_event_id="hm-1",
        title="DETOUR: Anny, AK, Lemonline @ Hot Mass",
        incomplete_reasons=("missing venue", "missing location"),
    )
    filled = _apply_address_book(party, book)
    assert filled.venue == "Hot Mass"
    assert filled.location == "Hot Mass, Pittsburgh, PA (address emailed to ticketholders)"
    assert filled.incomplete_reasons == ()

    # An explicit venue always wins, and unrelated titles are untouched.
    explicit = DiscoveredEvent(
        source_name="ics",
        source_event_id="hm-2",
        title="Hot Mass Anniversary",
        venue="Somewhere Else",
        location="Somewhere Else, Pittsburgh, PA",
    )
    assert _apply_address_book(explicit, book) is explicit
    unrelated = DiscoveredEvent(
        source_name="ics",
        source_event_id="other-1",
        title="Mass Choir Recital",
        incomplete_reasons=("missing venue",),
    )
    assert _apply_address_book(unrelated, book).venue is None


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
