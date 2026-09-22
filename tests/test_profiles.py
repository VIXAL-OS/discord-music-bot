from __future__ import annotations

import pytest

from music_event_bot.domain.geography import GeoPoint
from music_event_bot.domain.metros import DEFAULT_TRAVEL_BAND, metro, nearest_metro, travel_band
from music_event_bot.services.profiles import (
    ADD_GENRES,
    CREATE,
    SKIPPED_CUSTOMIZED,
    SKIPPED_NO_ROLES,
    UNCHANGED,
    GuildMember,
    apply_role_seed,
    bucket_genre_roles,
    invert_genre_roles,
    plan_role_seed,
    summarize,
)

_GOTH_ROLE = 10
_PUNK_ROLE = 11
_UNRELATED_ROLE = 99
_ROLE_GENRES = {_GOTH_ROLE: ("darkwave", "goth"), _PUNK_ROLE: ("punk",)}


def _member(user_id: int, name: str, *roles: int) -> GuildMember:
    return GuildMember(user_id=user_id, display_name=name, role_ids=roles)


def _plan(members, profiles=None, genres=None):
    return plan_role_seed(
        members,
        _ROLE_GENRES,
        profiles or {},
        genres or {},
        default_metro="pittsburgh",
    )


def test_several_genres_can_share_one_role() -> None:
    """genre_roles is keyed by genre, so inverting it is not a reversed dict."""
    inverted = invert_genre_roles({"goth": 10, "darkwave": 10, "punk": 11})
    assert inverted == {10: ("darkwave", "goth"), 11: ("punk",)}


def test_seed_grants_exactly_the_coverage_the_roles_already_gave() -> None:
    actions = _plan([_member(1, "Avery", _GOTH_ROLE, _UNRELATED_ROLE)])
    assert len(actions) == 1
    action = actions[0]
    assert action.action == CREATE
    assert action.genres == ("darkwave", "goth")
    assert action.added_genres == ("darkwave", "goth")
    # The widest band on purpose: it is what every member effectively has
    # today, so the flip does not silently narrow anyone.
    assert action.travel_band == DEFAULT_TRAVEL_BAND
    assert action.metro == "pittsburgh"


def test_member_holding_no_genre_role_is_left_alone() -> None:
    actions = _plan([_member(2, "Lurker", _UNRELATED_ROLE)])
    assert actions[0].action == SKIPPED_NO_ROLES
    assert actions[0].writes is False


def test_rerunning_the_seed_changes_nothing() -> None:
    members = [_member(1, "Avery", _GOTH_ROLE)]
    profiles = {1: {"metro": "cleveland", "travel_band": "day-trip", "customized_at": None}}
    actions = _plan(members, profiles, {1: {"goth", "darkwave"}})
    assert actions[0].action == UNCHANGED
    assert actions[0].added_genres == ()
    # An existing profile keeps its own metro and band, not the defaults.
    assert (actions[0].metro, actions[0].travel_band) == ("cleveland", "day-trip")


def test_a_newly_gained_role_is_picked_up_on_the_next_run() -> None:
    members = [_member(1, "Avery", _GOTH_ROLE, _PUNK_ROLE)]
    profiles = {1: {"metro": "pittsburgh", "travel_band": "road-trip", "customized_at": None}}
    actions = _plan(members, profiles, {1: {"goth", "darkwave"}})
    assert actions[0].action == ADD_GENRES
    assert actions[0].added_genres == ("punk",)


def test_hand_tuned_profiles_are_never_walked_back() -> None:
    """The whole reason customized_at exists: someone narrowed their profile
    and a re-run of the seed must not hand their roles back to them."""
    members = [_member(1, "Avery", _GOTH_ROLE, _PUNK_ROLE)]
    profiles = {
        1: {
            "metro": "cleveland",
            "travel_band": "in-town",
            "customized_at": "2026-09-20T12:00:00+00:00",
        }
    }
    actions = _plan(members, profiles, {1: {"goth"}})
    assert actions[0].action == SKIPPED_CUSTOMIZED
    assert actions[0].writes is False
    assert actions[0].added_genres == ()


def test_summary_counts_every_action() -> None:
    actions = _plan(
        [_member(1, "Avery", _GOTH_ROLE), _member(2, "Lurker"), _member(3, "Rowan", _PUNK_ROLE)]
    )
    assert summarize(actions) == {
        CREATE: 2,
        ADD_GENRES: 0,
        UNCHANGED: 0,
        SKIPPED_CUSTOMIZED: 0,
        SKIPPED_NO_ROLES: 1,
    }


