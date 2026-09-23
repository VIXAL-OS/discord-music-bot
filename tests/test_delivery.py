from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from music_event_bot.domain.models import EventRecord, EventStatus
from music_event_bot.services.delivery import (
    STRONG_SCORE,
    UserMatch,
    apply_daily_caps,
    build_profiles,
    match_users,
    spends_budget,
)

_EASTERN = ZoneInfo("America/New_York")
_GOTH_ROLE = 10
_PUNK_ROLE = 11
# Mr Smalls in Millvale, and The Dance Cave in Toronto.
_LOCAL = (40.4795, -79.9767)
_TORONTO = (43.6650, -79.4103)


def _event(coords: tuple[float, float] | None = _LOCAL, title: str = "Show") -> EventRecord:
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    latitude, longitude = coords if coords else (None, None)
    return EventRecord(
        id=title,
        title=title,
        artist=None,
        artists=(),
        venue="Venue",
        location="Location",
        starts_at=now,
        ends_at=None,
        timezone="America/New_York",
        url=None,
        image_url=None,
        description=None,
        genres=("goth",),
        status=EventStatus.PUBLISHED,
        score=50,
        match_reasons=(),
        created_at=now,
        updated_at=now,
        venue_latitude=latitude,
        venue_longitude=longitude,
    )


def _event_with_lineup(artists: tuple[str, ...]) -> EventRecord:
    from dataclasses import replace

    return replace(_event(), artists=artists, artist=artists[0] if artists else None)


def _profiles(**overrides):
    row = {
        "display_name": "Avery",
        "metro": "pittsburgh",
        "travel_band": "road-trip",
        "daily_ping_cap": 5,
        "delivery": "mention",
    }
    row.update(overrides)
    return build_profiles({1: row}, {1: {"goth"}}, {"goth": _GOTH_ROLE, "punk": _PUNK_ROLE})


def test_taste_is_resolved_to_the_same_roles_the_event_router_uses() -> None:
    profile = _profiles()[1]
    assert profile.role_ids == frozenset({_GOTH_ROLE})
    assert profile.radius_miles == 350


def test_a_member_matches_only_buckets_they_hold() -> None:
    assert [m.user_id for m in match_users(_event(), (_GOTH_ROLE,), _profiles())] == [1]
    assert match_users(_event(), (_PUNK_ROLE,), _profiles()) == ()


def test_out_of_range_matches_are_kept_and_flagged_not_dropped() -> None:
    """The gap between taste matches and in-range matches is exactly what the
    metro filter is responsible for, which is what shadow mode measures."""
    matches = match_users(_event(_TORONTO), (_GOTH_ROLE,), _profiles(travel_band="in-town"))
    assert len(matches) == 1
    assert matches[0].within_band is False
    assert matches[0].distance_miles is not None and matches[0].distance_miles > 200


def test_a_venue_without_coordinates_is_never_filtered_out() -> None:
    matches = match_users(_event(None), (_GOTH_ROLE,), _profiles(travel_band="in-town"))
    assert (matches[0].within_band, matches[0].distance_miles) == (True, None)


def test_firehose_ignores_both_taste_and_distance() -> None:
    matches = match_users(
        _event(_TORONTO), (_PUNK_ROLE,), _profiles(delivery="firehose", travel_band="in-town")
    )
    assert len(matches) == 1 and matches[0].within_band is True


def test_delivery_off_matches_nothing() -> None:
    assert match_users(_event(), (_GOTH_ROLE,), _profiles(delivery="off")) == ()


def _sequence(count: int, day: int = 20) -> list[tuple[datetime, tuple[UserMatch, ...]]]:
    return [
        (
            datetime(2026, 9, day, 12 + n, 0, tzinfo=UTC),
            (UserMatch(user_id=1, display_name="Avery", distance_miles=4.0),),
        )
        for n in range(count)
    ]


def test_matches_past_the_daily_cap_are_queued_not_dropped() -> None:
    capped = apply_daily_caps(_sequence(5), _profiles(daily_ping_cap=2), _EASTERN)
    assert [matches[0].pinged for _at, matches in capped] == [True, True, False, False, False]
    # Still matches -- they wait for the catch-up post.
    assert all(len(matches) == 1 for _at, matches in capped)


def test_the_budget_resets_the_next_day() -> None:
    profiles = _profiles(daily_ping_cap=1)
    capped = apply_daily_caps(_sequence(2, day=20) + _sequence(2, day=21), profiles, _EASTERN)
    assert [matches[0].pinged for _at, matches in capped] == [True, False, True, False]


