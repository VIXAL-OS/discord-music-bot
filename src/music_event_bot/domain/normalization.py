from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from datetime import UTC, datetime
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_DEFAULT_GENRE_ALIASES = {
    "alt rock": "alternative rock",
    "alternative": "alternative rock",
    "edm": "electronic",
    "electronica": "electronic",
    "hip hop": "hip hop",
    "hip-hop": "hip hop",
    "indie": "indie rock",
    "rap": "hip hop",
    "r&b": "rnb",
    "rhythm and blues": "rnb",
}


@lru_cache(maxsize=8192)
def _normalize_text_cached(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return _normalize_text_cached(value)


def _default_alias_map() -> dict[str, str]:
    return {
        normalize_text(key): normalize_text(target)
        for key, target in _DEFAULT_GENRE_ALIASES.items()
    }


_DEFAULT_ALIAS_MAP = _default_alias_map()


def normalize_genre(value: str, aliases: dict[str, str] | None = None) -> str:
    normalized = normalize_text(value)
    if aliases:
        mapping = dict(_DEFAULT_ALIAS_MAP)
        mapping.update(
            {normalize_text(key): normalize_text(target) for key, target in aliases.items()}
        )
        return mapping.get(normalized, normalized)
    return _DEFAULT_ALIAS_MAP.get(normalized, normalized)


def normalize_genres(
    values: tuple[str, ...], aliases: dict[str, str] | None = None
) -> tuple[str, ...]:
    return tuple(
        sorted({genre for value in values if (genre := normalize_genre(value, aliases))})
    )


def normalize_url(value: str | None) -> str | None:
    if not value:
        return None
    parts = urlsplit(value.strip())
    if not parts.scheme or not parts.netloc:
        return value.strip()
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), parts.path, query, ""))


# Ticketmaster disambiguates same-named rooms with a trailing state code
# ("Howard Theatre-DC", "Lincoln Theatre-NC") while every other source omits
# it. Dropping the code cannot merge two genuinely different venues here: the
# fingerprint also pins the start minute, and one tour cannot play two cities
# at the same instant.
_US_STATE_CODES = frozenset(
    """
    al ak az ar ca co ct de dc fl ga hi id il in ia ks ky la me md ma mi mn ms
    mo mt ne nv nh nj nm ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv wi
    wy pr vi gu
    """.split()
)
_VENUE_SPELLING = {"theater": "theatre", "centre": "center"}


def normalize_venue(value: str | None, aliases: Mapping[str, str] | None = None) -> str:
    """Normalize a venue name for identity comparison.

    The fingerprint treats the venue as part of a show's identity, so every
    spelling a source invents fragments that identity and spawns a duplicate.
    Mechanical rules cover the variants that recur across sources; genuinely
    different names for one room ("Thunderbird" vs "Thunderbird Music Hall")
    carry no signal to infer from and come from the curated alias table
    instead -- see domain/venues.py.
    """
    normalized = normalize_text(value)
    if not normalized:
        return ""
    tokens = normalized.split()
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    if len(tokens) > 1 and tokens[-1] in _US_STATE_CODES:
        tokens = tokens[:-1]
    tokens = [_VENUE_SPELLING.get(token, token) for token in tokens]
    canonical = " ".join(tokens)
    if aliases:
        canonical = aliases.get(canonical, canonical)
    return canonical


def canonical_fingerprint(
    title: str,
    venue: str | None,
    starts_at: datetime | None,
    *,
    source_name: str,
    source_event_id: str,
    venue_aliases: Mapping[str, str] | None = None,
) -> str:
    if starts_at is None:
        identity = f"incomplete|{source_name}|{source_event_id}"
    else:
        start_key = starts_at.astimezone(UTC).replace(second=0, microsecond=0).isoformat()
        identity = f"{normalize_text(title)}|{normalize_venue(venue, venue_aliases)}|{start_key}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()
