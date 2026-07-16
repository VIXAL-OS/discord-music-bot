from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx

from music_event_bot.config import Settings
from music_event_bot.domain.models import TasteProfile
from music_event_bot.domain.normalization import normalize_genre, normalize_text
from music_event_bot.storage.repositories import EventRepository
from music_event_bot.taste.genre_mapping import BROAD_GENRES, GenreTagMapper
from music_event_bot.taste.lastfm import LastfmTagFetcher
from music_event_bot.taste.musicbrainz import MusicbrainzTagFetcher

logger = logging.getLogger(__name__)


class TagFetcher(Protocol):
    source: str

    async def top_tags(self, artist: str) -> list[tuple[str, int]]: ...


def default_tag_fetcher(settings: Settings) -> TagFetcher | None:
    if settings.lastfm_configured:
        return LastfmTagFetcher(settings)
    if settings.musicbrainz_enabled:
        return MusicbrainzTagFetcher(settings)
    return None


class TasteEnricher:
    """Expand a taste profile's genres from niche artist tags.

    Pipeline: community tags per profile artist — Last.fm when a key is
    configured, otherwise the keyless MusicBrainz fallback — cached in
    SQLite, then a Claude mapping of each distinct tag onto the community's
    Discord genre buckets and broad Ticketmaster-style genres (also cached).
    Both steps fail soft so a missing key or network error never blocks
    startup.
    """

    def __init__(
        self,
        settings: Settings,
        repository: EventRepository,
        fetcher: TagFetcher | None = None,
        mapper: GenreTagMapper | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.fetcher = fetcher if fetcher is not None else default_tag_fetcher(settings)
        self.mapper = mapper or GenreTagMapper(settings)

    async def enrich(self, profile: TasteProfile) -> TasteProfile:
        if not profile.artists:
            return profile
        artist_names = {
            normalized: artist
            for artist in profile.artists
            if (normalized := normalize_text(artist))
        }
        if self.fetcher is not None:
            await self._refresh_artist_tags(artist_names)
        tags = await self.repository.get_tags_for_artists(set(artist_names))
        if not tags:
            return profile
        umbrella = self.settings.umbrella_genre_set
        strong = set(profile.genres)
        weak = set(profile.weak_genres)
        for tag in tags:
            if normalize_genre(tag) in umbrella:
                weak.add(tag)
            else:
                strong.add(tag)
        # The broad vocabulary itself is mapped too, so announcement role
        # tagging can alias Ticketmaster genre names onto Discord buckets.
        broad_vocab = {normalize_text(genre) for genre in BROAD_GENRES}
        mappings = await self._ensure_tag_mappings(tags | broad_vocab)
        for tag in tags:
            if tag not in mappings:
                continue
            buckets, broad_genres = mappings[tag]
            # Upward-mapped genres are inherently broader than the tag they
            # came from, so they only ever count as weak evidence.
            weak.update(buckets)
            weak.update(broad_genres)
        strong_normalized = {normalize_genre(genre) for genre in strong}
        weak = {genre for genre in weak if normalize_genre(genre) not in strong_normalized}
        return TasteProfile(
            artists=profile.artists,
            genres=tuple(sorted(strong)),
            venues=profile.venues,
            weak_genres=tuple(sorted(weak)),
        )

    async def _refresh_artist_tags(self, artist_names: dict[str, str]) -> None:
        fetcher = self.fetcher
        if fetcher is None:
            return
        freshness = await self.repository.get_artist_tag_freshness(fetcher.source)
        cache_days = self.settings.lastfm_tag_cache_days
        cutoff = (
            datetime.now(UTC) - timedelta(days=cache_days) if cache_days > 0 else None
        )
        stale: dict[str, str] = {}
        for normalized, artist in artist_names.items():
            known = freshness.get(normalized)
            if known is None or (cutoff is not None and known[0] < cutoff):
                stale[normalized] = artist
        if not stale:
            return
        logger.info("Fetching %s tags for %d artists", fetcher.source, len(stale))
        fetched = 0
        for normalized, artist in sorted(stale.items()):
            try:
                tags = await fetcher.top_tags(artist)
            except httpx.HTTPError as exc:
                logger.warning(
                    "%s tag fetch failed for %r: %s", fetcher.source, artist, exc
                )
                continue
            # Storage merges: new tags accumulate and existing tags are never
            # removed, so an empty or partial result cannot lose history.
            await self.repository.store_artist_tags(normalized, tags, fetcher.source)
            fetched += 1
        logger.info(
            "Stored %s tags for %d of %d artists", fetcher.source, fetched, len(stale)
        )

    async def _ensure_tag_mappings(
        self, tags: set[str]
    ) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
        mappings = await self.repository.get_tag_mappings(tags)
        unmapped = sorted(tags - set(mappings))
        if unmapped:
            if self.settings.anthropic_configured:
                buckets = tuple(sorted(self.settings.role_map))
                logger.info(
                    "Mapping %d new genre tags with %s",
                    len(unmapped),
                    self.settings.genre_map_model,
                )
                new_mappings = await self.mapper.map_tags(unmapped, buckets)
                if new_mappings:
                    await self.repository.store_tag_mappings(
                        new_mappings, self.settings.genre_map_model
                    )
                    mappings.update(new_mappings)
            else:
                logger.info(
                    "%d genre tags are unmapped; set MUSICBOT_ANTHROPIC_API_KEY to map "
                    "them onto Discord genre buckets",
                    len(unmapped),
                )
        return mappings
