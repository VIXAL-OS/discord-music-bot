from __future__ import annotations

import asyncio
from typing import Any

import httpx

from music_event_bot.config import Settings
from music_event_bot.domain.normalization import normalize_text

_API_URL = "https://ws.audioscrobbler.com/2.0/"

# Community tags that are popular but are not musical genres.
JUNK_TAGS = frozenset(
    normalize_text(tag)
    for tag in (
        "seen live",
        "favorite",
        "favorites",
        "favourite",
        "favourites",
        "albums i own",
        "vinyl",
        "under 2000 listeners",
        "spotify",
        "check out",
        "beautiful",
        "awesome",
        "epic",
        "love",
        "female vocalists",
        "female vocalist",
        "male vocalists",
        "male vocalist",
        "female fronted",
        "all",
        "60s",
        "70s",
        "80s",
        "90s",
        "00s",
        "10s",
        "20s",
        "1990s",
        "2000s",
        "2010s",
        "2020s",
        "american",
        "usa",
        "british",
        "uk",
        "german",
        "french",
        "canadian",
        "australian",
        "japanese",
        "swedish",
        "norwegian",
        "finnish",
        "icelandic",
        "polish",
        "russian",
        "italian",
        "scottish",
        "irish",
    )
)


class LastfmTagFetcher:
    """Fetch community genre tags for artists from the Last.fm API.

    Tags are global per-artist community data, so no Last.fm account or
    scrobble history is required — only an API key.
    """

    source = "lastfm"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client
        self._last_request_at = 0.0

    async def top_tags(self, artist: str) -> list[tuple[str, int]]:
        api_key = self.settings.lastfm_api_key
        if api_key is None:
            return []
        owned_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        try:
            await self._respect_rate_limit()
            response = await client.get(
                _API_URL,
                params={
                    "method": "artist.getTopTags",
                    "artist": artist,
                    "autocorrect": "1",
                    "api_key": api_key.get_secret_value(),
                    "format": "json",
                },
            )
            response.raise_for_status()
            payload = response.json()
        finally:
            if owned_client:
                await client.aclose()
        if not isinstance(payload, dict) or "error" in payload:
            return []
        return self._parse_tags(payload, artist)

    def _parse_tags(self, payload: dict[str, Any], artist: str) -> list[tuple[str, int]]:
        toptags = payload.get("toptags", {})
        raw_tags = toptags.get("tag", []) if isinstance(toptags, dict) else []
        if isinstance(raw_tags, dict):
            raw_tags = [raw_tags]
        artist_normalized = normalize_text(artist)
        seen: dict[str, int] = {}
        for raw in raw_tags:
            if not isinstance(raw, dict):
                continue
            name = normalize_text(str(raw.get("name", "")))
            try:
                weight = int(raw.get("count", 0))
            except (TypeError, ValueError):
                weight = 0
            if (
                not name
                or name in JUNK_TAGS
                or name == artist_normalized
                or weight < self.settings.lastfm_min_tag_weight
            ):
                continue
            seen[name] = max(seen.get(name, 0), weight)
        ranked = sorted(seen.items(), key=lambda item: (-item[1], item[0]))
        return ranked[: self.settings.lastfm_max_tags_per_artist]

    async def _respect_rate_limit(self) -> None:
        loop = asyncio.get_running_loop()
        elapsed = loop.time() - self._last_request_at
        if elapsed < 0.25:
            await asyncio.sleep(0.25 - elapsed)
        self._last_request_at = loop.time()
