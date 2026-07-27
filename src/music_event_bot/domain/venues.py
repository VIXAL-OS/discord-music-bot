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
