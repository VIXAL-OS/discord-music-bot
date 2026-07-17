from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite

from music_event_bot.domain.geography import GeoPoint
from music_event_bot.domain.models import (
    DiscoveredEvent,
    EventRecord,
    EventStatus,
    ScoreResult,
    TasteProfile,
    UpsertResult,
)
from music_event_bot.domain.normalization import (
    canonical_fingerprint,
    normalize_genres,
    normalize_text,
    normalize_url,
)
from music_event_bot.domain.scoring import score_event
from music_event_bot.storage.database import Database


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _coordinates_from_ticketmaster_raw(raw: Any) -> GeoPoint | None:
    if not isinstance(raw, dict):
        return None
    embedded = raw.get("_embedded", {})
    if not isinstance(embedded, dict):
        return None
    venues = embedded.get("venues", [])
    if not venues or not isinstance(venues[0], dict):
        return None
    location = venues[0].get("location", {})
    if not isinstance(location, dict):
        return None
    latitude = location.get("latitude")
    longitude = location.get("longitude")
    if latitude is None or longitude is None:
        return None
    try:
        return GeoPoint(float(latitude), float(longitude))
    except (TypeError, ValueError):
        return None


def _event_from_row(row: aiosqlite.Row) -> EventRecord:
    return EventRecord(
        id=row["id"],
        title=row["title"],
        artist=row["artist"],
        artists=tuple(json.loads(row["artists_json"])),
        venue=row["venue"],
        location=row["location"],
        starts_at=_parse_datetime(row["starts_at"]),
        ends_at=_parse_datetime(row["ends_at"]),
        timezone=row["timezone"],
        url=row["url"],
        image_url=row["image_url"],
        description=row["description"],
        genres=tuple(json.loads(row["genres_json"])),
        status=EventStatus(row["status"]),
        score=int(row["score"]),
        match_reasons=tuple(json.loads(row["match_reasons_json"])),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        venue_latitude=row["venue_latitude"],
        venue_longitude=row["venue_longitude"],
    )


class EventRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def upsert_discovered(
        self, event: DiscoveredEvent, score: ScoreResult
    ) -> UpsertResult:
        now = _now()
        fingerprint = canonical_fingerprint(
            event.title,
            event.venue,
            event.starts_at,
            source_name=event.source_name,
            source_event_id=event.source_event_id,
        )
        genres = normalize_genres(event.genres)
        incomplete = set(event.incomplete_reasons)
        if event.starts_at is None:
            incomplete.add("missing start time")
        if not event.venue:
            incomplete.add("missing venue")
        if not event.location:
            incomplete.add("missing location")
        desired_status = (
            EventStatus.INCOMPLETE if incomplete else EventStatus.PENDING_REVIEW
        )

        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """
                    SELECT e.* FROM events e
                    JOIN event_sources s ON s.event_id = e.id
                    WHERE s.source_name = ? AND s.source_event_id = ?
                    """,
                    (event.source_name, event.source_event_id),
                )
                existing = await cursor.fetchone()
                created = False
                source_created = False

                if existing:
                    event_id = existing["id"]
                    await connection.execute(
                        """
                        UPDATE event_sources
                        SET source_url = ?, raw_json = ?, last_observed_at = ?
                        WHERE source_name = ? AND source_event_id = ?
                        """,
                        (
                            normalize_url(event.source_url),
                            json.dumps(event.raw, sort_keys=True, default=str),
                            now.isoformat(),
                            event.source_name,
                            event.source_event_id,
                        ),
                    )
                    if existing["status"] in {
                        EventStatus.DISCOVERED.value,
                        EventStatus.PENDING_REVIEW.value,
                        EventStatus.INCOMPLETE.value,
                    }:
                        await self._update_discovered_fields(
                            connection, event_id, event, genres, score, desired_status, now
                        )
                else:
                    cursor = await connection.execute(
                        "SELECT * FROM events WHERE fingerprint = ?", (fingerprint,)
                    )
                    canonical = await cursor.fetchone()
                    if canonical:
                        event_id = canonical["id"]
                        if canonical["status"] in {
                            EventStatus.DISCOVERED.value,
                            EventStatus.PENDING_REVIEW.value,
                            EventStatus.INCOMPLETE.value,
                        }:
                            await self._fill_missing_fields(
                                connection, event_id, event, genres, score, desired_status, now
                            )
                    else:
                        event_id = str(uuid.uuid4())
                        await connection.execute(
                            """
                            INSERT INTO events(
                                id, fingerprint, title, title_normalized, artist,
                                artists_json, venue,
                                venue_normalized, location, starts_at, ends_at, timezone,
                                url, image_url, description, genres_json, status, score,
                                match_reasons_json, created_at, updated_at,
                                venue_latitude, venue_longitude
                            ) VALUES (
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                            )
                            """,
                            (
                                event_id,
                                fingerprint,
                                event.title.strip(),
                                normalize_text(event.title),
                                event.artist,
                                json.dumps(list(event.artists)),
                                event.venue,
                                normalize_text(event.venue),
                                event.location,
                                _iso(event.starts_at),
                                _iso(event.ends_at),
                                event.timezone,
                                normalize_url(event.source_url),
                                normalize_url(event.image_url),
                                event.description,
                                json.dumps(genres),
                                desired_status.value,
                                score.score,
                                json.dumps(score.reasons),
                                now.isoformat(),
                                now.isoformat(),
                                event.venue_latitude,
                                event.venue_longitude,
                            ),
                        )
                        created = True

                    await connection.execute(
                        """
                        INSERT INTO event_sources(
                            event_id, source_name, source_event_id, source_url,
                            raw_json, last_observed_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            event.source_name,
                            event.source_event_id,
                            normalize_url(event.source_url),
                            json.dumps(event.raw, sort_keys=True, default=str),
                            now.isoformat(),
                        ),
                    )
                    source_created = True

                await connection.execute(
                    """
                    INSERT INTO reviews(event_id, updated_at)
                    VALUES (?, ?)
                    ON CONFLICT(event_id) DO NOTHING
                    """,
                    (event_id, now.isoformat()),
                )
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise

        stored = await self.get_event(event_id)
        if stored is None:
            raise RuntimeError(f"Event {event_id} disappeared after upsert")
        return UpsertResult(stored, created=created, source_created=source_created)

    async def _update_discovered_fields(
        self,
        connection: aiosqlite.Connection,
        event_id: str,
        event: DiscoveredEvent,
        genres: tuple[str, ...],
        score: ScoreResult,
        status: EventStatus,
        now: datetime,
    ) -> None:
        await connection.execute(
            """
            UPDATE events SET
                title = ?, title_normalized = ?, artist = ?, artists_json = ?, venue = ?,
                venue_normalized = ?, location = ?, starts_at = ?, ends_at = ?,
                timezone = ?, url = ?, image_url = ?, description = ?, genres_json = ?,
                venue_latitude = COALESCE(?, venue_latitude),
                venue_longitude = COALESCE(?, venue_longitude),
                status = ?, score = ?, match_reasons_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                event.title.strip(),
                normalize_text(event.title),
                event.artist,
                json.dumps(list(event.artists)),
                event.venue,
                normalize_text(event.venue),
                event.location,
                _iso(event.starts_at),
                _iso(event.ends_at),
                event.timezone,
                normalize_url(event.source_url),
                normalize_url(event.image_url),
                event.description,
                json.dumps(genres),
                event.venue_latitude,
                event.venue_longitude,
                status.value,
                score.score,
                json.dumps(score.reasons),
                now.isoformat(),
                event_id,
            ),
        )

    async def _fill_missing_fields(
        self,
        connection: aiosqlite.Connection,
        event_id: str,
        event: DiscoveredEvent,
        genres: tuple[str, ...],
        score: ScoreResult,
        status: EventStatus,
        now: datetime,
    ) -> None:
        await connection.execute(
            """
            UPDATE events SET
                artist = COALESCE(artist, ?),
                artists_json = CASE WHEN artists_json = '[]' THEN ? ELSE artists_json END,
                venue = COALESCE(venue, ?),
                venue_normalized = CASE WHEN venue_normalized = '' THEN ? ELSE venue_normalized END,
                location = COALESCE(location, ?),
                starts_at = COALESCE(starts_at, ?),
                ends_at = COALESCE(ends_at, ?),
                timezone = COALESCE(timezone, ?),
                url = COALESCE(url, ?),
                image_url = COALESCE(image_url, ?),
                description = COALESCE(description, ?),
                venue_latitude = COALESCE(venue_latitude, ?),
                venue_longitude = COALESCE(venue_longitude, ?),
                genres_json = CASE WHEN genres_json = '[]' THEN ? ELSE genres_json END,
                status = CASE WHEN status = 'incomplete' THEN ? ELSE status END,
                score = MAX(score, ?),
                match_reasons_json = CASE WHEN score < ? THEN ? ELSE match_reasons_json END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                event.artist,
                json.dumps(list(event.artists)),
                event.venue,
                normalize_text(event.venue),
                event.location,
                _iso(event.starts_at),
                _iso(event.ends_at),
                event.timezone,
                normalize_url(event.source_url),
                normalize_url(event.image_url),
                event.description,
                event.venue_latitude,
                event.venue_longitude,
                json.dumps(genres),
                status.value,
                score.score,
                score.score,
                json.dumps(score.reasons),
                now.isoformat(),
                event_id,
            ),
        )

    async def get_event(self, event_id: str) -> EventRecord | None:
        async with self.database.connect() as connection:
            cursor = await connection.execute("SELECT * FROM events WHERE id = ?", (event_id,))
            row = await cursor.fetchone()
            return _event_from_row(row) if row else None

    async def list_events(self, *statuses: EventStatus) -> list[EventRecord]:
        async with self.database.connect() as connection:
            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                cursor = await connection.execute(
                    f"SELECT * FROM events WHERE status IN ({placeholders}) "
                    "ORDER BY starts_at IS NULL, starts_at",
                    tuple(status.value for status in statuses),
                )
            else:
                cursor = await connection.execute(
                    "SELECT * FROM events ORDER BY starts_at IS NULL, starts_at"
                )
            return [_event_from_row(row) for row in await cursor.fetchall()]

    async def list_review_queue(self) -> list[EventRecord]:
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM events
                WHERE status IN ('pending_review', 'incomplete', 'publish_failed')
                ORDER BY score DESC, starts_at IS NULL, starts_at, id
                """
            )
            return [_event_from_row(row) for row in await cursor.fetchall()]

    async def list_review_registrations(self) -> list[tuple[str, int, int]]:
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                """
                SELECT r.event_id, r.review_channel_id, r.review_message_id
                FROM reviews r JOIN events e ON e.id = r.event_id
                WHERE r.review_message_id IS NOT NULL
                  AND e.status IN ('pending_review', 'incomplete', 'publish_failed')
                """
            )
            return [
                (row["event_id"], int(row["review_channel_id"]), int(row["review_message_id"]))
                for row in await cursor.fetchall()
            ]

    async def get_review_message_id(self, event_id: str) -> int | None:
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                "SELECT review_message_id FROM reviews WHERE event_id = ?", (event_id,)
            )
            row = await cursor.fetchone()
            return int(row["review_message_id"]) if row and row["review_message_id"] else None

    async def upsert_rsvp(
        self, event_id: str, user_id: int, display_name: str, state: str
    ) -> None:
        if state not in ("going", "interested", "declined"):
            raise ValueError(f"Unknown RSVP state: {state}")
        now = _now().isoformat()
        async with self.database.connect() as connection:
            await connection.execute(
                """
                INSERT INTO rsvps(event_id, user_id, display_name, state, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(event_id, user_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    state = excluded.state,
                    updated_at = excluded.updated_at
                """,
                (event_id, str(user_id), display_name, state, now),
            )
            await connection.commit()

    async def get_rsvps(self, event_id: str) -> dict[str, list[str]]:
        """Display names grouped by RSVP state, earliest responders first."""
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                "SELECT display_name, state FROM rsvps WHERE event_id = ? ORDER BY updated_at",
                (event_id,),
            )
            groups: dict[str, list[str]] = {}
            for row in await cursor.fetchall():
                groups.setdefault(row["state"], []).append(row["display_name"])
            return groups

    async def list_announcement_registrations(self) -> list[tuple[str, int]]:
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                """
                SELECT p.event_id, p.announcement_message_id
                FROM publications p JOIN events e ON e.id = p.event_id
                WHERE p.announcement_message_id IS NOT NULL AND e.status = 'published'
                """
            )
            return [
                (row["event_id"], int(row["announcement_message_id"]))
                for row in await cursor.fetchall()
            ]

    async def count_publications_since(self, cutoff: datetime) -> int:
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                "SELECT COUNT(*) AS count FROM publications "
                "WHERE state = 'published' AND updated_at >= ?",
                (cutoff.astimezone(UTC).isoformat(),),
            )
            row = await cursor.fetchone()
            return int(row["count"]) if row else 0

    async def get_review_sync_state(self, event_id: str) -> tuple[int | None, str | None]:
        """Return the posted review message ID and the card hash it carries."""
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                "SELECT review_message_id, card_hash FROM reviews WHERE event_id = ?",
                (event_id,),
            )
            row = await cursor.fetchone()
            if row is None or not row["review_message_id"]:
                return None, None
            return int(row["review_message_id"]), row["card_hash"]

    async def set_review_message(
        self,
        event_id: str,
        channel_id: int,
        message_id: int,
        card_hash: str | None = None,
    ) -> None:
        now = _now().isoformat()
        async with self.database.connect() as connection:
            await connection.execute(
                """
                INSERT INTO reviews(
                    event_id, review_channel_id, review_message_id, notified_at,
                    card_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    review_channel_id = excluded.review_channel_id,
                    review_message_id = excluded.review_message_id,
                    notified_at = COALESCE(reviews.notified_at, excluded.notified_at),
                    card_hash = excluded.card_hash,
                    updated_at = excluded.updated_at
                """,
                (event_id, str(channel_id), str(message_id), now, card_hash, now),
            )
            await connection.commit()

    async def update_event(self, event_id: str, **fields: Any) -> EventRecord:
        allowed = {
            "title",
            "artist",
            "venue",
            "location",
            "starts_at",
            "ends_at",
            "timezone",
            "url",
            "image_url",
            "description",
            "genres",
        }
        invalid = set(fields) - allowed
        if invalid:
            raise ValueError(f"Unsupported event fields: {', '.join(sorted(invalid))}")

        values: dict[str, Any] = {}
        for key, value in fields.items():
            if key in {"starts_at", "ends_at"}:
                if value is not None and not isinstance(value, datetime):
                    raise TypeError(f"{key} must be datetime or None")
                values[key] = _iso(value)
            elif key == "genres":
                values["genres_json"] = json.dumps(normalize_genres(tuple(value)))
            elif key in {"url", "image_url"}:
                values[key] = normalize_url(value)
            else:
                values[key] = value
        if "title" in values:
            values["title_normalized"] = normalize_text(values["title"])
        if "venue" in values:
            values["venue_normalized"] = normalize_text(values["venue"])
        values["updated_at"] = _now().isoformat()

        assignments = ", ".join(f"{key} = ?" for key in values)
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                f"UPDATE events SET {assignments} WHERE id = ?",
                (*values.values(), event_id),
            )
            if cursor.rowcount != 1:
                await connection.rollback()
                raise KeyError(f"Unknown event ID: {event_id}")
            cursor = await connection.execute("SELECT * FROM events WHERE id = ?", (event_id,))
            row = await cursor.fetchone()
            if row is None:
                await connection.rollback()
                raise KeyError(f"Unknown event ID: {event_id}")
            record = _event_from_row(row)
            if record.status == EventStatus.INCOMPLETE and record.is_complete:
                await connection.execute(
                    "UPDATE events SET status = ? WHERE id = ?",
                    (EventStatus.PENDING_REVIEW.value, event_id),
                )
            await connection.commit()
        updated = await self.get_event(event_id)
        if updated is None:
            raise RuntimeError("Updated event disappeared")
        return updated

    async def approve(self, event_id: str, reviewer_id: int) -> EventRecord:
        now = _now().isoformat()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute("SELECT * FROM events WHERE id = ?", (event_id,))
            row = await cursor.fetchone()
            if row is None:
                await connection.rollback()
                raise KeyError(f"Unknown event ID: {event_id}")
            event = _event_from_row(row)
            if not event.is_complete:
                await connection.rollback()
                raise ValueError(
                    "Event needs a title, venue, location, and start time before approval"
                )
            if event.status not in {
                EventStatus.PENDING_REVIEW,
                EventStatus.APPROVED,
                EventStatus.PUBLISH_FAILED,
                EventStatus.PUBLISHED,
            }:
                await connection.rollback()
                raise ValueError(f"Event cannot be approved from state {event.status}")
            if event.status != EventStatus.PUBLISHED:
                await connection.execute(
                    "UPDATE events SET status = ?, updated_at = ? WHERE id = ?",
                    (EventStatus.APPROVED.value, now, event_id),
                )
            await connection.execute(
                """
                UPDATE reviews SET decision = 'approved', reviewer_id = ?,
                    decided_at = ?, updated_at = ? WHERE event_id = ?
                """,
                (str(reviewer_id), now, now, event_id),
            )
            await connection.commit()
        approved = await self.get_event(event_id)
        if approved is None:
            raise RuntimeError("Approved event disappeared")
        return approved

    async def reject(self, event_id: str, reviewer_id: int, reason: str | None = None) -> None:
        now = _now().isoformat()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                """
                UPDATE events SET status = ?, updated_at = ?
                WHERE id = ? AND status IN ('pending_review', 'incomplete', 'publish_failed')
                """,
                (EventStatus.REJECTED.value, now, event_id),
            )
            if cursor.rowcount != 1:
                await connection.rollback()
                raise ValueError("Event does not exist or cannot be rejected in its current state")
            await connection.execute(
                """
                UPDATE reviews SET decision = 'rejected', reviewer_id = ?, reason = ?,
                    decided_at = ?, updated_at = ? WHERE event_id = ?
                """,
                (str(reviewer_id), reason, now, now, event_id),
            )
            await connection.commit()

    async def begin_publication(self, event_id: str) -> dict[str, Any]:
        now = _now().isoformat()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute("SELECT status FROM events WHERE id = ?", (event_id,))
            row = await cursor.fetchone()
            if row is None:
                await connection.rollback()
                raise KeyError(f"Unknown event ID: {event_id}")
            if row["status"] not in {
                EventStatus.APPROVED.value,
                EventStatus.PUBLISH_FAILED.value,
                EventStatus.PUBLISHED.value,
            }:
                await connection.rollback()
                raise ValueError(f"Event is not approved for publication: {row['status']}")
            await connection.execute(
                """
                INSERT INTO publications(event_id, state, attempts, updated_at)
                VALUES (?, 'publishing', 1, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    state = 'publishing', attempts = publications.attempts + 1,
                    last_error = NULL, updated_at = excluded.updated_at
                """,
                (event_id, now),
            )
            cursor = await connection.execute(
                "SELECT * FROM publications WHERE event_id = ?", (event_id,)
            )
            publication = dict(await cursor.fetchone())
            await connection.commit()
            return publication

    async def record_scheduled_event(self, event_id: str, scheduled_event_id: int) -> None:
        await self._update_publication(
            event_id, scheduled_event_id=str(scheduled_event_id), state="scheduled_event_created"
        )

    async def record_announcement(self, event_id: str, message_id: int) -> None:
        await self._update_publication(
            event_id, announcement_message_id=str(message_id), state="announcement_created"
        )

    async def mark_published(self, event_id: str) -> None:
        now = _now().isoformat()
        async with self.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                """
                SELECT scheduled_event_id, announcement_message_id
                FROM publications WHERE event_id = ?
                """,
                (event_id,),
            )
            row = await cursor.fetchone()
            if row is None or not row["scheduled_event_id"] or not row["announcement_message_id"]:
                await connection.rollback()
                raise ValueError("Publication is missing its Scheduled Event or announcement")
            await connection.execute(
                "UPDATE publications SET state = 'published', updated_at = ? WHERE event_id = ?",
                (now, event_id),
            )
            await connection.execute(
                "UPDATE events SET status = ?, updated_at = ? WHERE id = ?",
                (EventStatus.PUBLISHED.value, now, event_id),
            )
            await connection.commit()

    async def mark_publish_failed(self, event_id: str, error: str) -> None:
        now = _now().isoformat()
        async with self.database.connect() as connection:
            await connection.execute(
                """
                INSERT INTO publications(event_id, state, attempts, last_error, updated_at)
                VALUES (?, 'failed', 1, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    state = 'failed', last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (event_id, error[:2000], now),
            )
            await connection.execute(
                "UPDATE events SET status = ?, updated_at = ? WHERE id = ?",
                (EventStatus.PUBLISH_FAILED.value, now, event_id),
            )
            await connection.commit()

    async def _update_publication(self, event_id: str, **values: str) -> None:
        values["updated_at"] = _now().isoformat()
        assignments = ", ".join(f"{key} = ?" for key in values)
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                f"UPDATE publications SET {assignments} WHERE event_id = ?",
                (*values.values(), event_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Publication not initialized for event {event_id}")
            await connection.commit()

    async def get_publication(self, event_id: str) -> dict[str, Any] | None:
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                "SELECT * FROM publications WHERE event_id = ?", (event_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def set_genre_role(self, genre: str, role_id: int) -> None:
        normalized = normalize_text(genre)
        now = _now().isoformat()
        async with self.database.connect() as connection:
            await connection.execute(
                """
                INSERT INTO genre_roles(genre, role_id, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(genre) DO UPDATE SET role_id = excluded.role_id,
                    updated_at = excluded.updated_at
                """,
                (normalized, str(role_id), now),
            )
            await connection.commit()

    async def seed_genre_roles(self, mappings: dict[str, int]) -> None:
        for genre, role_id in mappings.items():
            await self.set_genre_role(genre, role_id)

    async def seed_genre_role_aliases(self, role_map: dict[str, int]) -> int:
        """Alias mapped genre tags onto bucket roles for announcement pings.

        Every cached tag mapping that points at exactly one Discord bucket
        becomes a genre_roles row (e.g. "dance electronic" -> the EDM role),
        so Ticketmaster genre names resolve to roles the way bucket names do.
        Explicit rows — settings seeds and /event set-role — are never
        overwritten, and ambiguous multi-bucket tags are skipped.
        """
        if not role_map:
            return 0
        now = _now().isoformat()
        added = 0
        async with self.database.connect() as connection:
            cursor = await connection.execute("SELECT tag, buckets_json FROM genre_tag_map")
            rows = await cursor.fetchall()
            for row in rows:
                buckets = json.loads(row["buckets_json"])
                if len(buckets) != 1:
                    continue
                role_id = role_map.get(str(buckets[0]))
                if role_id is None:
                    continue
                genre_key = normalize_text(row["tag"])
                if not genre_key:
                    continue
                insert_cursor = await connection.execute(
                    """
                    INSERT INTO genre_roles(genre, role_id, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(genre) DO NOTHING
                    """,
                    (genre_key, str(role_id), now),
                )
                added += insert_cursor.rowcount
            await connection.commit()
        return added

    async def get_roles_for_genres(self, genres: tuple[str, ...]) -> tuple[int, ...]:
        """Return every role the event's genres point at, best match first.

        Events carry several genres, so a crossover bill can legitimately
        belong to more than one community role. Distinct roles are ordered by
        how many genres voted for them; ties go to the earliest-listed genre.
        """
        normalized = [normalize_text(genre) for genre in genres]
        if not normalized:
            return ()
        votes: dict[int, list[int]] = {}
        async with self.database.connect() as connection:
            for position, genre in enumerate(normalized):
                cursor = await connection.execute(
                    "SELECT role_id FROM genre_roles WHERE genre = ?", (genre,)
                )
                row = await cursor.fetchone()
                if row:
                    role_id = int(row["role_id"])
                    entry = votes.setdefault(role_id, [0, position])
                    entry[0] += 1
        ranked = sorted(votes.items(), key=lambda item: (-item[1][0], item[1][1]))
        return tuple(role_id for role_id, _ in ranked)

    async def store_taste_preferences(self, kind: str, values: set[str], source: str) -> None:
        """Merge preference values for a kind/source; existing rows persist."""
        now = _now().isoformat()
        async with self.database.connect() as connection:
            for value in values:
                await connection.execute(
                    """
                    INSERT INTO taste_preferences(kind, value, source, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(kind, value, source) DO UPDATE SET
                        updated_at = excluded.updated_at
                    """,
                    (kind, value, source, now),
                )
            await connection.commit()

    async def get_taste_preferences(self, kind: str, source: str) -> tuple[str, ...]:
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                "SELECT value FROM taste_preferences WHERE kind = ? AND source = ?",
                (kind, source),
            )
            return tuple(sorted(row["value"] for row in await cursor.fetchall()))

    async def latest_taste_preference_update(self, source: str) -> datetime | None:
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                "SELECT MAX(updated_at) AS latest FROM taste_preferences WHERE source = ?",
                (source,),
            )
            row = await cursor.fetchone()
            if row is None or row["latest"] is None:
                return None
            return datetime.fromisoformat(row["latest"])

    async def get_artist_tag_freshness(
        self, source: str = "lastfm"
    ) -> dict[str, tuple[datetime, int]]:
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                """
                SELECT artist_normalized, fetched_at, tag_count
                FROM artist_tag_fetches WHERE source = ?
                """,
                (source,),
            )
            return {
                row["artist_normalized"]: (
                    datetime.fromisoformat(row["fetched_at"]),
                    int(row["tag_count"]),
                )
                for row in await cursor.fetchall()
            }

    async def store_artist_tags(
        self,
        artist_normalized: str,
        tags: list[tuple[str, int]],
        source: str = "lastfm",
    ) -> None:
        """Merge newly observed tags into the artist's stored set.

        Tags accumulate across fetches so an artist's history is never
        forgotten when their community tags evolve; only weights update.
        """
        now = _now().isoformat()
        async with self.database.connect() as connection:
            for tag, weight in tags:
                await connection.execute(
                    """
                    INSERT INTO artist_tags(artist_normalized, tag, weight, source, fetched_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(artist_normalized, tag, source) DO UPDATE SET
                        weight = excluded.weight,
                        fetched_at = excluded.fetched_at
                    """,
                    (artist_normalized, tag, weight, source, now),
                )
            cursor = await connection.execute(
                "SELECT COUNT(*) AS count FROM artist_tags "
                "WHERE artist_normalized = ? AND source = ?",
                (artist_normalized, source),
            )
            row = await cursor.fetchone()
            total_tags = int(row["count"]) if row else 0
            await connection.execute(
                """
                INSERT INTO artist_tag_fetches(artist_normalized, source, fetched_at, tag_count)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(artist_normalized, source) DO UPDATE SET
                    fetched_at = excluded.fetched_at,
                    tag_count = excluded.tag_count
                """,
                (artist_normalized, source, now, total_tags),
            )
            await connection.commit()

    async def get_tags_for_artists(
        self, artists_normalized: set[str], source: str | None = None
    ) -> set[str]:
        async with self.database.connect() as connection:
            if source is None:
                cursor = await connection.execute(
                    "SELECT artist_normalized, tag FROM artist_tags"
                )
            else:
                cursor = await connection.execute(
                    "SELECT artist_normalized, tag FROM artist_tags WHERE source = ?",
                    (source,),
                )
            return {
                row["tag"]
                for row in await cursor.fetchall()
                if row["artist_normalized"] in artists_normalized
            }

    async def get_tag_mappings(
        self, tags: set[str]
    ) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                "SELECT tag, buckets_json, broad_genres_json FROM genre_tag_map"
            )
            return {
                row["tag"]: (
                    tuple(json.loads(row["buckets_json"])),
                    tuple(json.loads(row["broad_genres_json"])),
                )
                for row in await cursor.fetchall()
                if row["tag"] in tags
            }

    async def store_tag_mappings(
        self,
        mappings: dict[str, tuple[tuple[str, ...], tuple[str, ...]]],
        model: str,
    ) -> None:
        now = _now().isoformat()
        async with self.database.connect() as connection:
            for tag, (buckets, broad_genres) in mappings.items():
                await connection.execute(
                    """
                    INSERT INTO genre_tag_map(
                        tag, buckets_json, broad_genres_json, model, mapped_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(tag) DO UPDATE SET
                        buckets_json = excluded.buckets_json,
                        broad_genres_json = excluded.broad_genres_json,
                        model = excluded.model,
                        mapped_at = excluded.mapped_at
                    """,
                    (
                        tag,
                        json.dumps(sorted(buckets)),
                        json.dumps(sorted(broad_genres)),
                        model,
                        now,
                    ),
                )
            await connection.commit()

    async def backfill_ticketmaster_coordinates(self) -> int:
        updated = 0
        now = _now().isoformat()
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                """
                SELECT e.id, s.raw_json
                FROM events e
                JOIN event_sources s ON s.event_id = e.id
                WHERE s.source_name = 'ticketmaster'
                  AND (e.venue_latitude IS NULL OR e.venue_longitude IS NULL)
                """
            )
            for row in await cursor.fetchall():
                try:
                    raw = json.loads(row["raw_json"])
                except (TypeError, json.JSONDecodeError):
                    continue
                coordinates = _coordinates_from_ticketmaster_raw(raw)
                if coordinates is None:
                    continue
                result = await connection.execute(
                    """
                    UPDATE events
                    SET venue_latitude = ?, venue_longitude = ?, updated_at = ?
                    WHERE id = ?
                      AND (venue_latitude IS NULL OR venue_longitude IS NULL)
                    """,
                    (
                        coordinates.latitude,
                        coordinates.longitude,
                        now,
                        row["id"],
                    ),
                )
                updated += result.rowcount
            await connection.commit()
        return updated

    async def rescore_reviewable_events(
        self,
        profile: TasteProfile,
        home: GeoPoint,
        max_travel_radius_miles: int,
    ) -> int:
        statuses = (
            EventStatus.PENDING_REVIEW.value,
            EventStatus.INCOMPLETE.value,
            EventStatus.PUBLISH_FAILED.value,
        )
        now = _now().isoformat()
        updated = 0
        async with self.database.connect() as connection:
            placeholders = ",".join("?" for _ in statuses)
            cursor = await connection.execute(
                f"SELECT * FROM events WHERE status IN ({placeholders})", statuses
            )
            for row in await cursor.fetchall():
                event = _event_from_row(row)
                score = score_event(
                    event,
                    profile,
                    home=home,
                    max_travel_radius_miles=max_travel_radius_miles,
                )
                result = await connection.execute(
                    """
                    UPDATE events
                    SET score = ?, match_reasons_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (score.score, json.dumps(score.reasons), now, event.id),
                )
                updated += result.rowcount
            await connection.commit()
        return updated

    async def record_job_run(
        self,
        job_name: str,
        started_at: datetime,
        status: str,
        details: dict[str, Any],
    ) -> None:
        now = _now().isoformat()
        async with self.database.connect() as connection:
            await connection.execute(
                """
                INSERT INTO job_runs(job_name, started_at, finished_at, status, details_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    job_name,
                    started_at.astimezone(UTC).isoformat(),
                    now,
                    status,
                    json.dumps(details, sort_keys=True, default=str),
                ),
            )
            await connection.commit()

    async def expire_past_events(self, grace: timedelta = timedelta(hours=12)) -> int:
        cutoff = (_now() - grace).isoformat()
        async with self.database.connect() as connection:
            cursor = await connection.execute(
                """
                UPDATE events SET status = ?, updated_at = ?
                WHERE starts_at < ?
                  AND status IN ('discovered', 'pending_review', 'incomplete', 'approved')
                """,
                (EventStatus.EXPIRED.value, _now().isoformat(), cutoff),
            )
            await connection.commit()
            return cursor.rowcount

    async def health(self) -> dict[str, Any]:
        async with self.database.connect() as connection:
            cursor = await connection.execute("SELECT COUNT(*) AS count FROM events")
            event_count = int((await cursor.fetchone())["count"])
            cursor = await connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            )
            schema_version = int((await cursor.fetchone())["version"])
            cursor = await connection.execute(
                """
                SELECT job_name, status, finished_at FROM job_runs
                WHERE id IN (SELECT MAX(id) FROM job_runs GROUP BY job_name)
                """
            )
            jobs = [dict(row) for row in await cursor.fetchall()]
            cursor = await connection.execute(
                "SELECT COUNT(*) AS count FROM artist_tag_fetches"
            )
            tagged_artist_count = int((await cursor.fetchone())["count"])
            cursor = await connection.execute("SELECT COUNT(*) AS count FROM genre_tag_map")
            mapped_tag_count = int((await cursor.fetchone())["count"])
            return {
                "database": str(self.database.path),
                "schema_version": schema_version,
                "event_count": event_count,
                "latest_jobs": jobs,
                "tagged_artist_count": tagged_artist_count,
                "mapped_tag_count": mapped_tag_count,
            }
