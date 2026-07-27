from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from music_event_bot.domain.models import DiscoveredEvent, ScoreResult
from music_event_bot.domain.normalization import canonical_fingerprint, normalize_venue
from music_event_bot.domain.venues import VenueAliases
from music_event_bot.services.dedupe import DedupeService, titles_describe_one_show
from music_event_bot.storage.database import Database
from music_event_bot.storage.repositories import EventRepository

EASTERN = ZoneInfo("America/New_York")
SCORE = ScoreResult(score=50, reasons=("test",), affinity_score=50, location_bonus=0)


def _event(**overrides: object) -> DiscoveredEvent:
    base = DiscoveredEvent(
        source_name="source-a",
        source_event_id="a-1",
        title="Ensiferum",
        artist="Ensiferum",
        venue="Thunderbird Music Hall",
        location="Thunderbird Music Hall, 4053 Butler St, Pittsburgh, PA",
        starts_at=datetime(2026, 9, 13, 20, 0, tzinfo=EASTERN),
        ends_at=datetime(2026, 9, 13, 23, 0, tzinfo=EASTERN),
        timezone="America/New_York",
        source_url="https://example.test/ensiferum",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


class TestNormalizeVenue:
    def test_strips_leading_the_and_unifies_theatre_spelling(self) -> None:
        assert normalize_venue("Mr Smalls Theater") == normalize_venue("Mr Smalls Theatre")
        assert normalize_venue("The Roboto Project") == normalize_venue("Roboto Project")

    def test_drops_ticketmaster_state_suffix(self) -> None:
        assert normalize_venue("Howard Theatre-DC") == normalize_venue("Howard Theatre")
        assert normalize_venue("Lincoln Theatre-NC") == normalize_venue("Lincoln Theatre")

    def test_keeps_a_trailing_word_that_is_not_a_state_code(self) -> None:
        assert normalize_venue("Grog Shop and B Side") != normalize_venue("Grog Shop")

    def test_never_collapses_a_bare_state_code_venue_to_nothing(self) -> None:
        assert normalize_venue("PA") == "pa"

    def test_applies_curated_aliases(self) -> None:
        aliases = {"thunderbird": "thunderbird music hall"}
        assert normalize_venue("Thunderbird", aliases) == "thunderbird music hall"

    def test_separate_rooms_stay_separate(self) -> None:
        assert normalize_venue("The Funhouse at Mr Smalls") != normalize_venue(
            "Mr Smalls Theatre"
        )
        assert normalize_venue("Southgate House Revival Sanctuary") != normalize_venue(
            "Southgate House Revival"
        )


class TestVenueAliases:
    def test_absent_file_is_an_empty_table(self, tmp_path: Path) -> None:
        assert not VenueAliases.load(tmp_path / "missing.json")

    def test_loads_and_normalizes_entries(self, tmp_path: Path) -> None:
        path = tmp_path / "venues.json"
        path.write_text(
            json.dumps(
                [{"name": "Thunderbird Music Hall", "aliases": ["Thunderbird"]}]
            ),
            encoding="utf-8",
        )
        aliases = VenueAliases.load(path)
        assert aliases.entries == {"thunderbird": "thunderbird music hall"}

    def test_rejects_an_alias_claimed_by_two_venues(self, tmp_path: Path) -> None:
        path = tmp_path / "venues.json"
        path.write_text(
            json.dumps(
                [
                    {"name": "Venue One", "aliases": ["Shared"]},
                    {"name": "Venue Two", "aliases": ["Shared"]},
                ]
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="claimed by both"):
            VenueAliases.load(path)

    def test_shipped_alias_file_is_valid(self) -> None:
        path = Path(__file__).resolve().parents[1] / "config" / "venue-aliases.json"
        aliases = VenueAliases.load(path)
        assert aliases.entries["thunderbird"] == "thunderbird music hall"


class TestFingerprintStaysCurrent:
    @pytest.mark.asyncio
    async def test_late_arriving_venue_rewrites_the_fingerprint(
        self, repository: EventRepository
    ) -> None:
        """The ICS location-drop case: ingested with no venue, corrected later."""
        first = await repository.upsert_discovered(
            _event(venue=None, location=None), SCORE
        )
        stored = await self._fingerprint_of(repository, first.event.id)
        assert stored == canonical_fingerprint(
            "Ensiferum",
            None,
            datetime(2026, 9, 13, 20, 0, tzinfo=EASTERN),
            source_name="",
            source_event_id="",
        )

        await repository.upsert_discovered(_event(), SCORE)
        repaired = await self._fingerprint_of(repository, first.event.id)
        assert repaired == canonical_fingerprint(
            "Ensiferum",
            "Thunderbird Music Hall",
            datetime(2026, 9, 13, 20, 0, tzinfo=EASTERN),
            source_name="",
            source_event_id="",
        )

    @pytest.mark.asyncio
    async def test_corrected_row_can_still_absorb_another_source(
        self, repository: EventRepository
    ) -> None:
        """Without the resync this second source would create a duplicate."""
        first = await repository.upsert_discovered(
            _event(venue=None, location=None), SCORE
        )
        await repository.upsert_discovered(_event(), SCORE)
        second = await repository.upsert_discovered(
            _event(source_name="source-b", source_event_id="b-1"), SCORE
        )
        assert second.event.id == first.event.id
        assert not second.created

    @pytest.mark.asyncio
    async def test_collision_leaves_the_key_stale_rather_than_crashing(
        self, repository: EventRepository
    ) -> None:
        await repository.upsert_discovered(_event(), SCORE)
        other = await repository.upsert_discovered(
            _event(
                source_name="source-b",
                source_event_id="b-1",
                title="Ensiferum",
                venue="Thunderbird",
            ),
            SCORE,
        )
        # Correcting source-b's venue makes it identical to the first row.
        await repository.upsert_discovered(
            _event(source_name="source-b", source_event_id="b-1"), SCORE
        )
        assert await repository.get_event(other.event.id) is not None

    @pytest.mark.asyncio
    async def test_manual_edit_resyncs_the_fingerprint(
        self, repository: EventRepository
    ) -> None:
        result = await repository.upsert_discovered(_event(venue="Thunderbird"), SCORE)
        await repository.update_event(result.event.id, venue="Thunderbird Music Hall")
        assert await self._fingerprint_of(
            repository, result.event.id
        ) == canonical_fingerprint(
            "Ensiferum",
            "Thunderbird Music Hall",
            datetime(2026, 9, 13, 20, 0, tzinfo=EASTERN),
            source_name="",
            source_event_id="",
        )

    @staticmethod
    async def _fingerprint_of(repository: EventRepository, event_id: str) -> str:
        async with repository.database.connect() as connection:
            cursor = await connection.execute(
                "SELECT fingerprint FROM events WHERE id = ?", (event_id,)
            )
            row = await cursor.fetchone()
            assert row is not None
            return str(row["fingerprint"])


class TestVenueAliasesMergeAtIngest:
    @pytest.mark.asyncio
    async def test_aliased_venues_are_one_event(self, database: Database) -> None:
        aliases = VenueAliases(entries={"thunderbird": "thunderbird music hall"})
        repository = EventRepository(database, venue_aliases=aliases)
        first = await repository.upsert_discovered(_event(), SCORE)
        second = await repository.upsert_discovered(
            _event(source_name="source-b", source_event_id="b-1", venue="Thunderbird"),
            SCORE,
        )
        assert second.event.id == first.event.id

    @pytest.mark.asyncio
    async def test_unaliased_venues_remain_distinct(self, database: Database) -> None:
        repository = EventRepository(database)
        first = await repository.upsert_discovered(_event(), SCORE)
        second = await repository.upsert_discovered(
            _event(source_name="source-b", source_event_id="b-1", venue="Spirit Lodge"),
            SCORE,
        )
        assert second.event.id != first.event.id


class TestVenueAndNightLookup:
    """What the curator tasks call before writing onto the shared calendar."""

    @pytest.mark.asyncio
    async def test_venue_filter_follows_aliases(self, database: Database) -> None:
        aliases = VenueAliases(entries={"thunderbird": "thunderbird music hall"})
        repository = EventRepository(database, venue_aliases=aliases)
        await repository.upsert_discovered(_event(venue="Thunderbird"), SCORE)
        found = await repository.list_events(venue="Thunderbird Music Hall")
        assert [event.title for event in found] == ["Ensiferum"]

    @pytest.mark.asyncio
    async def test_night_filter_uses_local_date_not_utc(
        self, repository: EventRepository
    ) -> None:
        """A 9pm Pittsburgh show is 01:00 UTC the next day."""
        await repository.upsert_discovered(
            _event(starts_at=datetime(2026, 9, 13, 21, 0, tzinfo=EASTERN)), SCORE
        )
        assert await repository.list_events(on_date=date(2026, 9, 13))
        assert not await repository.list_events(on_date=date(2026, 9, 14))

    @pytest.mark.asyncio
    async def test_unrelated_venue_is_excluded(self, repository: EventRepository) -> None:
        await repository.upsert_discovered(_event(), SCORE)
        assert not await repository.list_events(venue="Spirit")

    @pytest.mark.asyncio
    async def test_finds_rows_whose_stored_venue_key_predates_the_alias(
        self, database: Database
    ) -> None:
        """The row was written before the alias existed, so its column is stale.

        Missing it is the dangerous failure: the caller reads "not present",
        adds a calendar entry, and creates the duplicate.
        """
        await EventRepository(database).upsert_discovered(
            _event(venue="Thunderbird"), SCORE
        )
        aliased = EventRepository(
            database, venue_aliases=VenueAliases(entries={"thunderbird": "thunderbird music hall"})
        )
        found = await aliased.list_events(venue="Thunderbird Music Hall")
        assert [event.title for event in found] == ["Ensiferum"]


class TestTitleMatching:
    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("Ensiferum", "Winter Storm Over North America 2026: Ensiferum & Firewind"),
            ("Hoagie Dreams", "Hoagie Dreams All Night Long @ Hot Mass"),
            ("Hot Mass: C Powers, Nick Boyd, Naeem", "C Powers, Nick Boyd, Naeem @ Hot Mass"),
            ("Interpol w/ DIIV", "Interpol with DIIV"),
        ],
    )
    def test_recognizes_one_show_described_two_ways(self, left: str, right: str) -> None:
        assert titles_describe_one_show(left, right)

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("Nuovo Testamento, Shadow Age, Cemetery Sex", "Mau Maus, Stepmother, K.O.S."),
            ("Dopethrone", "Eyehategod"),
            ("The Beths", "The Body"),
        ],
    )
    def test_keeps_different_bills_apart(self, left: str, right: str) -> None:
        assert not titles_describe_one_show(left, right)


