from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from music_event_bot.domain.models import DiscoveredEvent


def manual_event(
    *,
    title: str,
    starts_at: datetime | None,
    venue: str | None,
    location: str | None,
    source_url: str | None = None,
    artist: str | None = None,
    genre: str | None = None,
    description: str | None = None,
    duration_minutes: int = 180,
    submitted_by: int | None = None,
) -> DiscoveredEvent:
    if starts_at is not None and starts_at.tzinfo is None:
        raise ValueError("Manual event start time must be timezone-aware")
    incomplete: list[str] = []
    if starts_at is None:
        incomplete.append("missing start time")
    if not venue:
        incomplete.append("missing venue")
    if not location:
        incomplete.append("missing location")
    return DiscoveredEvent(
        source_name="manual",
        source_event_id=str(uuid.uuid4()),
        title=title.strip() or "Untitled event",
        artist=artist,
        venue=venue,
        location=location,
        starts_at=starts_at,
        ends_at=(starts_at + timedelta(minutes=duration_minutes)) if starts_at else None,
        timezone=str(starts_at.tzinfo) if starts_at else None,
        source_url=source_url,
        genres=(genre,) if genre else (),
        description=description,
        raw={"submitted_by": submitted_by},
        incomplete_reasons=tuple(incomplete),
    )
