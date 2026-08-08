from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from music_event_bot.config import Settings
from music_event_bot.discovery.base import DiscoveryWindow
from music_event_bot.domain.models import (
    DiscoveredEvent,
    EventRecord,
    EventStatus,
    ScoreResult,
    TasteProfile,
)
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


def test_address_book_corrects_a_wrong_address_when_keyed_by_venue() -> None:
    """A curated entry outranks the source for the room it names.

    arcane.city lists The Eagle in 15202; it is in 15212. A fallback-only book
    could never fix that, because a wrong address still counts as present.
    """
    from music_event_bot.services.orchestration import _apply_address_book

    book = {"eagle": "The Eagle, 1740 Eckert St, Pittsburgh, PA 15212"}
    spin = DiscoveredEvent(
        source_name="arcane-city",
        source_event_id="spin-september-2026",
        title="SPIN - September 2026",
        venue="The Eagle",
        location="1740 Eckert, Pittsburgh, PA, 15202",
    )
    corrected = _apply_address_book(spin, book)
    assert corrected.location == "The Eagle, 1740 Eckert St, Pittsburgh, PA 15212"
    # The source's own venue spelling is kept -- only the address was wrong.
    assert corrected.venue == "The Eagle"

    # Word-bounded, so a different room that merely contains the fragment is safe.
    other = DiscoveredEvent(
        source_name="arcane-city",
        source_event_id="en-1",
        title="Some Show",
        venue="Eagles Nest",
        location="Eagles Nest, Somewhere, PA 15001",
    )
    assert _apply_address_book(other, book) is other


def test_address_book_leaves_addresses_it_does_not_contradict() -> None:
    """The book corrects wrong listings, it does not overwrite good ones.

    A curated entry is often vaguer than what a source eventually supplies
    (Hot Mass withholds the street on purpose), and differing spellings of the
    same address are not errors worth an announcement edit.
    """
    from music_event_bot.services.orchestration import _apply_address_book

    book = {
        "hot mass": "Hot Mass, Pittsburgh, PA (address emailed to ticketholders)",
        "roboto": "The Mr. Roboto Project, 5106 Penn Avenue, Pittsburgh, PA 15224",
    }
    # The source knows the street; the book deliberately does not.
    precise = DiscoveredEvent(
        source_name="arcane-city",
        source_event_id="hm-3",
        title="Hoagie Dreams",
        venue="Hot Mass",
        location="1139 Penn Ave, Pittsburgh, PA",
    )
    assert _apply_address_book(precise, book) is precise

    # Same address, different spelling -- agrees on the zip, so no churn.
    spelling = DiscoveredEvent(
        source_name="ics",
        source_event_id="rb-9",
        title="Truck Violence",
        venue="The Mr. Roboto Project",
        location="The Mr. Roboto Project, 5106 Penn Ave, Pittsburgh, PA 15224",
    )
    assert _apply_address_book(spelling, book) is spelling


def test_address_book_fills_a_location_that_only_echoes_the_venue() -> None:
    """A location repeating the venue name is not an address."""
    from music_event_bot.services.orchestration import _apply_address_book

    book = {"roboto": "The Mr. Roboto Project, 5106 Penn Avenue, Pittsburgh, PA 15224"}
    flea = DiscoveredEvent(
        source_name="ics",
        source_event_id="roboto-flea-1",
        title="Roboto Punk Rock Flea Market",
        venue="Roboto Project",
        location="Roboto Project",
    )
    filled = _apply_address_book(flea, book)
    assert filled.location == "The Mr. Roboto Project, 5106 Penn Avenue, Pittsburgh, PA 15224"
    assert filled.venue == "Roboto Project"


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


def _record(**overrides: Any) -> EventRecord:
    values: dict[str, Any] = {
        "id": "ev-1",
        "title": "The Example Ensemble",
        "artist": "The Example Ensemble",
        "artists": ("The Example Ensemble",),
        "venue": "Example Hall",
        "location": "Example Hall, Pittsburgh, PA",
        "starts_at": datetime(2026, 9, 1, 23, 0, tzinfo=UTC),
        "ends_at": None,
        "timezone": "America/New_York",
        "url": "https://events.example.test/show",
        "image_url": None,
        "description": None,
        "genres": ("indie",),
        "status": EventStatus.PENDING_REVIEW,
        "score": 60,
        "match_reasons": ("genre match: indie",),
        "created_at": datetime(2026, 8, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 8, 1, tzinfo=UTC),
    }
    values.update(overrides)
    return EventRecord(**values)


