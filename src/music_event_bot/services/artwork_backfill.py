"""Find artwork for events that were stored before any resolver could run.

Discovery only resolves artwork for events it is upserting right then, so every
event stored before that step existed keeps a blank embed forever -- nothing
revisits it. This walks the stored events instead of a feed, so a backlog can be
filled in without waiting for a source to re-list a show it has already dropped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from music_event_bot.discovery.artwork import ArtworkResolver, candidate_pages
from music_event_bot.domain.models import EventRecord, EventStatus
from music_event_bot.storage.repositories import EventRepository

logger = logging.getLogger(__name__)

# Expired and rejected events are never rendered again, so their art is wasted work.
_DEFAULT_STATUSES = (
    EventStatus.PUBLISHED,
    EventStatus.APPROVED,
    EventStatus.PENDING_REVIEW,
    EventStatus.INCOMPLETE,
    EventStatus.PUBLISH_FAILED,
)


@dataclass(frozen=True, slots=True)
class BackfillSummary:
    scanned: int
    without_image: int
    resolved: int
    applied: int
    shared_art_skipped: int
    matches: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)


class ArtworkBackfill:
    def __init__(
        self,
        repository: EventRepository,
        artwork: ArtworkResolver | None = None,
    ) -> None:
        self.repository = repository
        self.artwork = artwork or ArtworkResolver()

    async def run(
        self,
        *,
        statuses: tuple[EventStatus, ...] = _DEFAULT_STATUSES,
        apply: bool = False,
    ) -> BackfillSummary:
        events = await self.repository.list_events(*statuses)
        blank = [event for event in events if not event.image_url]

        # Resolve everything before writing anything. A venue that puts one house
        # image on every event page only reveals itself once a second event claims
        # the same file, which is too late if the first was already committed.
        found: list[tuple[EventRecord, str]] = []
        for event in blank:
            image = await self.artwork.resolve_pages(_pages_for(event))
            if image:
                found.append((event, image))

        shared = self.artwork.shared_images
        matches: dict[str, str] = {}
        skipped: dict[str, str] = {}
        applied = 0
        for event, image in found:
            if image in shared:
                skipped[event.id] = image
                logger.info(
                    "Skipping %r: %s is shared across events, so it depicts the venue",
                    event.title,
                    image,
                )
                continue
            matches[event.id] = image
            if apply:
                await self.repository.update_event(event.id, image_url=image)
                applied += 1

        return BackfillSummary(
            scanned=len(events),
            without_image=len(blank),
            resolved=len(found),
            applied=applied,
            shared_art_skipped=len(skipped),
            matches=matches,
            skipped=skipped,
        )


def _pages_for(event: EventRecord) -> list[str]:
    """Pages that might carry this event's art.

    A stored record keeps the listing link in ``url``; the description often
    holds others, exactly as it did at discovery time.
    """
    return candidate_pages(event.description, event.url)
