from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from music_event_bot.config import Settings
from music_event_bot.discovery.artwork import ArtworkResolver
from music_event_bot.discovery.base import DiscoveryWindow, EventSource
from music_event_bot.domain.blocklist import Blocklist
from music_event_bot.domain.geography import GeoPoint, haversine_miles
from music_event_bot.domain.models import DiscoveredEvent, EventRecord, TasteProfile
from music_event_bot.domain.normalization import normalize_text, normalize_venue
from music_event_bot.domain.scoring import score_event
from music_event_bot.storage.repositories import EventRepository
from music_event_bot.taste.event_genres import EventGenreClassifier


def _is_trusted_venue(venue: str | None, fragments: tuple[str, ...]) -> bool:
    if not venue or not fragments:
        return False
    normalized = normalize_text(venue)
    return any(fragment in normalized for fragment in fragments)


def _within_travel_radius(
    event: DiscoveredEvent, home: GeoPoint, max_radius_miles: int
) -> bool:
    """False when the venue is provably too far to be worth reviewing.

    Ticketmaster applied this while parsing, so every other source -- ICS, RSS,
    Squarespace, arcane, AXS/TicketWeb -- ingested at any distance and reached
    review on affinity alone (a Manhattan show scored 90 on an artist match).
    The gate compares affinity_score, which excludes location_bonus by
    construction, so scoring could never hold those back on its own.

    Events whose venue has no coordinates are kept: absent geography is not
    evidence of distance, and DIY listings frequently carry none.
    """
    if max_radius_miles <= 0:
        return True
    if event.venue_latitude is None or event.venue_longitude is None:
        return True
    venue_point = GeoPoint(event.venue_latitude, event.venue_longitude)
    return haversine_miles(home, venue_point) <= max_radius_miles


_ZIP_CODE = re.compile(r"\b\d{5}\b")


def _echoes_venue(event: DiscoveredEvent) -> bool:
    """True when the location field merely repeats the venue name.

    Several sources fill location with the venue name rather than leave it
    empty, which looks populated but carries no address. Treating that as a
    real location let the address book skip the row entirely.
    """
    if not event.location or not event.venue:
        return False
    return normalize_text(event.location) == normalize_text(event.venue)


def _zip_code(value: str | None) -> str | None:
    match = _ZIP_CODE.search(value or "")
    return match.group(0) if match else None


def _contradicts(location: str, curated: str) -> bool:
    """True when the two addresses disagree on the zip they name.

    The narrowest evidence that a listing is actually wrong rather than merely
    spelled differently. It keeps the book from trampling addresses that are
    fine: "Ave" vs "Avenue" agrees on 15224, and a book entry that names no zip
    at all (Hot Mass) never contradicts anything.
    """
    listed, book = _zip_code(location), _zip_code(curated)
    return bool(listed and book and listed != book)


def _fill(event: DiscoveredEvent, venue: str, location: str) -> DiscoveredEvent:
    incomplete = tuple(
        reason
        for reason in event.incomplete_reasons
        if not (reason == "missing venue" and venue)
        and not (reason == "missing location" and location)
    )
    return replace(event, venue=venue, location=location, incomplete_reasons=incomplete)


def _apply_address_book(event: DiscoveredEvent, book: dict[str, str]) -> DiscoveredEvent:
    """Fill or correct venue/location from the address book.

    Two ways in, with deliberately different authority:

    * Keyed by **venue** -- a hand-checked entry for a room we know. It may
      correct what the source supplied, but only on the two signals that mean
      the listing is genuinely unusable: a location that just repeats the venue
      name, or one whose zip contradicts the curated one (arcane.city puts The
      Eagle in 15202; it is 15212). Anything else is left alone, so a source
      that supplies a *better* address than the book -- Hot Mass listings that
      name the street the book deliberately withholds -- keeps it.
    * Keyed by **title** -- secret-location parties and recurring series that
      never carry an address at all. A title fragment is a far looser signal
      than a venue name, so it only ever fills a gap.
    """
    if not book:
        return event
    echo = _echoes_venue(event)
    venue_padded = f" {normalize_venue(event.venue)} " if event.venue else ""
    title_padded = f" {normalize_text(event.title)} "

    for fragment, curated in book.items():
        normalized = f" {normalize_text(fragment)} "
        if venue_padded and normalized in venue_padded:
            if not (echo or not event.location or _contradicts(event.location, curated)):
                return event
            return _fill(event, event.venue or curated.split(",", 1)[0].strip(), curated)
        if normalized in title_padded and not (event.venue and event.location and not echo):
            return _fill(
                event,
                event.venue or curated.split(",", 1)[0].strip(),
                curated if (echo or not event.location) else event.location,
            )
    return event

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DiscoverySummary:
    discovered: int
    created: int
    sources_added: int
    ignored_below_score: int
    source_errors: dict[str, str]
    artwork_found: int = 0
    genres_assigned: int = 0
    ignored_out_of_radius: int = 0
    blocked_artists: int = 0


