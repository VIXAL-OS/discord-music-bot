from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from music_event_bot.config import Settings
from music_event_bot.discovery.arcane import ArcaneCitySource
from music_event_bot.discovery.artwork import ArtworkResolver
from music_event_bot.discovery.base import EventSource
from music_event_bot.discovery.feeds import CalendarSource, FeedSource
from music_event_bot.discovery.squarespace import SquarespaceSource
from music_event_bot.discovery.ticketmaster import TicketmasterSource
from music_event_bot.domain.blocklist import Blocklist
from music_event_bot.domain.models import TasteProfile
from music_event_bot.domain.venues import VenueAliases
from music_event_bot.services.orchestration import DiscoveryOrchestrator
from music_event_bot.storage.database import Database
from music_event_bot.storage.repositories import EventRepository
from music_event_bot.taste.enrichment import TasteEnricher
from music_event_bot.taste.event_genres import EventGenreClassifier
from music_event_bot.taste.manual import profile_from_settings
from music_event_bot.taste.spotify import SpotifyTasteImporter

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Application:
    settings: Settings
    database: Database
    repository: EventRepository
    profile: TasteProfile
    sources: list[EventSource]
    discovery: DiscoveryOrchestrator

    @classmethod
    async def create(cls, settings: Settings) -> Application:
        database = Database(settings.database_path)
        await database.initialize()
        venue_aliases = VenueAliases.load(settings.venue_aliases_path)
        if venue_aliases:
            logger.info("Collapsing %d venue aliases", len(venue_aliases.entries))
        repository = EventRepository(database, venue_aliases=venue_aliases)
        await repository.seed_genre_roles(settings.role_map)

        manual_profile = profile_from_settings(settings)
        spotify_profile = await SpotifyTasteImporter(settings, repository).import_profile()
        profile = TasteProfile(
            artists=tuple(sorted(set(manual_profile.artists) | set(spotify_profile.artists))),
            genres=tuple(sorted(set(manual_profile.genres) | set(spotify_profile.genres))),
            venues=manual_profile.venues,
        )
        profile = await TasteEnricher(settings, repository).enrich(profile)
        demoted_artists = await repository.rejected_artist_signals()
        if demoted_artists:
            profile = replace(profile, demoted_artists=demoted_artists)
            logger.info(
                "Review history demotes %d previously rejected headliners",
                len(demoted_artists),
            )
        aliases_added = await repository.seed_genre_role_aliases(settings.role_map)
        if aliases_added:
            logger.info("Seeded %d genre->role aliases from cached tag mappings", aliases_added)
        await repository.backfill_ticketmaster_coordinates()
        await repository.rescore_reviewable_events(
            profile,
            settings.home_point,
            settings.max_travel_radius_miles,
        )

        sources: list[EventSource] = []
        if settings.ticketmaster_configured:
            sources.append(TicketmasterSource(settings))
        if settings.calendar_urls:
            sources.append(
                CalendarSource(
                    settings.calendar_urls, venue_defaults=settings.feed_venue_default_map
                )
            )
        if settings.feed_urls:
            sources.append(
                FeedSource(settings.feed_urls, venue_defaults=settings.feed_venue_default_map)
            )
        if settings.squarespace_event_urls:
            sources.append(SquarespaceSource(settings.squarespace_event_urls))
        if settings.arcane_city_enabled:
            sources.append(ArcaneCitySource())
        # The vocabulary is whatever currently routes to a role, so genres added
        # to genre_roles become assignable without touching this code.
        genre_classifier = EventGenreClassifier(settings, await repository.known_genres())
        blocklist = Blocklist.load(settings.blocked_artists_path)
        if blocklist:
            logger.info("Blocking %d artists from review", len(blocklist.entries))
        discovery = DiscoveryOrchestrator(
            settings,
            repository,
            sources,
            profile,
            artwork=ArtworkResolver(),
            genres=genre_classifier,
            blocklist=blocklist,
        )
        return cls(settings, database, repository, profile, sources, discovery)