def test_home_metro_and_bands_resolve() -> None:
    assert nearest_metro(GeoPoint(40.4406, -79.9959)).key == "pittsburgh"
    assert metro("toronto").label == "Toronto"
    assert travel_band("in-town").radius_miles == 40
    with pytest.raises(ValueError, match="Unknown metro"):
        metro("atlantis")
    with pytest.raises(ValueError, match="Unknown travel band"):
        travel_band("teleport")


@pytest.mark.asyncio
async def test_apply_writes_profiles_and_is_idempotent(repository) -> None:
    members = [_member(1, "Avery", _GOTH_ROLE), _member(2, "Lurker", _UNRELATED_ROLE)]
    first = await apply_role_seed(repository, _plan(members), daily_ping_cap=5)
    assert first == {"profiles_written": 1, "genres_written": 2}

    profiles = await repository.list_user_profiles()
    assert set(profiles) == {1}
    assert profiles[1]["metro"] == "pittsburgh"
    assert profiles[1]["daily_ping_cap"] == 5
    assert profiles[1]["customized_at"] is None

    second = await apply_role_seed(
        repository,
        _plan(members, profiles, await repository.list_user_taste("genre")),
        daily_ping_cap=5,
    )
    assert second == {"profiles_written": 0, "genres_written": 0}


@pytest.mark.asyncio
async def test_apply_does_not_reset_settings_a_member_changed(repository) -> None:
    members = [_member(1, "Avery", _GOTH_ROLE)]
    await apply_role_seed(repository, _plan(members), daily_ping_cap=5)
    async with repository.database.connect() as connection:
        await connection.execute(
            "UPDATE user_profiles SET metro = 'cleveland', travel_band = 'in-town', "
            "daily_ping_cap = 2 WHERE user_id = '1'"
        )
        await connection.commit()

    await apply_role_seed(
        repository,
        _plan(members, await repository.list_user_profiles(), {}),
        daily_ping_cap=5,
    )

    profiles = await repository.list_user_profiles()
    assert profiles[1]["metro"] == "cleveland"
    assert profiles[1]["travel_band"] == "in-town"
    assert profiles[1]["daily_ping_cap"] == 2


@pytest.mark.asyncio
async def test_a_demoted_genre_is_not_promoted_back_by_a_reseed(repository) -> None:
    """Negative weight means "stop showing me this". The seed must read it as
    already-held, not as a gap to fill."""
    members = [_member(1, "Avery", _GOTH_ROLE)]
    await apply_role_seed(repository, _plan(members), daily_ping_cap=5)
    async with repository.database.connect() as connection:
        await connection.execute(
            "UPDATE user_taste SET weight = -1 WHERE user_id = '1' AND value = 'goth'"
        )
        await connection.commit()

    held = await repository.list_user_taste("genre")
    assert held[1] == {"darkwave"}
    await apply_role_seed(
        repository,
        _plan(members, await repository.list_user_profiles(), held),
        daily_ping_cap=5,
    )

    async with repository.database.connect() as connection:
        cursor = await connection.execute(
            "SELECT weight FROM user_taste WHERE user_id = '1' AND value = 'goth'"
        )
        assert (await cursor.fetchone())["weight"] == -1


def test_seed_takes_the_configured_buckets_not_the_derived_aliases() -> None:
    """genre_roles also holds the hundreds of tag aliases seeded from cached
    Last.fm mappings. Those are how events are recognised and they improve
    over time; a member's profile must not freeze a snapshot of them."""
    stored = {
        "goth": _GOTH_ROLE,
        "punk": _PUNK_ROLE,
        # Derived aliases pointing at the same two roles.
        "darkwave": _GOTH_ROLE,
        "coldwave": _GOTH_ROLE,
        "hardcore punk": _PUNK_ROLE,
        "d beat": _PUNK_ROLE,
    }
    role_genres = bucket_genre_roles(stored, {"goth", "punk"})
    assert role_genres == {_GOTH_ROLE: ("goth",), _PUNK_ROLE: ("punk",)}

    actions = plan_role_seed(
        [_member(1, "Avery", _GOTH_ROLE, _PUNK_ROLE)],
        role_genres,
        {},
        {},
        default_metro="pittsburgh",
    )
    assert actions[0].genres == ("goth", "punk")
