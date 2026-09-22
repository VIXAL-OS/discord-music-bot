"""Seed per-user taste profiles from the genre roles members already hold.

Replacing role pings with per-user mentions has one failure mode that
matters: anyone without a profile goes quiet. Seeding from roles removes it
up front — every member who holds a genre role starts with a profile that
grants exactly the coverage that role already gave them, so the flip changes
what people are pinged *about* without changing whether they are pinged.

The planning half is pure. It takes the guild roster and the current rows
and returns what it would do, which is what the dry run prints and what the
apply path executes.
"""

from __future__ import annotations

from dataclasses import dataclass

from music_event_bot.domain.metros import DEFAULT_TRAVEL_BAND
from music_event_bot.storage.repositories import EventRepository

CREATE = "create"
ADD_GENRES = "add-genres"
UNCHANGED = "unchanged"
SKIPPED_CUSTOMIZED = "skipped-customized"
SKIPPED_NO_ROLES = "skipped-no-roles"


@dataclass(frozen=True, slots=True)
class GuildMember:
    user_id: int
    display_name: str
    role_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SeedAction:
    user_id: int
    display_name: str
    action: str
    metro: str
    travel_band: str
    # Every genre the profile should end up holding, and the subset this run
    # would add. They differ once a member gains a role after the first seed.
    genres: tuple[str, ...]
    added_genres: tuple[str, ...]

    @property
    def writes(self) -> bool:
        return self.action in {CREATE, ADD_GENRES}


def invert_genre_roles(genre_roles: dict[str, int]) -> dict[int, tuple[str, ...]]:
    """role ID -> the genres that route to it.

    Several genres legitimately share one role, which is why this is not a
    reversed dict.
    """
    inverted: dict[int, list[str]] = {}
    for genre, role_id in genre_roles.items():
        inverted.setdefault(role_id, []).append(genre)
    return {role_id: tuple(sorted(genres)) for role_id, genres in inverted.items()}


def bucket_genre_roles(
    genre_roles: dict[str, int], buckets: set[str]
) -> dict[int, tuple[str, ...]]:
    """role ID -> the configured bucket genres that route to it.

    genre_roles also holds the hundreds of tag aliases seed_genre_role_aliases
    derives on every startup. Those are how an event's genres are
    *recognised*, and they improve over time. Freezing a snapshot of them into
    a member's profile would be unreadable and would cut that member off from
    every later improvement, so seeding takes the buckets and leaves matching
    to resolve event genres through the live alias table.
    """
    return invert_genre_roles(
        {genre: role_id for genre, role_id in genre_roles.items() if genre in buckets}
    )


def plan_role_seed(
    members: list[GuildMember],
    role_genres: dict[int, tuple[str, ...]],
    existing_profiles: dict[int, dict[str, object]],
    existing_genres: dict[int, set[str]],
    *,
    default_metro: str,
    default_travel_band: str = DEFAULT_TRAVEL_BAND,
) -> tuple[SeedAction, ...]:
    """Decide what seeding would do, without touching the database."""
    actions: list[SeedAction] = []
    for member in members:
        granted: set[str] = set()
        for role_id in member.role_ids:
            granted.update(role_genres.get(role_id, ()))
        profile = existing_profiles.get(member.user_id)
        metro = str(profile["metro"]) if profile else default_metro
        band = str(profile["travel_band"]) if profile else default_travel_band
        genres = tuple(sorted(granted))

        if not granted:
            action = SKIPPED_NO_ROLES
        elif profile is not None and profile.get("customized_at"):
            # Someone has since tuned this profile by hand. Re-running the
            # seed must never walk that back, which is the whole reason
            # customized_at exists.
            action = SKIPPED_CUSTOMIZED
        else:
            action = CREATE if profile is None else UNCHANGED
        added: tuple[str, ...] = ()
        if action in {CREATE, UNCHANGED}:
            added = tuple(sorted(granted - existing_genres.get(member.user_id, set())))
            if action is UNCHANGED and added:
                action = ADD_GENRES
        actions.append(
            SeedAction(
                user_id=member.user_id,
                display_name=member.display_name,
                action=action,
                metro=metro,
                travel_band=band,
                genres=genres,
                added_genres=added,
            )
        )
    return tuple(actions)


def summarize(actions: tuple[SeedAction, ...]) -> dict[str, int]:
    counts = {
        CREATE: 0,
        ADD_GENRES: 0,
        UNCHANGED: 0,
        SKIPPED_CUSTOMIZED: 0,
        SKIPPED_NO_ROLES: 0,
    }
    for action in actions:
        counts[action.action] += 1
    return counts


async def apply_role_seed(
    repository: EventRepository,
    actions: tuple[SeedAction, ...],
    *,
    daily_ping_cap: int,
    source: str = "role-seed",
) -> dict[str, int]:
    """Execute a plan. Only CREATE and ADD_GENRES touch the database."""
    profiles_written = 0
    genres_written = 0
    for action in actions:
        if not action.writes:
            continue
        if action.action == CREATE:
            await repository.upsert_user_profile(
                action.user_id,
                display_name=action.display_name,
                metro=action.metro,
                travel_band=action.travel_band,
                daily_ping_cap=daily_ping_cap,
                source=source,
            )
            profiles_written += 1
        genres_written += await repository.add_user_taste(
            action.user_id, "genre", action.added_genres, source=source
        )
    return {"profiles_written": profiles_written, "genres_written": genres_written}
