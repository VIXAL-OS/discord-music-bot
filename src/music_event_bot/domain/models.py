from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class EventStatus(StrEnum):
    DISCOVERED = "discovered"
    PENDING_REVIEW = "pending_review"
    INCOMPLETE = "incomplete"
    APPROVED = "approved"
    PUBLISHED = "published"
    REJECTED = "rejected"
    EXPIRED = "expired"
    PUBLISH_FAILED = "publish_failed"


@dataclass(frozen=True, slots=True)
class DiscoveredEvent:
    source_name: str
    source_event_id: str
    title: str
    source_url: str | None = None
    artist: str | None = None
    # Every act on the bill (headliner first) when the source provides a
    # structured lineup; empty for sources that only supply free text.
    artists: tuple[str, ...] = ()
    venue: str | None = None
    location: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    timezone: str | None = None
    genres: tuple[str, ...] = ()
    description: str | None = None
    image_url: str | None = None
    venue_latitude: float | None = None
    venue_longitude: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    incomplete_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.starts_at is not None and self.starts_at.tzinfo is None:
            raise ValueError("starts_at must be timezone-aware")
        if self.ends_at is not None and self.ends_at.tzinfo is None:
            raise ValueError("ends_at must be timezone-aware")
        if self.ends_at and self.starts_at and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be later than starts_at")
        if (self.venue_latitude is None) != (self.venue_longitude is None):
            raise ValueError("venue coordinates must be provided together")


@dataclass(frozen=True, slots=True)
class TasteProfile:
    artists: tuple[str, ...] = ()
    genres: tuple[str, ...] = ()
    venues: tuple[str, ...] = ()
    # Weak evidence: umbrella genres ("rock", "pop") and broad genres derived
    # by mapping niche tags upward. They boost scores but can never push an
    # event past the review gate on their own.
    weak_genres: tuple[str, ...] = ()
    # Negative evidence: headliners whose genre-matched events the reviewer
    # rejected (and never approved). Their future events score lower.
    demoted_artists: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ScoreResult:
    score: int
    reasons: tuple[str, ...]
    affinity_score: int = 0
    location_bonus: int = 0
    distance_miles: float | None = None


@dataclass(frozen=True, slots=True)
class EventRecord:
    id: str
    title: str
    artist: str | None
    artists: tuple[str, ...]
    venue: str | None
    location: str | None
    starts_at: datetime | None
    ends_at: datetime | None
    timezone: str | None
    url: str | None
    image_url: str | None
    description: str | None
    genres: tuple[str, ...]
    status: EventStatus
    score: int
    match_reasons: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    venue_latitude: float | None = None
    venue_longitude: float | None = None

    @property
    def is_complete(self) -> bool:
        return bool(self.title and self.venue and self.location and self.starts_at)

    @property
    def starts_at_utc(self) -> datetime | None:
        return self.starts_at.astimezone(UTC) if self.starts_at else None


@dataclass(frozen=True, slots=True)
class UpsertResult:
    event: EventRecord
    created: bool
    source_created: bool