@pytest_asyncio.fixture
async def seeded(database: Database) -> EventRepository:
    repository = EventRepository(database)
    await repository.upsert_discovered(_event(), SCORE)
    await repository.upsert_discovered(
        _event(
            source_name="source-b",
            source_event_id="b-1",
            title="Winter Storm Over North America 2026: Ensiferum & Firewind",
            starts_at=datetime(2026, 9, 13, 19, 0, tzinfo=EASTERN),
        ),
        SCORE,
    )
    return repository


class TestDedupeService:
    @pytest.mark.asyncio
    async def test_finds_the_pair_and_keeps_nothing_by_default(
        self, seeded: EventRepository
    ) -> None:
        report = await DedupeService(seeded).scan()
        assert len(report.groups) == 1
        assert len(report.groups[0].losers) == 1
        assert report.merged == 0
        assert len(await seeded.list_events()) == 2

    @pytest.mark.asyncio
    async def test_merge_folds_sources_into_the_keeper(
        self, seeded: EventRepository
    ) -> None:
        service = DedupeService(seeded)
        report = await service.scan()
        merged = await service.merge(report.groups[0], include_published=False)
        assert merged == 1
        remaining = await seeded.list_events()
        assert len(remaining) == 1
        async with seeded.database.connect() as connection:
            cursor = await connection.execute(
                "SELECT source_name FROM event_sources WHERE event_id = ?",
                (remaining[0].id,),
            )
            names = sorted(row["source_name"] for row in await cursor.fetchall())
        assert names == ["source-a", "source-b"]

    @pytest.mark.asyncio
    async def test_an_early_and_a_late_set_are_not_duplicates(
        self, database: Database
    ) -> None:
        repository = EventRepository(database)
        await repository.upsert_discovered(
            _event(title="Sinead Harnett", venue="Blue Note Jazz Club"), SCORE
        )
        await repository.upsert_discovered(
            _event(
                source_name="source-b",
                source_event_id="b-1",
                title="Sinead Harnett",
                venue="Blue Note Jazz Club",
                starts_at=datetime(2026, 9, 13, 22, 30, tzinfo=EASTERN),
            ),
            SCORE,
        )
        report = await DedupeService(repository).scan()
        assert report.groups == []

    @pytest.mark.asyncio
    async def test_published_row_wins_and_a_published_loser_is_protected(
        self, seeded: EventRepository
    ) -> None:
        events = sorted(await seeded.list_events(), key=lambda event: event.title)
        # The longer arcane-style title is published and announced; the bare one is not.
        published = next(event for event in events if event.title.startswith("Winter"))
        await seeded.approve(published.id, reviewer_id=1)
        await seeded.begin_publication(published.id)
        await seeded.record_announcement(published.id, 999)
        await seeded.mark_published(published.id)

        service = DedupeService(seeded)
        report = await service.scan()
        group = report.groups[0]
        assert group.keeper.event_id == published.id

        # The announced row is the keeper, so nothing here needs manual cleanup.
        assert group.blocked_by_publication == ()
        assert group.losers[0].is_public is False

    @pytest.mark.asyncio
    async def test_an_announced_loser_is_not_deleted_without_opt_in(
        self, seeded: EventRepository
    ) -> None:
        """Both copies announced: merging would strand a live Discord message."""
        for event in await seeded.list_events():
            await seeded.approve(event.id, reviewer_id=1)
            await seeded.begin_publication(event.id)
            await seeded.record_announcement(event.id, 900 + len(event.title))
            await seeded.mark_published(event.id)

        service = DedupeService(seeded)
        group = (await service.scan()).groups[0]
        assert len(group.blocked_by_publication) == 1

        assert await service.merge(group, include_published=False) == 0
        assert len(await seeded.list_events()) == 2

        assert await service.merge(group, include_published=True) == 1
        assert len(await seeded.list_events()) == 1

    @pytest.mark.asyncio
    async def test_repair_skips_colliding_rows(self, seeded: EventRepository) -> None:
        service = DedupeService(seeded)
        report = await service.scan()
        repaired = await service.repair(report)
        assert repaired == report.repaired
        rescan = await DedupeService(seeded).scan()
        assert [stale for stale in rescan.stale if stale.collides_with is None] == []

    @pytest.mark.asyncio
    async def test_window_is_configurable(self, database: Database) -> None:
        repository = EventRepository(database)
        await repository.upsert_discovered(_event(title="Grouper"), SCORE)
        await repository.upsert_discovered(
            _event(
                source_name="source-b",
                source_event_id="b-1",
                title="Grouper",
                starts_at=datetime(2026, 9, 13, 20, 0, tzinfo=EASTERN)
                + timedelta(minutes=150),
            ),
            SCORE,
        )
        assert (await DedupeService(repository).scan(window_minutes=90)).groups == []
        assert (await DedupeService(repository).scan(window_minutes=180)).groups
