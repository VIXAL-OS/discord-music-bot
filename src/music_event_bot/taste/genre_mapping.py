from __future__ import annotations

import json
import logging
from typing import Any

from music_event_bot.config import Settings
from music_event_bot.domain.normalization import normalize_text

logger = logging.getLogger(__name__)

TagMapping = tuple[tuple[str, ...], tuple[str, ...]]

# Broad genre vocabulary aligned with Ticketmaster's music genre/subgenre names
# (matched after normalization) so mapped genres actually score discovered events.
BROAD_GENRES: tuple[str, ...] = (
    "metal",
    "heavy metal",
    "black metal",
    "death metal",
    "doom metal",
    "metalcore",
    "hard rock",
    "nu-metal",
    "punk",
    "pop punk",
    "hardcore",
    "post-hardcore",
    "emo",
    "ska",
    "rock",
    "alternative rock",
    "indie rock",
    "classic rock",
    "grunge",
    "psychedelic rock",
    "post-rock",
    "shoegaze",
    "new wave",
    "industrial",
    "gothic",
    "darkwave",
    "noise",
    "experimental",
    "avant-garde",
    "ambient",
    "electronic",
    "dance/electronic",
    "techno",
    "house",
    "trance",
    "drum and bass",
    "dubstep",
    "synthpop",
    "pop",
    "indie pop",
    "hip hop",
    "trap",
    "rnb",
    "funk",
    "soul",
    "disco",
    "folk",
    "singer-songwriter",
    "country",
    "americana",
    "bluegrass",
    "jazz",
    "blues",
    "classical",
    "world",
    "reggae",
    "latin",
    "new age",
)

_CHUNK_SIZE = 120


def _mapping_schema(buckets: tuple[str, ...]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "mappings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "tag": {"type": "string"},
                        "buckets": {
                            "type": "array",
                            "items": {"type": "string", "enum": list(buckets)},
                        },
                        "broad_genres": {
                            "type": "array",
                            "items": {"type": "string", "enum": list(BROAD_GENRES)},
                        },
                    },
                    "required": ["tag", "buckets", "broad_genres"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["mappings"],
        "additionalProperties": False,
    }


def _prompt(tags: list[str], buckets: tuple[str, ...]) -> str:
    bucket_lines = "\n".join(f"- {bucket}" for bucket in buckets)
    broad_lines = ", ".join(BROAD_GENRES)
    tag_lines = "\n".join(tags)
    return (
        "Map each niche music genre tag below to broader categories.\n\n"
        "For every input tag return one mapping entry with the tag echoed back "
        "exactly as given, plus:\n"
        '1. "buckets": zero or more of the Discord community genre buckets whose '
        "audience would care about events of this genre.\n"
        '2. "broad_genres": zero or more terms from the allowed broad genre list '
        "that a mainstream ticketing site (e.g. Ticketmaster) would file this "
        "genre under.\n\n"
        "Use empty arrays when a tag is not a music genre (moods, nationalities, "
        "decades, scenes without a genre meaning). Prefer precise assignments over "
        "broad ones; do not map everything to rock.\n\n"
        f"Discord community genre buckets:\n{bucket_lines}\n\n"
        f"Allowed broad genres: {broad_lines}\n\n"
        f"Tags to map:\n{tag_lines}"
    )


class GenreTagMapper:
    """Map niche genre tags onto Discord buckets and broad genres with Claude."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        self._client = client

    def _resolve_client(self) -> Any | None:
        if self._client is not None:
            return self._client
        api_key = self.settings.anthropic_api_key
        if api_key is None:
            return None
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key.get_secret_value())
        return self._client

    async def map_tags(
        self, tags: list[str], buckets: tuple[str, ...]
    ) -> dict[str, TagMapping]:
        client = self._resolve_client()
        if client is None or not tags or not buckets:
            return {}
        import anthropic

        schema = _mapping_schema(buckets)
        results: dict[str, TagMapping] = {}
        for start in range(0, len(tags), _CHUNK_SIZE):
            chunk = tags[start : start + _CHUNK_SIZE]
            try:
                response = await client.messages.create(
                    model=self.settings.genre_map_model,
                    max_tokens=16000,
                    system=(
                        "You classify music genre tags for a Discord community's "
                        "event announcement bot. The tags are Last.fm community "
                        "tags for artists the community listens to."
                    ),
                    messages=[{"role": "user", "content": _prompt(chunk, buckets)}],
                    output_config={"format": {"type": "json_schema", "schema": schema}},
                )
            except anthropic.APIError as exc:
                logger.warning(
                    "Genre tag mapping request failed for %d tags: %s", len(chunk), exc
                )
                continue
            if response.stop_reason == "refusal":
                logger.warning("Genre tag mapping request was refused; skipping chunk")
                continue
            if response.stop_reason == "max_tokens":
                logger.warning(
                    "Genre tag mapping response was truncated; skipping chunk of %d tags",
                    len(chunk),
                )
                continue
            text = next(
                (block.text for block in response.content if block.type == "text"), None
            )
            if text is None:
                logger.warning("Genre tag mapping response had no text block")
                continue
            results.update(self._parse_chunk(text, chunk))
        return results

    @staticmethod
    def _parse_chunk(text: str, requested: list[str]) -> dict[str, TagMapping]:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Genre tag mapping response was not valid JSON")
            return {}
        requested_set = set(requested)
        parsed: dict[str, TagMapping] = {}
        for entry in payload.get("mappings", []):
            if not isinstance(entry, dict):
                continue
            tag = normalize_text(str(entry.get("tag", "")))
            if tag not in requested_set:
                continue
            buckets = tuple(sorted({str(value) for value in entry.get("buckets", [])}))
            broad = tuple(sorted({str(value) for value in entry.get("broad_genres", [])}))
            parsed[tag] = (buckets, broad)
        # A tag the model skipped in an otherwise-successful response is stored
        # as an empty mapping so it is not re-sent on every startup.
        for tag in requested_set - set(parsed):
            parsed[tag] = ((), ())
        return parsed