def test_a_cap_of_zero_means_unlimited() -> None:
    capped = apply_daily_caps(_sequence(4), _profiles(daily_ping_cap=0), _EASTERN)
    assert all(matches[0].pinged for _at, matches in capped)


def test_an_out_of_range_match_is_never_pinged_whatever_the_budget() -> None:
    sequence = [
        (
            datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
            (UserMatch(1, "Avery", distance_miles=224.0, within_band=False),),
        )
    ]
    capped = apply_daily_caps(sequence, _profiles(daily_ping_cap=10), _EASTERN)
    assert capped[0][1][0].pinged is False


def _profiles_with_artist(*names: str, cap: int = 5, band: str = "road-trip"):
    row = {
        "display_name": "Avery",
        "metro": "pittsburgh",
        "travel_band": band,
        "daily_ping_cap": cap,
        "delivery": "mention",
    }
    return build_profiles(
        {1: row},
        {1: {"goth"}},
        {"goth": _GOTH_ROLE, "punk": _PUNK_ROLE},
        {1: dict.fromkeys(names, 1)},
    )


def test_a_named_act_on_the_bill_scores_far_above_proximity_alone() -> None:
    """The threshold between the two budget tiers is not a fine judgement:
    an artist match is 60, proximity alone tops out at 20."""
    plain = match_users(_event(), (_GOTH_ROLE,), _profiles_with_artist())
    followed = match_users(
        _event_with_lineup(("Liturgy", "Yellow Eyes")),
        (_GOTH_ROLE,),
        _profiles_with_artist("liturgy"),
    )
    assert plain[0].score < STRONG_SCORE <= followed[0].score


def test_following_an_act_does_not_widen_what_you_match() -> None:
    """Buckets still decide eligibility. Following only decides which
    matches survive a busy day."""
    matches = match_users(
        _event_with_lineup(("Liturgy",)), (_PUNK_ROLE,), _profiles_with_artist("liturgy")
    )
    assert matches == ()


def test_an_unfollowed_act_scores_below_one_that_is_followed() -> None:
    profiles = build_profiles(
        {
            1: {
                "display_name": "Avery",
                "metro": "pittsburgh",
                "travel_band": "road-trip",
                "daily_ping_cap": 5,
                "delivery": "mention",
            }
        },
        {1: {"goth"}},
        {"goth": _GOTH_ROLE},
        {1: {"liturgy": -1}},
    )
    demoted = match_users(_event_with_lineup(("Liturgy",)), (_GOTH_ROLE,), profiles)
    assert demoted[0].score < STRONG_SCORE


def test_ordinary_matches_stop_short_of_the_full_cap() -> None:
    # cap 5, 2 reserved -> ordinary matches get 3.
    assert [spends_budget(0, spent, 5, 2) for spent in range(6)] == [
        True,
        True,
        True,
        False,
        False,
        False,
    ]


def test_a_strong_match_can_spend_the_reserve() -> None:
    assert [spends_budget(60, spent, 5, 2) for spent in range(6)] == [
        True,
        True,
        True,
        True,
        True,
        False,
    ]


def test_a_quiet_day_is_untouched_by_the_reserve() -> None:
    """The reserve is only reached once the ordinary budget is gone, so a
    member who matches twice still hears about both."""
    assert spends_budget(0, 0, 5, 2) and spends_budget(0, 1, 5, 2)


def test_an_uncapped_member_always_spends() -> None:
    assert spends_budget(0, 99, 0, 2) is True


def test_reserving_the_whole_cap_leaves_only_strong_matches() -> None:
    assert spends_budget(0, 0, 2, 2) is False
    assert spends_budget(60, 0, 2, 2) is True


def test_the_best_five_survive_a_busy_day_not_the_first_five() -> None:
    """The point of the reserve: on a busy day the show you follow still
    reaches you even though it published last."""
    profiles = _profiles_with_artist("liturgy", cap=3)
    ordinary = [
        (
            datetime(2026, 9, 20, 12 + n, 0, tzinfo=UTC),
            (UserMatch(1, "Avery", distance_miles=4.0, score=10),),
        )
        for n in range(4)
    ]
    followed = [
        (
            datetime(2026, 9, 20, 20, 0, tzinfo=UTC),
            (UserMatch(1, "Avery", distance_miles=4.0, score=70),),
        )
    ]

    capped = apply_daily_caps(ordinary + followed, profiles, _EASTERN, reserved=2)

    pinged = [matches[0].pinged for _at, matches in capped]
    # Only one ordinary match spends (cap 3 minus 2 reserved), and the
    # followed act still gets through at the end of the day.
    assert pinged == [True, False, False, False, True]