class _FoundArtwork:
    """Stands in for ArtworkResolver: always turns up one image."""

    shared_images: frozenset[str] = frozenset()

    def __init__(self, image: str = "https://cdn.example.test/flyer.webp") -> None:
        self.image = image

    async def resolve(self, event: DiscoveredEvent) -> str | None:
        return self.image


class _ArtworkRepository:
    """Repository stub for the artwork path; upsert hands back a real record."""

    def __init__(self, status: EventStatus) -> None:
        self.status = status
        self.updates: list[tuple[str, dict[str, Any]]] = []

    async def upsert_discovered(
        self, event: DiscoveredEvent, score: ScoreResult
    ) -> SimpleNamespace:
        return SimpleNamespace(
            created=True,
            source_created=True,
            event=_record(status=self.status),
        )

    async def update_event(self, event_id: str, **fields: Any) -> EventRecord:
        self.updates.append((event_id, fields))
        return _record(id=event_id, status=self.status, **fields)

    async def record_job_run(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def dedupe_tour_events(self, *args: Any) -> int:
        return 0

    async def published_with_closer_pending(self, *args: Any) -> list[tuple[str, str]]:
        return []


class _SyncSpy:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.error = error

    async def update_existing(self, event_id: str) -> None:
        self.calls.append(event_id)
        if self.error is not None:
            raise self.error


def _artwork_orchestrator(
    repository: _ArtworkRepository, sync: _SyncSpy | None, complete_event: DiscoveredEvent
) -> DiscoveryOrchestrator:
    settings = Settings(_env_file=None, preferred_genres="indie")
    source = StaticSource("calendar", [complete_event], requires_affinity=True)
    return DiscoveryOrchestrator(
        settings,
        cast(EventRepository, repository),
        [source],
        TasteProfile(genres=("indie",)),
        artwork=cast(Any, _FoundArtwork()),
        published_sync=sync,
    )


@pytest.mark.asyncio
async def test_late_artwork_refreshes_an_already_published_announcement(
    complete_event,
) -> None:
    """Art that arrives after the announcement must reach Discord, not just SQLite.

    Sources routinely post a flyer days after first listing a show. Discovery
    wrote the new image_url straight to the database and stopped there, so the
    embed Discord had already posted kept its empty image until somebody ran
    set-image by hand -- 57 future shows were sitting in that state.
    """
    repository = _ArtworkRepository(EventStatus.PUBLISHED)
    sync = _SyncSpy()
    orchestrator = _artwork_orchestrator(repository, sync, complete_event)

    summary = await orchestrator.run()

    assert summary.artwork_found == 1
    assert repository.updates == [("ev-1", {"image_url": "https://cdn.example.test/flyer.webp"})]
    assert sync.calls == ["ev-1"]


@pytest.mark.asyncio
async def test_late_artwork_leaves_unpublished_events_alone(complete_event) -> None:
    """Nothing has been posted yet, so there is no embed to refresh."""
    repository = _ArtworkRepository(EventStatus.PENDING_REVIEW)
    sync = _SyncSpy()
    orchestrator = _artwork_orchestrator(repository, sync, complete_event)

    summary = await orchestrator.run()

    assert summary.artwork_found == 1
    assert sync.calls == []


@pytest.mark.asyncio
async def test_a_failed_announcement_refresh_does_not_abort_discovery(complete_event) -> None:
    """The artwork is already saved; a Discord hiccup must not lose the run."""
    repository = _ArtworkRepository(EventStatus.PUBLISHED)
    sync = _SyncSpy(error=RuntimeError("Discord 503"))
    orchestrator = _artwork_orchestrator(repository, sync, complete_event)

    summary = await orchestrator.run()

    assert sync.calls == ["ev-1"]
    assert summary.artwork_found == 1


@pytest.mark.asyncio
async def test_discovery_runs_without_a_publisher(complete_event) -> None:
    """CLI scrape-once and tests have no live guild; discovery must still finish."""
    repository = _ArtworkRepository(EventStatus.PUBLISHED)
    orchestrator = _artwork_orchestrator(repository, None, complete_event)

    summary = await orchestrator.run()

    assert summary.artwork_found == 1
