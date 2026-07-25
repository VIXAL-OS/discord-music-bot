"""A curated list of acts that must never reach review.

This is deliberately a hand-maintained list, not a classifier. Nothing in an
event listing -- title, venue, genre tags, ticket price -- carries evidence
about an artist's politics, so there is no signal to infer from. The only
honest mechanism is a roster somebody decided to add, with the reason recorded
next to the name so a future reader can re-examine the call.

Distinct from TasteProfile.demoted_artists, which docks 15 points from a
headliner whose events were rejected before: that is a soft nudge derived from
review history and strong evidence elsewhere on the bill can still outvote it.
A blocklist entry is absolute and applies to any act on the bill, not just the
headliner -- an opener nobody wants to platform is still on the poster.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from music_event_bot.domain.models import DiscoveredEvent
from music_event_bot.domain.normalization import normalize_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BlockedMatch:
    """Which entry matched, and where on the bill it was found."""

    name: str
    reason: str
    matched_on: str


@dataclass(frozen=True, slots=True)
class Blocklist:
    # normalized name -> (display name, reason)
    entries: dict[str, tuple[str, str]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Blocklist:
        """Read the blocklist file; an absent file is an empty list, not an error."""
        if not path.exists():
            return cls(entries={})
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Could not read blocklist {path}: {exc}") from None
        if not isinstance(raw, list):
            raise ValueError(f"{path} must contain a JSON array of objects")
        entries: dict[str, tuple[str, str]] = {}
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError(f"{path}: every entry must be an object")
            name = str(item.get("name", "")).strip()
            reason = str(item.get("reason", "")).strip()
            if not name:
                continue
            normalized = normalize_text(name)
            if normalized:
                entries[normalized] = (name, reason or "no reason recorded")
        return cls(entries=entries)

    def __bool__(self) -> bool:
        return bool(self.entries)

    def match(self, event: DiscoveredEvent) -> BlockedMatch | None:
        """The first blocked act found anywhere on the bill, or None.

        Checks the structured lineup first, since that is exact. The title is
        only consulted when the source supplied no lineup at all -- the same
        last-resort ordering scoring uses, and for the same reason: title text
        also names unrelated bands and marketing copy.
        """
        if not self.entries:
            return None

        headliner = normalize_text(event.artist)
        if headliner and headliner in self.entries:
            name, reason = self.entries[headliner]
            return BlockedMatch(name=name, reason=reason, matched_on="artist")

        for performer in event.artists:
            normalized = normalize_text(performer)
            if normalized and normalized in self.entries:
                name, reason = self.entries[normalized]
                return BlockedMatch(name=name, reason=reason, matched_on="lineup")

        lineup_known = headliner or any(normalize_text(name) for name in event.artists)
        if not lineup_known:
            title_padded = f" {normalize_text(event.title)} "
            for normalized, (name, reason) in self.entries.items():
                if f" {normalized} " in title_padded:
                    return BlockedMatch(name=name, reason=reason, matched_on="title")
        return None
