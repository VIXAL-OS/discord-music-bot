from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Protocol

from music_event_bot.domain.models import EventRecord
from music_event_bot.storage.repositories import EventRepository


class PublicationGateway(Protocol):
    async def create_or_find_scheduled_event(self, event: EventRecord) -> int: ...

    async def create_or_find_announcement(
        self, event: EventRecord, role_id: int | None
    ) -> int: ...

    async def update_published_event(
        self,
        event: EventRecord,
        scheduled_event_id: int,
        announcement_message_id: int,
        role_id: int | None,
    ) -> None: ...


class PublicationService:
    def __init__(self, repository: EventRepository, gateway: PublicationGateway) -> None:
        self.repository = repository
        self.gateway = gateway
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

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
                    role_id = await self.repository.get_role_for_genres(event.genres)
                    announcement_message_id = await self.gateway.create_or_find_announcement(
                        event, role_id
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
            role_id = await self.repository.get_role_for_genres(event.genres)
            await self.gateway.update_published_event(
                event,
                int(scheduled_id),
                int(announcement_id),
                role_id,
            )
