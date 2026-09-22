from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from music_event_bot.domain.models import EventRecord, EventStatus
from music_event_bot.services.delivery import (
    UserMatch,
    apply_daily_caps,
    build_profiles,
    match_users,
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
