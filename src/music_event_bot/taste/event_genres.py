"""Give an event genres when its source supplied none.

Ticketmaster ships genres and the calendar feeds carry a hand-written "Genres:"
line, but the scraped listings carry neither -- so those events reach publication
with an empty genre list and fall through to the catch-all community role, no
matter how squarely they sit in one scene.

Two passes, cheapest first. Most listings name their genres in prose ("a night of
post-punk, new wave, 80s goth, and synth sleaze"), and matching those against the
vocabulary already curated in ``genre_roles`` costs nothing. Claude is asked only
about what is left over.

Note this is a different job from :mod:`music_event_bot.taste.genre_mapping`,
which maps a listener's Last.fm tags onto community buckets to build the taste
profile. That one never looks at an event; this one only looks at events.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from music_event_bot.config import Settings
from music_event_bot.domain.normalization import normalize_text

logger = logging.getLogger(__name__)

# Genre words that are ordinary English besides. A listing that says "GOING TO
# DAD'S HOUSE" or "rock out with us" is not announcing house or rock, and a
# venue in an industrial park is not announcing industrial. These are still
# assignable by Claude, which can read the sentence -- they are only barred from
# the literal text match, which cannot.
_AMBIGUOUS_ALONE = frozenset(
    {
        "house",
        "rock",
        "pop",
        "soul",
        "country",
        "trap",
        "garage",
        "drone",
        "industrial",
        "experimental",
        "comedy",
        "other",
        "swing",
        "grime",
        "breaks",
        "jungle",
        # Event names are full of these: a "Vinyl Club" meeting is not club
        # music, "Non-Stop Erotic Cabaret" is a goth night rather than cabaret,
        # and every listing invites you to dance.
        "club",
        "cabaret",
        "dance",
        "disco",
        "revival",
    }
)

# Longest vocabulary entries are four words ("melodic hardcore punk revival").
_MAX_PHRASE_WORDS = 4

_MAX_GENRES = 5

_TOKEN_RE = re.compile(r"[a-z0-9]+")

_LLM_BATCH_SIZE = 20


def _phrases(text: str) -> set[str]:
    """Every 1..4-word run in the text, normalized for vocabulary comparison."""
    tokens = _TOKEN_RE.findall(normalize_text(text))
    found: set[str] = set()
    for size in range(1, _MAX_PHRASE_WORDS + 1):
        for start in range(len(tokens) - size + 1):
            found.add(" ".join(tokens[start : start + size]))
    return found


def genres_in_text(text: str, vocabulary: tuple[str, ...]) -> tuple[str, ...]:
    """Vocabulary genres named outright in the text, longest first.

    Longest-first so a listing that says "melodic death metal" is filed under
    that rather than under the "death metal" and "metal" also contained in it.
    """
    if not text:
        return ()
    present = _phrases(text)
    matches = [
        genre
        for genre in vocabulary
        if (normalized := normalize_text(genre)) in present
        and not (" " not in normalized and normalized in _AMBIGUOUS_ALONE)
    ]
    matches.sort(key=lambda genre: (-len(genre.split()), genre))
    kept: list[str] = []
    for genre in matches:
        # A broader phrase already covering this one adds no routing information.
        if any(genre != other and genre in other for other in kept):
            continue
        kept.append(genre)
    return tuple(kept[:_MAX_GENRES])


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "genres": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["id", "genres"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["events"],
        "additionalProperties": False,
    }


def _prompt(entries: list[dict[str, str]], vocabulary: tuple[str, ...]) -> str:
    listing = "\n\n".join(
        f"id: {entry['id']}\ntitle: {entry['title']}\nvenue: {entry['venue']}\n"
        f"description: {entry['description']}"
        for entry in entries
    )
    return (
        "Assign music genres to each event below, based on its title, venue, and "
        "description.\n\n"
        "Rules:\n"
        "1. Choose only from the allowed genre list, copied exactly. A label "
        "outside the list routes the event nowhere, so it is worse than omitting "
        "it.\n"
        "2. Return at most 4 genres per event, most characteristic first.\n"
        "3. Return an empty array when the event is not a music event (a reading, "
        "a walk, a market, a film screening with no live score) or when the text "
        "gives no real signal. Guessing is worse than leaving it blank.\n"
        "4. Judge the event, not the venue's usual booking.\n\n"
        f"Allowed genres:\n{', '.join(vocabulary)}\n\n"
        f"Events:\n\n{listing}"
    )


class EventGenreClassifier:
    """Infer an event's genres from its own text."""

    def __init__(
        self,
        settings: Settings,
        vocabulary: tuple[str, ...],
        client: Any | None = None,
    ) -> None:
        self.settings = settings
        self.vocabulary = vocabulary
        self._allowed = {normalize_text(genre): genre for genre in vocabulary}
        self._client = client

    def from_text(self, title: str | None, description: str | None) -> tuple[str, ...]:
        """Pass one: genres the listing names outright. Free, no network."""
        return genres_in_text(" ".join(filter(None, (title, description))), self.vocabulary)

    def _resolve_client(self) -> Any | None:
        if self._client is not None:
            return self._client
        api_key = self.settings.anthropic_api_key
        if api_key is None:
            return None
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key.get_secret_value())
        return self._client

    async def classify(self, entries: list[dict[str, str]]) -> dict[str, tuple[str, ...]]:
        """Pass two: ask Claude about the events pass one could not place."""
        client = self._resolve_client()
        if client is None or not entries or not self.vocabulary:
            return {}
        import anthropic

        results: dict[str, tuple[str, ...]] = {}
        for start in range(0, len(entries), _LLM_BATCH_SIZE):
            chunk = entries[start : start + _LLM_BATCH_SIZE]
            try:
                response = await client.messages.create(
                    model=self.settings.genre_map_model,
                    max_tokens=4000,
                    system=(
                        "You classify live music events for a Discord community's "
                        "announcement bot, which routes each event to genre-specific "
                        "channels. A wrong genre pings the wrong people, so prefer "
                        "returning nothing to guessing."
                    ),
                    messages=[
                        {"role": "user", "content": _prompt(chunk, self.vocabulary)}
                    ],
                    output_config={"format": {"type": "json_schema", "schema": _schema()}},
                )
            except anthropic.APIError as exc:
                logger.warning(
                    "Event genre classification failed for %d events: %s", len(chunk), exc
                )
                continue
            if response.stop_reason in {"refusal", "max_tokens"}:
                logger.warning(
                    "Event genre classification returned %s; skipping chunk of %d",
                    response.stop_reason,
                    len(chunk),
                )
                continue
            text = next(
                (block.text for block in response.content if block.type == "text"), None
            )
            if text is None:
                continue
            results.update(self._parse(text, {entry["id"] for entry in chunk}))
        return results

    def _parse(self, text: str, requested: set[str]) -> dict[str, tuple[str, ...]]:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Event genre classification response was not JSON")
            return {}
        results: dict[str, tuple[str, ...]] = {}
        for entry in payload.get("events", []):
            if not isinstance(entry, dict):
                continue
            event_id = entry.get("id")
            if event_id not in requested:
                continue
            genres: list[str] = []
            for raw in entry.get("genres", []):
                # Anything outside the curated vocabulary maps to no role, so it
                # would leave the event in the catch-all regardless.
                canonical = self._allowed.get(normalize_text(str(raw)))
                if canonical and canonical not in genres:
                    genres.append(canonical)
            if genres:
                results[event_id] = tuple(genres[:_MAX_GENRES])
        return results
