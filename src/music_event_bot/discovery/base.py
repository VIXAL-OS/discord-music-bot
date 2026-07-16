from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from music_event_bot.domain.models import DiscoveredEvent


@dataclass(frozen=True, slots=True)
class DiscoveryWindow:
    starts_at: datetime
    ends_at: datetime
    default_timezone: ZoneInfo
    default_event_duration_minutes: int

    def __post_init__(self) -> None:
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise ValueError("Discovery window datetimes must be timezone-aware")
        if self.ends_at <= self.starts_at:
            raise ValueError("Discovery window end must be after its start")


class EventSource(Protocol):
    name: str

    async def discover(self, window: DiscoveryWindow) -> list[DiscoveredEvent]: ...
