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
from music_event_bot.domain.models import EventRecord, TasteProfile
from music_event_bot.domain.normalization import normalize_text
from music_event_bot.domain.scoring import score_event
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
    # This decides *whether* they match, and is deliberately no finer than a
    # role mention already was.
    role_ids: frozenset[int]
    # Named acts, which decide *which* matches survive a full day's cap.
    artists: tuple[str, ...] = ()
    demoted_artists: tuple[str, ...] = ()
    venues: tuple[str, ...] = ()

    @property
    def home(self) -> GeoPoint:
        return lookup_metro(self.metro).center

    @property
    def radius_miles(self) -> int:
        return lookup_travel_band(self.travel_band).radius_miles

    @property
    def taste(self) -> TasteProfile:
        """The member's taste in the shape score_event already understands.

        Genres are left out on purpose. user_taste holds bucket names while
        an event carries whatever labels its source used, so a bucket would
        only ever match an event literally tagged with it -- and eligibility
        is already settled by role overlap before scoring runs. What is left
        is exactly the personal signal: acts the member named, and how close
        the show is to them.
        """
        return TasteProfile(
            artists=self.artists,
            venues=self.venues,
            demoted_artists=self.demoted_artists,
        )


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
    # How much this member in particular should care, used to ration a full
    # day's budget. An act they named scores 60; proximity alone tops out
    # at 20, so the threshold between the two tiers is not a fine judgement.
    score: int = 0


def build_profiles(
    rows: dict[int, dict[str, Any]],
    genres_by_user: dict[int, set[str]],
    bucket_roles: dict[str, int],
    artists_by_user: dict[int, dict[str, int]] | None = None,
    venues_by_user: dict[int, dict[str, int]] | None = None,
) -> dict[int, UserProfile]:
    """Turn stored rows into profiles, resolving taste to role IDs.

    Artist rows carry their weight because both signs are used: a positive
    one promotes a show up the day's ranking, a negative one pushes it down
    the same way a rejected headliner does for the curator's own profile.
    """
    artists_by_user = artists_by_user or {}
    venues_by_user = venues_by_user or {}
    profiles: dict[int, UserProfile] = {}
    for user_id, row in rows.items():
        role_ids = frozenset(
            bucket_roles[genre]
            for genre in genres_by_user.get(user_id, set())
            if genre in bucket_roles
        )
        weighted = artists_by_user.get(user_id, {})
        profiles[user_id] = UserProfile(
            user_id=user_id,
            display_name=str(row.get("display_name") or user_id),
            metro=str(row["metro"]),
            travel_band=str(row["travel_band"]),
            daily_ping_cap=int(row["daily_ping_cap"]),
            delivery=str(row["delivery"]),
            role_ids=role_ids,
            artists=tuple(sorted(name for name, weight in weighted.items() if weight > 0)),
            demoted_artists=tuple(
                sorted(name for name, weight in weighted.items() if weight <= 0)
            ),
            venues=tuple(
                sorted(
                    name
                    for name, weight in venues_by_user.get(user_id, {}).items()
                    if weight > 0
                )
            ),
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
                score=score_event(
                    event,
                    profile.taste,
                    home=profile.home,
                    max_travel_radius_miles=profile.radius_miles,
                ).score,
            )
        )
    return tuple(sorted(matches, key=lambda match: match.display_name.casefold()))


# An act the member named scores 60 in score_event; proximity alone tops out
# at 20. Anything at or above this is "a show they would be annoyed to miss".
STRONG_SCORE = 50


def spends_budget(score: int, spent: int, cap: int, reserved: int) -> bool:
    """Whether a match may spend a ping, given what the day has already cost.

    The cap alone rations by publish order, which is arbitrary with respect
    to how much anyone cares: on a twenty-five show day you got the first
    five, not the best five. Nothing online can know at noon whether a
    better show publishes at six, so instead the last few slots are reserved
    -- ordinary matches stop at cap minus reserved, and only a strong match
    can spend the rest. A quiet day is unaffected, because the reserve is
    only reached once the ordinary budget is gone.
    """
    if cap <= 0:
        return True
    if spent >= cap:
        return False
    ordinary = max(0, cap - reserved)
    return score >= STRONG_SCORE or spent < ordinary


def apply_daily_caps(
    announcements: list[tuple[datetime, tuple[UserMatch, ...]]],
    profiles: dict[int, UserProfile],
    timezone: ZoneInfo,
    reserved: int = 0,
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
            pinged = match.within_band and spends_budget(
                match.score, used, budget, reserved
            )
            if pinged:
                spent[key] = used + 1
            decided.append(
                UserMatch(
                    user_id=match.user_id,
                    display_name=match.display_name,
                    distance_miles=match.distance_miles,
                    within_band=match.within_band,
                    pinged=pinged,
                    score=match.score,
                )
            )
        capped.append((announced_at, tuple(decided)))
    return capped
