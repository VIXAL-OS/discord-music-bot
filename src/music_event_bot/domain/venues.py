"""Curated aliases that collapse one room's many names into one identity.

The canonical fingerprint is ``title|venue|start``, so a venue spelled two ways
is two shows as far as the bot is concerned -- the same failure that put
Ensiferum in the review queue twice, once as "Thunderbird" (arcane.city) and
once as "Thunderbird Music Hall" (the venue's own feed).

Mechanical variants -- a leading "The", theater/theatre, a trailing state code
-- are handled in normalize_venue and need no entry here. This file is for the
cases nothing in the string reveals: only a person who knows Pittsburgh can say
that "Thunderbird" and "Thunderbird Cafe & Music Hall" are one room while
"Spirit Hall" and "Spirit Lodge" are two, in the same building, running
different bills on the same night. That judgement gets recorded next to the
name rather than inferred.

Shared prefixes are the trap here, so some near-misses are left out on purpose:
"Southgate House Revival" and its Sanctuary/Revival Room/Lounge, and TSDMAAC
and its Catacombs/Confessional/Crypt, are multi-room venues running concurrent
bills; "Brooklyn Bowl" and "Brooklyn Bowl Philadelphia" are different cities.
Aliasing any of those would merge shows that genuinely differ.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from music_event_bot.domain.normalization import normalize_venue

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VenueAliases:
    # normalized alias -> normalized canonical name
    entries: dict[str, str] = field(default_factory=dict)
    # normalized canonical name -> display name, for reporting
    display: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> VenueAliases:
        """Read the alias file; an absent file is an empty table, not an error."""
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Could not read venue aliases {path}: {exc}") from None
        if not isinstance(raw, list):
            raise ValueError(f"{path} must contain a JSON array of objects")
        entries: dict[str, str] = {}
        display: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError(f"{path}: every entry must be an object")
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            # The canonical name is resolved without the table so an entry can
            # never point at itself through another entry.
            canonical = normalize_venue(name)
            if not canonical:
                continue
            display[canonical] = name
            for alias in item.get("aliases", []):
                normalized = normalize_venue(str(alias))
                if not normalized or normalized == canonical:
                    continue
                previous = entries.get(normalized)
                if previous is not None and previous != canonical:
                    raise ValueError(
                        f"{path}: alias {alias!r} is claimed by both "
                        f"{display.get(previous, previous)!r} and {name!r}"
                    )
                entries[normalized] = canonical
        return cls(entries=entries, display=display)

    def __bool__(self) -> bool:
        return bool(self.entries)

    def as_mapping(self) -> dict[str, str]:
        return dict(self.entries)


_STATE_PART = re.compile(r"([A-Z]{2})(?:\s+(\d{5})(?:-\d{4})?)?")


def _locality(location: str | None) -> tuple[str | None, str | None]:
    """(city|state, zip) read off an address, either part None when absent.

    The zip is only taken where it follows the state code, because a bare
    five-digit run is as likely to be a street number (TSDMAAC is 15701 James
    Couzens Fwy) as a zip.
    """
    parts = [part.strip() for part in (location or "").split(",")]
    for index, part in enumerate(parts):
        match = _STATE_PART.fullmatch(part)
        if match is None or index == 0:
            continue
        city = " ".join(parts[index - 1].casefold().split())
        zip_code = match.group(2)
        if zip_code is None and index + 1 < len(parts) and re.fullmatch(r"\d{5}", parts[index + 1]):
            zip_code = parts[index + 1]
        return f"{city}|{match.group(1)}", zip_code
    return None, None


def same_locality(first: str | None, second: str | None) -> bool:
    """True when two addresses provably name the same town.

    Venue names repeat across cities ("The Foundry" is in Cleveland and
    elsewhere; "Spirit" is a Pittsburgh room and a common word), so a shared
    name alone is not enough to lend one event another's coordinates. Zips
    decide when both addresses carry one; otherwise city and state must
    match. An address that names neither proves nothing and returns False.
    """
    first_city, first_zip = _locality(first)
    second_city, second_zip = _locality(second)
    if first_zip and second_zip:
        return first_zip == second_zip
    return first_city is not None and first_city == second_city
