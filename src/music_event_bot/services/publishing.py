from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from music_event_bot.domain.models import EventRecord, EventStatus
from music_event_bot.domain.normalization import normalize_text
from music_event_bot.storage.repositories import EventRepository

logger = logging.getLogger(__name__)


class PublicationGateway(Protocol):
    async def create_or_find_scheduled_event(self, event: EventRecord) -> int | None: ...

    async def create_or_find_announcement(
        self, event: EventRecord, role_ids: tuple[int, ...], scheduled_event_id: int | None
    ) -> int: ...

    async def update_published_event(
        self,
        event: EventRecord,
        scheduled_event_id: int | None,
        announcement_message_id: int,
        role_ids: tuple[int, ...],
    ) -> None: ...


class PublicationService:
    def __init__(
        self,
        repository: EventRepository,
        gateway: PublicationGateway,
        fallback_role_ids: frozenset[int] = frozenset(),
        catchall_role_ids: frozenset[int] | None = None,
    ) -> None:
        self.repository = repository
        self.gateway = gateway
        # Catch-all buckets ("Other Music", "Other Events") only ping when no
        # specific role matched; otherwise every event tagged "rock" would
        # also ping the catch-all community.
        self.fallback_role_ids = fallback_role_ids
        # When an event matches nothing anywhere, only these roles hear about
        # it (typically just "Other Music", not every catch-all bucket).
        self.catchall_role_ids = (
            catchall_role_ids if catchall_role_ids is not None else fallback_role_ids
        )
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def _roles_for(self, event: EventRecord) -> tuple[int, ...]:
        roles = await self.repository.get_roles_for_genres(event.genres)
        specific = tuple(role for role in roles if role not in self.fallback_role_ids)
        if specific:
            return specific
        # The source's genre labels matched no mapped role. Before settling
        # for the catch-all, let the lineup's cached artist tags vote for
        # genre buckets the same way discovery scoring does.
        artists = {normalize_text(name) for name in (*event.artists, event.artist or "")}
        artists.discard("")
        if artists:
            tags = await self.repository.get_tags_for_artists(artists)
            if tags:
                mappings = await self.repository.get_tag_mappings(tags)
                buckets = tuple(
                    bucket for bucket_list, _broad in mappings.values() for bucket in bucket_list
                )
                bucket_roles = await self.repository.get_roles_for_genres(buckets)
                bucket_specific = tuple(
                    role for role in bucket_roles if role not in self.fallback_role_ids
                )
                if bucket_specific:
                    return bucket_specific
        if roles:
            return roles
        # Nothing matched anywhere: ping the music catch-all rather than
        # publishing silently.
        return tuple(sorted(self.catchall_role_ids))

    async def publish(self, event_id: str) -> EventRecord:
        async with self._locks[event_id]:
            event = await self.repository.get_event(event_id)
            if event is None:
                raise KeyError(f"Unknown event ID: {event_id}")
            publication = await self.repository.begin_publication(event_id)
            try:
                scheduled_id = publication.get("scheduled_event_id")
                if scheduled_id:
                    scheduled_event_id = int(scheduled_id)
                else:
                    # None when the guild is at Discord's scheduled-event cap:
                    # the announcement still goes out, just without the
                    # native event link.
                    scheduled_event_id = await self.gateway.create_or_find_scheduled_event(event)
                    if scheduled_event_id is not None:
                        await self.repository.record_scheduled_event(event_id, scheduled_event_id)

                announcement_id = publication.get("announcement_message_id")
                if announcement_id:
                    announcement_message_id = int(announcement_id)
                else:
                    role_ids = await self._roles_for(event)
                    announcement_message_id = await self.gateway.create_or_find_announcement(
                        event, role_ids, scheduled_event_id
                    )
                    await self.repository.record_announcement(event_id, announcement_message_id)

                await self.repository.mark_published(event_id)
            except Exception as exc:
                await self.repository.mark_publish_failed(event_id, str(exc))
                raise

            published = await self.repository.get_event(event_id)
            if published is None:
                raise RuntimeError("Published event disappeared")
            return published

    async def drain_approved(
        self,
        *,
        limit_per_hour: int,
        start_hour: int,
        end_hour: int,
        timezone: ZoneInfo,
        now: datetime | None = None,
    ) -> int:
        """Publish queued approved events within the hourly ping budget.

        Runs only between start_hour and end_hour local time so nobody is
        pinged overnight. The budget counts everything published in the last
        hour (from any trigger), and the queue drains soonest show first.
        Returns the number of events published this call.
        """
        if limit_per_hour <= 0:
            return 0
        current = (now or datetime.now(UTC)).astimezone(timezone)
        if not start_hour <= current.hour < end_hour:
            return 0
        recent = await self.repository.count_publications_since(current - timedelta(hours=1))
        budget = limit_per_hour - recent
        published = 0
        for event in await self.repository.list_events(EventStatus.APPROVED):
            if published >= budget:
                break
            try:
                await self.publish(event.id)
                published += 1
            except Exception:
                # publish() already marked the event publish_failed; its
                # review card regains a Retry button on the next sync.
                logger.exception("Queued publication failed for event %s", event.id)
        return published

    async def update_existing(self, event_id: str) -> None:
        async with self._locks[event_id]:
            event = await self.repository.get_event(event_id)
            publication = await self.repository.get_publication(event_id)
            if event is None or publication is None:
                return
            scheduled_id = publication.get("scheduled_event_id")
            announcement_id = publication.get("announcement_message_id")
            if not announcement_id:
                return
            role_ids = await self._roles_for(event)
            await self.gateway.update_published_event(
                event,
                int(scheduled_id) if scheduled_id else None,
                int(announcement_id),
                role_ids,
            )
