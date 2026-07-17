from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from music_event_bot.config import Settings
from music_event_bot.discovery.base import DiscoveryWindow, EventSource
from music_event_bot.domain.models import TasteProfile
from music_event_bot.domain.normalization import normalize_text
from music_event_bot.domain.scoring import score_event
from music_event_bot.storage.repositories import EventRepository


def _is_trusted_venue(venue: str | None, fragments: tuple[str, ...]) -> bool:
    if not venue or not fragments:
        return False
    normalized = normalize_text(venue)
    return any(fragment in normalized for fragment in fragments)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DiscoverySummary:
    discovered: int
    created: int
    sources_added: int
    ignored_below_score: int
    source_errors: dict[str, str]


class DiscoveryOrchestrator:
    def __init__(
        self,
        settings: Settings,
        repository: EventRepository,
        sources: list[EventSource],
        profile: TasteProfile,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.sources = sources
        self.profile = profile

    async def run(self) -> DiscoverySummary:
        started_at = datetime.now(UTC)
        local_now = datetime.now(self.settings.timezone)
        window = DiscoveryWindow(
            starts_at=local_now
            + timedelta(days=self.settings.discovery_start_offset_days),
            ends_at=local_now + timedelta(days=self.settings.discovery_horizon_days),
            default_timezone=self.settings.timezone,
            default_event_duration_minutes=self.settings.default_event_duration_minutes,
        )
        discovered = created = sources_added = ignored = 0
        errors: dict[str, str] = {}
        source_diagnostics: dict[str, object] = {}

        for source in self.sources:
            try:
                source_events = await source.discover(window)
            except Exception as exc:
                logger.exception("Discovery source %s failed", source.name)
                errors[source.name] = str(exc)
                continue
            discovered += len(source_events)
            diagnostics = getattr(source, "last_diagnostics", None)
            if diagnostics:
                source_diagnostics[source.name] = diagnostics
            requires_affinity = getattr(source, "requires_affinity", False)
            trusted_venues = self.settings.trusted_venue_fragments
            for event in source_events:
                score = score_event(
                    event,
                    self.profile,
                    home=self.settings.home_point,
                    max_travel_radius_miles=self.settings.max_travel_radius_miles,
                )
                below_gate = (
                    requires_affinity
                    and score.affinity_score < self.settings.minimum_affinity_score
                )
                venue_trusted = below_gate and _is_trusted_venue(event.venue, trusted_venues)
                if (below_gate and not venue_trusted) or (
                    score.score < self.settings.minimum_match_score
                ):
                    ignored += 1
                    continue
                if venue_trusted:
                    score = replace(
                        score, reasons=(*score.reasons, f"trusted venue: {event.venue}")
                    )
                elif not requires_affinity:
                    # Trusted feeds bypass the affinity gate; say so on the
                    # review card instead of leaving "Why it matched" empty.
                    score = replace(
                        score, reasons=(*score.reasons, f"curated source: {source.name}")
                    )
                result = await self.repository.upsert_discovered(event, score)
                created += int(result.created)
                sources_added += int(result.source_created)

        summary = DiscoverySummary(
            discovered=discovered,
            created=created,
            sources_added=sources_added,
            ignored_below_score=ignored,
            source_errors=errors,
        )
        await self.repository.record_job_run(
            "discovery",
            started_at,
            "partial" if errors else "success",
            {
                "discovered": discovered,
                "created": created,
                "sources_added": sources_added,
                "ignored_below_score": ignored,
                "source_errors": errors,
                "source_diagnostics": source_diagnostics,
            },
        )
        return summary
