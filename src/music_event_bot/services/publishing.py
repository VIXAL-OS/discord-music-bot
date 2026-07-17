from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from music_event_bot.domain.models import EventRecord, EventStatus
from music_event_bot.storage.repositories import EventRepository

logger = logging.getLogger(__name__)


class PublicationGateway(Protocol):
    async def create_or_find_scheduled_event(self, event: EventRecord) -> int: ...

    async def create_or_find_announcement(
        self, event: EventRecord, role_ids: tuple[int, ...], scheduled_event_id: int
    ) -> int: ...

    async def update_published_event(
        self,
        event: EventRecord,
        scheduled_event_id: int,
        announcement_message_id: int,
        role_ids: tuple[int, ...],
    ) -> None: ...


class PublicationService:
    def __init__(
        self,
        repository: EventRepository,
        gateway: PublicationGateway,
        fallback_role_ids: frozenset[int] = frozenset(),
    ) -> None:
        self.repository = repository
        self.gateway = gateway
        # Catch-all buckets ("Other Music", "Other Events") only ping when no
        # specific role matched; otherwise every event tagged "rock" would
        # also ping the catch-all community.
        self.fallback_role_ids = fallback_role_ids
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def _roles_for(self, event: EventRecord) -> tuple[int, ...]:
        roles = await self.repository.get_roles_for_genres(event.genres)
        specific = tuple(role for role in roles if role not in self.fallback_role_ids)
        return specific or roles

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
                    scheduled_event_id = await self.gateway.create_or_find_scheduled_event(event)
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
            if not scheduled_id or not announcement_id:
                return
            role_ids = await self._roles_for(event)
            await self.gateway.update_published_event(
                event,
                int(scheduled_id),
                int(announcement_id),
                role_ids,
            )
