from __future__ import annotations

import asyncio
from typing import Any

import httpx

from music_event_bot.config import Settings
from music_event_bot.domain.normalization import normalize_text
from music_event_bot.taste.lastfm import JUNK_TAGS

_API_URL = "https://musicbrainz.org/ws/2/artist"
# MusicBrainz requires a meaningful User-Agent and at most one request/second.
_USER_AGENT = "music-event-bot/0.1.0 (Discord community event announcement bot)"
_MIN_SEARCH_SCORE = 90


class MusicbrainzTagFetcher:
    """Fetch community genre tags from the MusicBrainz search API.

    Keyless fallback for when a Last.fm API key is not configured. Tags are
    sparser than Last.fm's, and tag counts run roughly 1-20 rather than
    0-100, so any tagged genre is accepted rather than weight-filtered.
    """

    source = "musicbrainz"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client
        self._last_request_at = 0.0

    async def top_tags(self, artist: str) -> list[tuple[str, int]]:
        owned_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0), headers={"User-Agent": _USER_AGENT}
        )
        try:
            await self._respect_rate_limit()
            response = await client.get(
                _API_URL,
                params={"query": f'artist:"{artist}"', "fmt": "json", "limit": "1"},
            )
            response.raise_for_status()
            payload = response.json()
        finally:
            if owned_client:
                await client.aclose()
        if not isinstance(payload, dict):
            return []
        return self._parse_tags(payload, artist)

    def _parse_tags(self, payload: dict[str, Any], artist: str) -> list[tuple[str, int]]:
        artists = payload.get("artists", [])
        if not artists or not isinstance(artists[0], dict):
            return []
        top_match = artists[0]
        try:
            score = int(top_match.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        if score < _MIN_SEARCH_SCORE:
            return []
        artist_normalized = normalize_text(artist)
        seen: dict[str, int] = {}
        for raw in top_match.get("tags", []) or []:
            if not isinstance(raw, dict):
                continue
            name = normalize_text(str(raw.get("name", "")))
            try:
                weight = int(raw.get("count", 0))
            except (TypeError, ValueError):
                weight = 0
            if not name or name in JUNK_TAGS or name == artist_normalized or weight < 1:
                continue
            seen[name] = max(seen.get(name, 0), weight)
        ranked = sorted(seen.items(), key=lambda item: (-item[1], item[0]))
        return ranked[: self.settings.lastfm_max_tags_per_artist]

    async def _respect_rate_limit(self) -> None:
        loop = asyncio.get_running_loop()
        elapsed = loop.time() - self._last_request_at
        if elapsed < 1.1:
            await asyncio.sleep(1.1 - elapsed)
        self._last_request_at = loop.time()