class DiscoveryOrchestrator:
    def __init__(
        self,
        settings: Settings,
        repository: EventRepository,
        sources: list[EventSource],
        profile: TasteProfile,
        artwork: ArtworkResolver | None = None,
        genres: EventGenreClassifier | None = None,
        blocklist: Blocklist | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.sources = sources
        self.profile = profile
        self.artwork = artwork
        self.genres = genres
        self.blocklist = blocklist or Blocklist()

    async def _classify_genres(self, events: list[EventRecord]) -> int:
        """Ask Claude about events whose own text named no genre."""
        if self.genres is None or not events:
            return 0
        entries = [
            {
                "id": event.id,
                "title": event.title,
                "venue": event.venue or "",
                "description": (event.description or "")[:600],
            }
            for event in events
        ]
        assigned = await self.genres.classify(entries)
        for event_id, genres in assigned.items():
            await self.repository.update_event(event_id, genres=list(genres))
            logger.info("Classified %s as %s", event_id, ", ".join(genres))
        return len(assigned)

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
        discovered = created = sources_added = ignored = artwork_found = 0
        out_of_radius = blocked = 0
        errors: dict[str, str] = {}
        source_diagnostics: dict[str, object] = {}
        # Artwork is written only once every source has run: a venue's house image
        # is not provably generic until a second event turns up carrying it, and
        # by then the first event would already own it.
        pending_artwork: list[tuple[str, str]] = []
        needs_genres: list[EventRecord] = []

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
            curated = not getattr(source, "requires_affinity", False)
            trusted_venues = self.settings.trusted_venue_fragments
            address_book = self.settings.venue_address_map
            for event in source_events:
                event = _apply_address_book(event, address_book)
                blocked_match = self.blocklist.match(event)
                if blocked_match is not None:
                    logger.info(
                        "Blocked %r from %s: %s (%s, matched on %s)",
                        event.title,
                        source.name,
                        blocked_match.name,
                        blocked_match.reason,
                        blocked_match.matched_on,
                    )
                    blocked += 1
                    continue
                if not _within_travel_radius(
                    event,
                    self.settings.home_point,
                    self.settings.max_travel_radius_miles,
                ):
                    out_of_radius += 1
                    continue
                # Before scoring, so a listing that names its genres in prose is
                # judged on them rather than being gated as though it had none.
                if self.genres is not None and not event.genres:
                    named = self.genres.from_text(event.title, event.description)
                    if named:
                        event = replace(event, genres=named)
                score = score_event(
                    event,
                    self.profile,
                    home=self.settings.home_point,
                    max_travel_radius_miles=self.settings.max_travel_radius_miles,
                )
                # Every automated source passes the taste gate — curated feeds
                # included. Manual submissions and @mention requests reach
                # review directly without going through discovery.
                below_gate = score.affinity_score < self.settings.minimum_affinity_score
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
                elif curated:
                    score = replace(
                        score, reasons=(*score.reasons, f"curated source: {source.name}")
                    )
                result = await self.repository.upsert_discovered(event, score)
                created += int(result.created)
                sources_added += int(result.source_created)
                # Only after the upsert, so a listing whose art was already found
                # (or supplied by a merged Ticketmaster row) is never re-fetched.
                if self.artwork is not None and result.event.image_url is None:
                    image = await self.artwork.resolve(event)
                    if image:
                        pending_artwork.append((result.event.id, image))
                # Whatever the text did not name outright goes to Claude, but only
                # for events that actually cleared the gate -- asking about every
                # rejected listing would be thousands of calls a run for nothing.
                if self.genres is not None and not result.event.genres:
                    needs_genres.append(result.event)

        genres_assigned = await self._classify_genres(needs_genres)

        if pending_artwork:
            shared = self.artwork.shared_images if self.artwork is not None else frozenset()
            for event_id, image in pending_artwork:
                if image in shared:
                    logger.info(
                        "Dropping artwork %s for %s: another event carries the same image",
                        image,
                        event_id,
                    )
                    continue
                await self.repository.update_event(event_id, image_url=image)
                artwork_found += 1

        deduped = await self.repository.dedupe_tour_events(
            self.settings.home_point, self.settings.max_travel_radius_miles
        )
        if deduped:
            logger.info("Tour dedupe removed %d farther sibling events", deduped)
        closer_pending = await self.repository.published_with_closer_pending(
            self.settings.home_point
        )
        for published_title, closer_title in closer_pending:
            logger.warning(
                "Published event %r has a closer unpublished sibling %r",
                published_title,
                closer_title,
            )

        summary = DiscoverySummary(
            discovered=discovered,
            created=created,
            sources_added=sources_added,
            ignored_below_score=ignored,
            source_errors=errors,
            artwork_found=artwork_found,
            genres_assigned=genres_assigned,
            ignored_out_of_radius=out_of_radius,
            blocked_artists=blocked,
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
                "ignored_out_of_radius": out_of_radius,
                "blocked_artists": blocked,
                "artwork_found": artwork_found,
                "genres_assigned": genres_assigned,
                "source_errors": errors,
                "source_diagnostics": source_diagnostics,
            },
        )
        return summary
