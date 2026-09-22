"""Work out which members an event would be announced to, one by one.

Shadow mode's whole job is to make the flip measurable before it happens,
which means the comparison has to isolate what is actually changing. Taste
matching here is deliberately bucket-equivalent to the role routing it would
replace: a member matches an event when the buckets they hold overlap the
roles the event maps to, which is exactly what a role mention does today.

So the delta shadow reports is attributable to the two things per-user
delivery introduces and nothing else -- the metro/travel-band filter, and
the daily ping cap. Finer-grained taste than a bucket is a later refinement;
adding it now would muddy the only measurement that matters.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from music_event_bot.domain.geography import GeoPoint, haversine_miles
from music_event_bot.domain.metros import metro as lookup_metro
from music_event_bot.domain.metros import travel_band as lookup_travel_band
from music_event_bot.domain.models import EventRecord
from music_event_bot.domain.normalization import normalize_text
from music_event_bot.storage.repositories import EventRepository


async def roles_for_event(
    repository: EventRepository,
    event: EventRecord,
    *,
    fallback_role_ids: frozenset[int] = frozenset(),
    catchall_role_ids: frozenset[int] = frozenset(),
) -> tuple[int, ...]:
    """The community roles an event routes to.

    Lifted out of PublicationService so the shadow report can resolve the
    same roles offline: replaying months of announcements to see what
    per-user delivery would have done should not need a Discord session.
    """
    roles = await repository.get_roles_for_genres(event.genres)
    specific = tuple(role for role in roles if role not in fallback_role_ids)
    if specific:
        return specific
    # The source's genre labels matched no mapped role. Before settling
    # for the catch-all, let the lineup's cached artist tags vote for
    # genre buckets the same way discovery scoring does.
    artists = {normalize_text(name) for name in (*event.artists, event.artist or "")}
    artists.discard("")
    if artists:
        tags = await repository.get_tags_for_artists(artists)
        if tags:
            mappings = await repository.get_tag_mappings(tags)
            buckets = tuple(
                bucket for bucket_list, _broad in mappings.values() for bucket in bucket_list
            )
            bucket_roles = await repository.get_roles_for_genres(buckets)
            bucket_specific = tuple(
                role for role in bucket_roles if role not in fallback_role_ids
            )
            if bucket_specific:
                return bucket_specific
    if roles:
        return roles
    # Nothing matched anywhere: ping the music catch-all rather than
    # publishing silently.
    return tuple(sorted(catchall_role_ids))


@dataclass(frozen=True, slots=True)
class UserProfile:
    user_id: int
    display_name: str
    metro: str
    travel_band: str
    daily_ping_cap: int
    delivery: str
    # The member's taste resolved to the same role IDs the event router uses.
    role_ids: frozenset[int]

    @property
    def home(self) -> GeoPoint:
        return lookup_metro(self.metro).center

    @property
    def radius_miles(self) -> int:
        return lookup_travel_band(self.travel_band).radius_miles


@dataclass(frozen=True, slots=True)
class UserMatch:
    user_id: int
    display_name: str
    distance_miles: float | None
    # A taste match the member is too far from. Kept rather than dropped so
    # the report can attribute the reduction: taste is what roles already
    # did, so everything filtered here is the metro's doing.
    within_band: bool = True
    # False once the member is over their cap for the day: still a match,
    # but it waits for the catch-up post rather than being dropped.
    pinged: bool = True


def build_profiles(
    rows: dict[int, dict[str, Any]],
    genres_by_user: dict[int, set[str]],
    bucket_roles: dict[str, int],
) -> dict[int, UserProfile]:
    """Turn stored rows into profiles, resolving taste to role IDs."""
    profiles: dict[int, UserProfile] = {}
    for user_id, row in rows.items():
        role_ids = frozenset(
            bucket_roles[genre]
            for genre in genres_by_user.get(user_id, set())
            if genre in bucket_roles
        )
        profiles[user_id] = UserProfile(
            user_id=user_id,
            display_name=str(row.get("display_name") or user_id),
            metro=str(row["metro"]),
            travel_band=str(row["travel_band"]),
            daily_ping_cap=int(row["daily_ping_cap"]),
            delivery=str(row["delivery"]),
            role_ids=role_ids,
        )
    return profiles


def match_users(
    event: EventRecord,
    event_role_ids: tuple[int, ...],
    profiles: dict[int, UserProfile],
) -> tuple[UserMatch, ...]:
    """Every member whose taste covers this event, in range or not.

    Out-of-range matches come back with within_band False rather than being
    dropped, because the difference between the two is exactly the reduction
    the metro filter is responsible for, and that is what shadow measures.

    Cap handling is not here: whether a match is pinged or queued depends on
    what else went out that day, which is a property of the sequence rather
    than of the event.
    """
    wanted = frozenset(event_role_ids)
    matches: list[UserMatch] = []
    for profile in profiles.values():
        if profile.delivery == "off":
            continue
        firehose = profile.delivery == "firehose"
        if not firehose and not (wanted & profile.role_ids):
            continue
        distance: float | None = None
        within = True
        if event.venue_latitude is not None and event.venue_longitude is not None:
            distance = haversine_miles(
                profile.home, GeoPoint(event.venue_latitude, event.venue_longitude)
            )
            # Same call as the channel split: a venue with no coordinates is
            # not evidence of distance, so it is not grounds for hiding.
            within = firehose or distance <= profile.radius_miles
        matches.append(
            UserMatch(
                user_id=profile.user_id,
                display_name=profile.display_name,
                distance_miles=distance,
                within_band=within,
            )
        )
    return tuple(sorted(matches, key=lambda match: match.display_name.casefold()))


def apply_daily_caps(
    announcements: list[tuple[datetime, tuple[UserMatch, ...]]],
    profiles: dict[int, UserProfile],
    timezone: ZoneInfo,
) -> list[tuple[datetime, tuple[UserMatch, ...]]]:
    """Mark matches past a member's daily budget as queued rather than pinged.

    Overflow is not dropped -- it waits for the next day's catch-up post --
    so this only decides which of the day's matches carry a mention.
    """
    spent: dict[tuple[int, str], int] = {}
    capped: list[tuple[datetime, tuple[UserMatch, ...]]] = []
    for announced_at, matches in sorted(announcements, key=lambda item: item[0]):
        day = announced_at.astimezone(timezone).date().isoformat()
        decided: list[UserMatch] = []
        for match in matches:
            profile = profiles.get(match.user_id)
            budget = profile.daily_ping_cap if profile else 0
            key = (match.user_id, day)
            used = spent.get(key, 0)
            pinged = match.within_band and (budget <= 0 or used < budget)
            if pinged:
                spent[key] = used + 1
            decided.append(
                UserMatch(
                    user_id=match.user_id,
                    display_name=match.display_name,
                    distance_miles=match.distance_miles,
                    within_band=match.within_band,
                    pinged=pinged,
                )
            )
        capped.append((announced_at, tuple(decided)))
    return capped
