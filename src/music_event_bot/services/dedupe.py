"""Find and merge events that are one show the bot stored twice.

Two things put the same show in the review queue twice. A stale fingerprint --
the key frozen at first-sight values while title, venue or start were corrected
afterwards -- removes a row from dedupe permanently, so the next source
describing that show creates a fresh one. And sources legitimately disagree
about what a show is called: "Ensiferum" from the venue's own feed against
"Winter Storm Over North America 2026: Ensiferum & Firewind" from arcane.city.

Repairing fingerprints is mechanical and safe. Merging is not: a venue with two
rooms runs two different bills at the same hour, and a jazz club sells an early
and a late set of the same billing. So the matcher is deliberately narrow, it
reports by default, and it refuses to touch a published row unless asked --
deleting one of those orphans a Discord announcement that only a human can take
down.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from music_event_bot.domain.models import EventStatus
from music_event_bot.domain.normalization import (
    canonical_fingerprint,
    normalize_text,
    normalize_venue,
)
from music_event_bot.storage.repositories import EventRepository

logger = logging.getLogger(__name__)

# Doors-vs-showtime is the disagreement worth absorbing; sources routinely
# differ by an hour on the same bill. Two-and-a-half hours is an early and a
# late set, which are different tickets, so the default stops short of it.
DEFAULT_WINDOW_MINUTES = 90

# Ranked best-keeper first. A row that reached the outside world outranks one
# that did not, whatever the review queue did afterwards.
_KEEPER_RANK = {
    EventStatus.PUBLISHED.value: 0,
    EventStatus.PUBLISH_FAILED.value: 1,
    EventStatus.APPROVED.value: 2,
    EventStatus.PENDING_REVIEW.value: 3,
    EventStatus.DISCOVERED.value: 4,
    EventStatus.INCOMPLETE.value: 5,
    EventStatus.REJECTED.value: 6,
    EventStatus.EXPIRED.value: 7,
}

_TITLE_NOISE = frozenset(
    """
    the a an and with at w presents tour live plus of for in on feat ft
    featuring special guest guests night show vol us usa north america american
    free all ages
    """.split()
)


@dataclass(frozen=True, slots=True)
class StaleFingerprint:
    event_id: str
    title: str
    stored: str
    recomputed: str
    collides_with: str | None


@dataclass(frozen=True, slots=True)
class DuplicateMember:
    event_id: str
    title: str
    venue: str | None
    starts_at: datetime
    status: str
    sources: tuple[str, ...]
    scheduled_event_id: str | None
    announcement_message_id: str | None

    @property
    def is_public(self) -> bool:
        """Whether removing this row would strand something outside the DB."""
        return bool(self.scheduled_event_id or self.announcement_message_id)


@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    keeper: DuplicateMember
    losers: tuple[DuplicateMember, ...]
    reason: str

    @property
    def blocked_by_publication(self) -> tuple[DuplicateMember, ...]:
        return tuple(loser for loser in self.losers if loser.is_public)


@dataclass(slots=True)
class DedupeReport:
    stale: list[StaleFingerprint] = field(default_factory=list)
    stale_venue_keys: list[str] = field(default_factory=list)
    groups: list[DuplicateGroup] = field(default_factory=list)
    repaired: int = 0
    venue_keys_refreshed: int = 0
    merged: int = 0
    skipped_published: int = 0


def _title_tokens(title: str) -> set[str]:
    return {
        token
        for token in normalize_text(title).split()
        if len(token) >= 3 and token not in _TITLE_NOISE
    }


def titles_describe_one_show(left: str, right: str) -> bool:
    """Whether two titles plausibly name the same bill.

    Containment covers a headliner listed against the full bill ("Ensiferum"
    inside "...: Ensiferum & Firewind"). Otherwise most of the shorter title's
    distinctive words must appear in the longer one, which keeps two different
    bands at one venue apart while tolerating a tour subtitle.
    """
    a, b = normalize_text(left), normalize_text(right)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    left_tokens, right_tokens = _title_tokens(left), _title_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    overlap = left_tokens & right_tokens
    return len(overlap) / min(len(left_tokens), len(right_tokens)) >= 0.6


def _rank(member: DuplicateMember) -> tuple[int, datetime]:
    return (_KEEPER_RANK.get(member.status, 99), member.starts_at)


class DedupeService:
    def __init__(self, repository: EventRepository) -> None:
        self.repository = repository

    async def scan(self, window_minutes: int = DEFAULT_WINDOW_MINUTES) -> DedupeReport:
        report = DedupeReport()
        async with self.repository.database.connect() as connection:
            cursor = await connection.execute(
                """
                SELECT e.id, e.fingerprint, e.title, e.venue, e.venue_normalized,
                       e.starts_at, e.status,
                       p.scheduled_event_id, p.announcement_message_id,
                       (SELECT group_concat(DISTINCT source_name)
                          FROM event_sources WHERE event_id = e.id) AS sources
                FROM events e
                LEFT JOIN publications p ON p.event_id = e.id
                WHERE e.starts_at IS NOT NULL
                ORDER BY e.starts_at
                """
            )
            rows = [dict(row) for row in await cursor.fetchall()]

        aliases = self.repository.venue_aliases.entries
        by_fingerprint = {row["fingerprint"]: row["id"] for row in rows}
        members: list[tuple[dict[str, Any], DuplicateMember]] = []
        for row in rows:
            starts_at = datetime.fromisoformat(row["starts_at"])
            # Derive the venue key here rather than trusting the stored column:
            # venue_normalized was written by whatever rules were in force when
            # the row was first seen, so on any run that introduces an alias it
            # is exactly the rows we are hunting that still carry the old key.
            venue_key = normalize_venue(row["venue"], aliases)
            row["venue_key"] = venue_key
            if venue_key != (row["venue_normalized"] or ""):
                report.stale_venue_keys.append(row["id"])
            recomputed = canonical_fingerprint(
                row["title"],
                row["venue"],
                starts_at,
                source_name="",
                source_event_id="",
                venue_aliases=self.repository.venue_aliases.entries,
            )
            if recomputed != row["fingerprint"]:
                owner = by_fingerprint.get(recomputed)
                report.stale.append(
                    StaleFingerprint(
                        event_id=row["id"],
                        title=row["title"],
                        stored=row["fingerprint"],
                        recomputed=recomputed,
                        collides_with=owner if owner and owner != row["id"] else None,
                    )
                )
            members.append(
                (
                    row,
                    DuplicateMember(
                        event_id=row["id"],
                        title=row["title"],
                        venue=row["venue"],
                        starts_at=starts_at,
                        status=row["status"],
                        sources=tuple((row["sources"] or "").split(",")) if row["sources"] else (),
                        scheduled_event_id=row["scheduled_event_id"],
                        announcement_message_id=row["announcement_message_id"],
                    ),
                )
            )

        report.groups = self._group(members, window_minutes)
        return report

    def _group(
        self, members: list[tuple[dict[str, Any], DuplicateMember]], window_minutes: int
    ) -> list[DuplicateGroup]:
        window = window_minutes * 60
        assigned: dict[str, int] = {}
        clusters: list[list[DuplicateMember]] = []
        for index, (row, member) in enumerate(members):
            venue_key = row["venue_key"]
            if not venue_key:
                # An empty venue matches every other empty venue; that is not
                # evidence of anything, and these rows are unapprovable anyway.
                continue
            for other_row, other in members[index + 1 :]:
                if other_row["venue_key"] != venue_key:
                    continue
                delta = abs((other.starts_at - member.starts_at).total_seconds())
                if delta > window:
                    continue
                if not titles_describe_one_show(member.title, other.title):
                    continue
                target = assigned.get(member.event_id, assigned.get(other.event_id))
                if target is None:
                    target = len(clusters)
                    clusters.append([])
                for candidate in (member, other):
                    if candidate.event_id not in assigned:
                        assigned[candidate.event_id] = target
                        clusters[target].append(candidate)

        groups: list[DuplicateGroup] = []
        for cluster in clusters:
            if len(cluster) < 2:
                continue
            ordered = sorted(cluster, key=_rank)
            keeper, *losers = ordered
            reason = (
                "same venue and start"
                if len({member.starts_at for member in cluster}) == 1
                else f"same venue, starts within {window_minutes} min"
            )
            groups.append(
                DuplicateGroup(keeper=keeper, losers=tuple(losers), reason=reason)
            )
        return groups

    async def repair(self, report: DedupeReport) -> int:
        """Bring the derived columns back in line with the fields they come from.

        venue_normalized is only a lookup key, so it is always safe to rewrite.
        The fingerprint is unique, so a row whose recomputed key already belongs
        to another row is left alone: that collision *is* the duplicate, and
        folding the two together is a separate decision with no undo.
        """
        repaired = 0
        async with self.repository.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                for event_id in report.stale_venue_keys:
                    cursor = await connection.execute(
                        "SELECT venue FROM events WHERE id = ?", (event_id,)
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        continue
                    await connection.execute(
                        "UPDATE events SET venue_normalized = ? WHERE id = ?",
                        (
                            normalize_venue(
                                row["venue"], self.repository.venue_aliases.entries
                            ),
                            event_id,
                        ),
                    )
                for stale in report.stale:
                    if stale.collides_with is not None:
                        continue
                    await connection.execute(
                        "UPDATE events SET fingerprint = ? WHERE id = ?",
                        (stale.recomputed, stale.event_id),
                    )
                    repaired += 1
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        report.repaired = repaired
        report.venue_keys_refreshed = len(report.stale_venue_keys)
        return repaired

    async def merge(self, group: DuplicateGroup, *, include_published: bool) -> int:
        """Fold every loser into the keeper. Returns how many rows were removed.

        Sources move first so the keeper inherits the provenance of both rows;
        the loser is then deleted, and the schema's ON DELETE CASCADE takes its
        review, publication and RSVP rows with it.
        """
        merged = 0
        async with self.repository.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                for loser in group.losers:
                    if loser.is_public and not include_published:
                        continue
                    await connection.execute(
                        """
                        UPDATE OR IGNORE event_sources SET event_id = ?
                        WHERE event_id = ?
                        """,
                        (group.keeper.event_id, loser.event_id),
                    )
                    await connection.execute(
                        "DELETE FROM event_sources WHERE event_id = ?", (loser.event_id,)
                    )
                    await connection.execute(
                        "DELETE FROM events WHERE id = ?", (loser.event_id,)
                    )
                    merged += 1
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        return merged
