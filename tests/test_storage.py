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

    assert await database.initialize() == LATEST_SCHEMA_VERSION
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


_HOME = GeoPoint(40.44, -79.99)  # Pittsburgh
_ARTIST_MATCH_SCORE = ScoreResult(
    score=60,
    reasons=("artist match: Man Man",),
    affinity_score=60,
    location_bonus=0,
    distance_miles=None,
)


def _tour_stop(
    complete_event,
    source_event_id: str,
    title: str,
    venue: str,
    artist: str = "Man Man",
    **coords,
):
    return replace(
        complete_event,
        source_event_id=source_event_id,
        title=title,
        venue=venue,
        artist=artist,
        **coords,
    )


@pytest.mark.asyncio
async def test_tour_dedupe_keeps_only_the_closest_pending_stop(
    repository, complete_event
) -> None:
    near = _tour_stop(
        complete_event,
        "tour-pgh",
        "Man Man at Mr Smalls",
        "Mr Smalls Theatre",
        venue_latitude=40.50,
        venue_longitude=-79.96,
    )
    far = _tour_stop(
        complete_event,
        "tour-cle",
        "Man Man at Grog Shop",
        "Grog Shop",
        venue_latitude=41.50,
        venue_longitude=-81.58,
    )
    coordless = _tour_stop(
        complete_event, "tour-tbd", "Man Man somewhere", "TBD Hall"
    )
    unrelated = _tour_stop(
        complete_event,
        "solo-1",
        "A Different Band",
        "Elsewhere",
        artist="A Different Band",
        venue_latitude=40.45,
        venue_longitude=-79.99,
    )
    for candidate in (near, far, coordless):
        await repository.upsert_discovered(candidate, _ARTIST_MATCH_SCORE)
    await repository.upsert_discovered(
        unrelated,
        ScoreResult(
            score=30,
            reasons=("genre match: grindcore",),
            affinity_score=30,
            location_bonus=0,
            distance_miles=None,
        ),
    )

    removed = await repository.dedupe_tour_events(_HOME, 350)

    assert removed == 2
    remaining = {
        event.title for event in await repository.list_events(EventStatus.PENDING_REVIEW)
    }
    assert remaining == {"Man Man at Mr Smalls", "A Different Band"}
    # Idempotent: a second pass finds nothing left to trim.
    assert await repository.dedupe_tour_events(_HOME, 350) == 0


@pytest.mark.asyncio
async def test_decided_stop_anchors_tour_dedupe(repository, complete_event) -> None:
    """An approved stop kills farther pending siblings even without an artist match."""
    genre_score = ScoreResult(
        score=34,
        reasons=("genre match: metal, nu metal",),
        affinity_score=15,
        location_bonus=19,
        distance_miles=7.0,
    )

    def stop(source_event_id: str, title: str, venue: str, lat: float, lon: float):
        return _tour_stop(
            complete_event,
            source_event_id,
            title,
            venue,
            artist="Motionless In White",
            venue_latitude=lat,
            venue_longitude=lon,
        )

    local = stop("miw-pgh", "MIW: Sweat and Blood", "PPG Paints Arena", 40.44, -79.99)
    indy = stop("miw-indy", "MIW: Sweat and Blood Indy", "Everwise Amphitheater", 39.76, -86.16)
    raleigh = stop("miw-ral", "MIW: Sweat and Blood Raleigh", "Lenovo Center", 35.80, -78.72)
    second_night = stop("miw-pgh-2", "MIW Night Two", "PPG Paints Arena", 40.44, -79.99)
    approved = (await repository.upsert_discovered(local, genre_score)).event
    for candidate in (indy, raleigh, second_night):
        await repository.upsert_discovered(candidate, genre_score)
    await repository.approve(approved.id, 4)

    removed = await repository.dedupe_tour_events(_HOME, 350)

    assert removed == 2
    remaining = {
        event.title for event in await repository.list_events(EventStatus.PENDING_REVIEW)
    }
    # The farther tour stops are gone; the same-venue second night survives.
    assert remaining == {"MIW Night Two"}


@pytest.mark.asyncio
async def test_published_with_closer_pending_flags_the_mistake(
    repository, complete_event
) -> None:
    far = _tour_stop(
        complete_event,
        "pub-cle",
        "Man Man at Grog Shop",
        "Grog Shop",
        venue_latitude=41.50,
        venue_longitude=-81.58,
    )
    near = _tour_stop(
        complete_event,
        "pend-pgh",
        "Man Man at Mr Smalls",
        "Mr Smalls Theatre",
        venue_latitude=40.50,
        venue_longitude=-79.96,
    )
    published = (await repository.upsert_discovered(far, _ARTIST_MATCH_SCORE)).event
    await repository.upsert_discovered(near, _ARTIST_MATCH_SCORE)
    async with repository.database.connect() as connection:
        await connection.execute(
            "UPDATE events SET status = 'published' WHERE id = ?", (published.id,)
        )
        await connection.commit()

    flagged = await repository.published_with_closer_pending(_HOME)
    assert flagged == [("Man Man at Grog Shop", "Man Man at Mr Smalls")]
    # The published stop is never deleted by tour dedupe, and the single
    # remaining pending stop has no sibling to trim.
    assert await repository.dedupe_tour_events(_HOME, 350) == 0


