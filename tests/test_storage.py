from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from music_event_bot.domain.geography import GeoPoint
from music_event_bot.domain.models import EventStatus, ScoreResult, TasteProfile
from music_event_bot.storage.database import Database
from music_event_bot.storage.migrations import LATEST_SCHEMA_VERSION, MIGRATIONS


@pytest.mark.asyncio
async def test_initialize_applies_migrations_once(database) -> None:
    assert await database.schema_version() == LATEST_SCHEMA_VERSION
    assert await database.initialize() == LATEST_SCHEMA_VERSION

    async with database.connect() as connection:
        cursor = await connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )
        assert [row["version"] for row in await cursor.fetchall()] == list(
            range(1, LATEST_SCHEMA_VERSION + 1)
        )

        cursor = await connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
        table_names = {row["name"] for row in await cursor.fetchall()}
        cursor = await connection.execute("PRAGMA table_info(events)")
        event_columns = {row["name"] for row in await cursor.fetchall()}

    assert {
        "events",
        "event_sources",
        "reviews",
        "publications",
        "taste_preferences",
        "genre_roles",
        "job_runs",
        "schema_migrations",
    } <= table_names
    assert {"venue_latitude", "venue_longitude"} <= event_columns


@pytest.mark.asyncio
async def test_migrates_existing_schema_one_database_to_two(tmp_path) -> None:
    database = Database(tmp_path / "schema-one.sqlite3")
    async with database.connect() as connection:
        await connection.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await connection.executescript(MIGRATIONS[0][1])
        await connection.execute("INSERT INTO schema_migrations(version) VALUES (1)")
        await connection.execute(
            """
            INSERT INTO events(
                id, fingerprint, title, title_normalized, venue_normalized,
                genres_json, status, score, match_reasons_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "existing-event",
                "existing-fingerprint",
                "Existing Event",
                "existing event",
                "",
                "[]",
                "pending_review",
                0,
                "[]",
                "2026-07-15T12:00:00+00:00",
                "2026-07-15T12:00:00+00:00",
            ),
        )
        await connection.commit()

    assert await database.initialize() == 6
    async with database.connect() as connection:
        cursor = await connection.execute("PRAGMA table_info(events)")
        columns = {row["name"] for row in await cursor.fetchall()}
        cursor = await connection.execute(
            "SELECT venue_latitude, venue_longitude FROM events WHERE id = ?",
            ("existing-event",),
        )
        row = await cursor.fetchone()
        cursor = await connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        tables = {table_row["name"] for table_row in await cursor.fetchall()}

    assert {"venue_latitude", "venue_longitude"} <= columns
    assert {"artist_tags", "artist_tag_fetches", "genre_tag_map"} <= tables
    assert row is not None
    assert row["venue_latitude"] is None
    assert row["venue_longitude"] is None


@pytest.mark.asyncio
async def test_discovered_upsert_is_idempotent_and_merges_second_source(
    database, repository, complete_event
) -> None:
    score = ScoreResult(score=75, reasons=("artist match: The Example Ensemble",))
    event_with_coordinates = replace(
        complete_event,
        venue_latitude=40.4406,
        venue_longitude=-79.9959,
    )

    initial = await repository.upsert_discovered(event_with_coordinates, score)
    replay = await repository.upsert_discovered(
        replace(
            event_with_coordinates,
            description="Updated source description.",
            venue_latitude=None,
            venue_longitude=None,
        ),
        score,
    )
    duplicate_source = await repository.upsert_discovered(
        replace(
            complete_event,
            source_name="second-source",
            source_event_id="same-concert-from-another-source",
        ),
        score,
    )

    assert initial.created and initial.source_created
    assert not replay.created and not replay.source_created
    assert not duplicate_source.created and duplicate_source.source_created
    assert initial.event.id == replay.event.id == duplicate_source.event.id

    stored = await repository.get_event(initial.event.id)
    assert stored is not None
    assert stored.description == "Updated source description."
    assert stored.status is EventStatus.PENDING_REVIEW
    assert stored.venue_latitude == 40.4406
    assert stored.venue_longitude == -79.9959

    async with database.connect() as connection:
        cursor = await connection.execute("SELECT COUNT(*) AS count FROM events")
        assert (await cursor.fetchone())["count"] == 1
        cursor = await connection.execute("SELECT COUNT(*) AS count FROM event_sources")
        assert (await cursor.fetchone())["count"] == 2


@pytest.mark.asyncio
async def test_review_queue_orders_by_score_then_date(repository, complete_event) -> None:
    later = replace(
        complete_event,
        source_event_id="later-high",
        starts_at=complete_event.starts_at + timedelta(days=5),
        ends_at=complete_event.ends_at + timedelta(days=5),
    )
    earlier_low = replace(
        complete_event,
        source_event_id="earlier-low",
        title="Earlier Low Score",
        artist="Earlier Artist",
        starts_at=complete_event.starts_at - timedelta(days=1),
        ends_at=complete_event.ends_at - timedelta(days=1),
    )
    earlier_high = replace(
        complete_event,
        source_event_id="earlier-high",
        title="Earlier High Score",
        artist="Earlier High Artist",
    )
    await repository.upsert_discovered(later, ScoreResult(80, ("high",)))
    await repository.upsert_discovered(earlier_low, ScoreResult(20, ("low",)))
    await repository.upsert_discovered(earlier_high, ScoreResult(80, ("high",)))

    queue = await repository.list_review_queue()
    assert [event.title for event in queue] == [
        "Earlier High Score",
        "The Example Ensemble",
        "Earlier Low Score",
    ]


@pytest.mark.asyncio
async def test_backfills_ticketmaster_coordinates_and_rescores(
    repository, complete_event
) -> None:
    ticketmaster_event = replace(
        complete_event,
        source_name="ticketmaster",
        source_event_id="tm-backfill",
        venue_latitude=None,
        venue_longitude=None,
        raw={
            "_embedded": {
                "venues": [
                    {"location": {"latitude": "40.4406", "longitude": "-79.9959"}}
                ]
            }
        },
    )
    inserted = await repository.upsert_discovered(ticketmaster_event, ScoreResult(0, ()))
    await repository.upsert_discovered(
        replace(
            ticketmaster_event,
            source_event_id="tm-malformed-coordinates",
            title="Malformed Coordinate Event",
            artist="Different Artist",
            raw={"_embedded": {"venues": [{"location": "not-an-object"}]}},
        ),
        ScoreResult(0, ()),
    )

    assert await repository.backfill_ticketmaster_coordinates() == 1
    assert (
        await repository.rescore_reviewable_events(
            TasteProfile(artists=("The Example Ensemble",)),
            home=GeoPoint(40.4406, -79.9959),
            max_travel_radius_miles=350,
        )
        == 2
    )
    stored = await repository.get_event(inserted.event.id)
    assert stored is not None
    assert stored.status is EventStatus.PENDING_REVIEW
    assert stored.venue_latitude == 40.4406
    assert stored.venue_longitude == -79.9959
    assert stored.score == 80
    assert "distance preference: +20" in stored.match_reasons[-1]