@pytest.mark.asyncio
async def test_requested_events_jump_the_review_queue(repository, complete_event) -> None:
    high = replace(
        complete_event, source_event_id="q-high", title="High Scorer", venue="Hall A"
    )
    request = replace(
        complete_event,
        source_name="request",
        source_event_id="q-req",
        title="Community Request",
        venue="Hall B",
    )
    await repository.upsert_discovered(
        high,
        ScoreResult(
            score=90,
            reasons=("genre match: metal",),
            affinity_score=90,
            location_bonus=0,
            distance_miles=None,
        ),
    )
    request_record = (
        await repository.upsert_discovered(
            request,
            ScoreResult(
                score=0,
                reasons=("requested by mirvana",),
                affinity_score=0,
                location_bonus=0,
                distance_miles=None,
            ),
        )
    ).event

    queue = await repository.list_review_queue()
    assert [event.title for event in queue] == ["Community Request", "High Scorer"]
    _total, _posted, waiting = await repository.review_queue_snapshot(10)
    assert waiting[0].title == "Community Request"

    # Requests are exempt from tour dedupe: nobody's specific ask gets
    # deleted for being the farther stop.
    assert await repository.dedupe_tour_events(_HOME, 350) == 0

    # And reconcile: clearing a registration by message id makes it repost.
    await repository.set_review_message(request_record.id, 2, 555, "hash")
    assert await repository.clear_review_messages({555}) == 1
    message_id, card_hash = await repository.get_review_sync_state(request_record.id)
    assert message_id is None
    assert card_hash is None


@pytest.mark.asyncio
async def test_rejected_artist_signals_mine_taste_not_logistics(
    repository, complete_event
) -> None:
    genre_score = ScoreResult(
        score=15,
        reasons=("genre match: metal",),
        affinity_score=15,
        location_bonus=0,
        distance_miles=None,
    )

    def show(source_event_id: str, title: str, artist: str):
        return replace(
            complete_event,
            source_event_id=source_event_id,
            title=title,
            artist=artist,
            venue=f"Venue {source_event_id}",
        )

    # Genre-matched rejection: pure taste signal.
    taste = (
        await repository.upsert_discovered(
            show("sig-1", "Cradle of Filth", "Cradle of Filth"), genre_score
        )
    ).event
    # Rejection of an artist-matched event: logistics, not taste.
    logistics = (
        await repository.upsert_discovered(
            show("sig-2", "Boy Harsher", "Boy Harsher"), _ARTIST_MATCH_SCORE
        )
    ).event
    # Genre-matched rejection of an artist with an approved sibling: exempt.
    liked_reject = (
        await repository.upsert_discovered(
            show("sig-3", "Weedeater in Cleveland", "Weedeater"), genre_score
        )
    ).event
    liked_approved = (
        await repository.upsert_discovered(
            show("sig-4", "Weedeater at Reverb", "Weedeater"), genre_score
        )
    ).event
    for event in (taste, logistics, liked_reject):
        await repository.reject(event.id, 4, None)
    await repository.approve(liked_approved.id, 4)

    assert await repository.rejected_artist_signals() == ("Cradle of Filth",)


@pytest.mark.asyncio
async def test_rsvp_reminder_queries(repository, complete_event) -> None:
    from datetime import UTC, datetime

    starts_at = datetime.now(UTC) + timedelta(hours=20)
    soon = replace(complete_event, starts_at=starts_at, ends_at=starts_at + timedelta(hours=3))
    event = (await repository.upsert_discovered(soon, _ARTIST_MATCH_SCORE)).event
    await repository.upsert_rsvp(event.id, 42, "casey", "going")
    await repository.upsert_rsvp(event.id, 43, "sam", "interested")
    await repository.upsert_rsvp(event.id, 44, "kit", "declined")
    async with repository.database.connect() as connection:
        await connection.execute(
            "UPDATE events SET status = 'published' WHERE id = ?", (event.id,)
        )
        await connection.execute(
            "INSERT INTO publications(event_id, state, updated_at) VALUES (?, ?, ?)",
            (event.id, "published", datetime.now(UTC).isoformat()),
        )
        await connection.commit()

    now = datetime.now(UTC)
    due = await repository.list_events_needing_reminder(now, now + timedelta(hours=25))
    assert len(due) == 1
    due_event, user_ids = due[0]
    assert due_event.id == event.id
    assert sorted(user_ids) == [42, 43]  # declined users are not pinged

    await repository.mark_reminder_sent(event.id)
    assert await repository.list_events_needing_reminder(now, now + timedelta(hours=25)) == []
