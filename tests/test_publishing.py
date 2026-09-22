from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from music_event_bot.domain.blocklist import Blocklist
from music_event_bot.domain.geography import GeoPoint
from music_event_bot.domain.models import EventRecord, EventStatus, ScoreResult
from music_event_bot.services.publishing import PublicationService

_EASTERN = ZoneInfo("America/New_York")
# 2026-07-17 12:00 EDT / 03:00 EDT expressed in UTC.
_NOON_LOCAL = datetime(2026, 7, 17, 16, 0, tzinfo=UTC)
_NIGHT_LOCAL = datetime(2026, 7, 17, 7, 0, tzinfo=UTC)


class FakePublicationGateway:
    def __init__(self) -> None:
        self.scheduled_calls: list[str] = []
        self.announcement_calls: list[tuple[str, tuple[int, ...], int | None]] = []
        self.update_calls: list[tuple[str, int, int, tuple[int, ...]]] = []
        # Kept beside the call tuples rather than inside them so the routing
        # tests can assert on channels without rewriting every existing
        # assertion about roles.
        self.announcement_channels: list[int | None] = []
        self.update_channels: list[int | None] = []
        self.announcement_error: Exception | None = None

    async def create_or_find_scheduled_event(self, event: EventRecord) -> int:
        self.scheduled_calls.append(event.id)
        return 7001

    async def create_or_find_announcement(
        self,
        event: EventRecord,
        role_ids: tuple[int, ...],
        scheduled_event_id: int,
        channel_id: int | None = None,
    ) -> int:
        self.announcement_calls.append((event.id, role_ids, scheduled_event_id))
        self.announcement_channels.append(channel_id)
        if self.announcement_error is not None:
            raise self.announcement_error
        return 8001

    async def update_published_event(
        self,
        event: EventRecord,
        scheduled_event_id: int,
        announcement_message_id: int,
        role_ids: tuple[int, ...],
        channel_id: int | None = None,
    ) -> None:
        self.update_calls.append(
            (event.id, scheduled_event_id, announcement_message_id, role_ids)
        )
        self.update_channels.append(channel_id)


async def _approved_event(repository, complete_event) -> EventRecord:
    stored = await repository.upsert_discovered(complete_event, ScoreResult(score=10, reasons=()))
    return await repository.approve(stored.event.id, reviewer_id=42)


async def _queue_of_three(repository, complete_event) -> list[str]:
    """Approve three events with staggered dates; return IDs soonest-first."""
    ids: list[str] = []
    for day, suffix in ((25, "late"), (18, "soon"), (20, "middle")):
        event = replace(
            complete_event,
            source_event_id=f"queued-{suffix}",
            title=f"Queued Show {suffix}",
            starts_at=complete_event.starts_at.replace(day=day),
            ends_at=complete_event.ends_at.replace(day=day),
        )
        approved = await _approved_event(repository, event)
        ids.append(approved.id)
    return [ids[1], ids[2], ids[0]]  # soonest (18), middle (20), late (25)


@pytest.mark.asyncio
async def test_nearby_venue_events_flagged_as_duplicates(repository, complete_event) -> None:
    first = await repository.upsert_discovered(
        complete_event, ScoreResult(score=20, reasons=())
    )
    hour_later = replace(
        complete_event,
        source_event_id="dupe-listing",
        title="The Example Ensemble (Late Show)",
        starts_at=complete_event.starts_at.replace(hour=21),
        ends_at=complete_event.ends_at.replace(hour=23, minute=30),
    )
    second = await repository.upsert_discovered(hour_later, ScoreResult(score=20, reasons=()))

    nearby = await repository.find_nearby_venue_events(first.event)
    assert [event.id for event in nearby] == [second.event.id]

    far_away = replace(
        complete_event,
        source_event_id="different-venue",
        title="Unrelated Show",
        venue="Another Room",
        location="Another Room, New York, NY",
    )
    third = await repository.upsert_discovered(far_away, ScoreResult(score=20, reasons=()))
    assert await repository.find_nearby_venue_events(third.event) == []


@pytest.mark.asyncio
async def test_rsvp_round_trip_and_state_changes(repository, complete_event) -> None:
    stored = await repository.upsert_discovered(
        complete_event, ScoreResult(score=10, reasons=())
    )
    event_id = stored.event.id

    await repository.upsert_rsvp(event_id, 1, "casey", "going")
    await repository.upsert_rsvp(event_id, 2, "sam", "interested")
    await repository.upsert_rsvp(event_id, 3, "vic", "going")
    assert await repository.get_rsvps(event_id) == {
        "going": ["casey", "vic"],
        "interested": ["sam"],
    }

    # Changing your answer moves you, never duplicates you.
    await repository.upsert_rsvp(event_id, 1, "casey", "declined")
    groups = await repository.get_rsvps(event_id)
    assert groups["going"] == ["vic"]
    assert groups["declined"] == ["casey"]

    with pytest.raises(ValueError, match="Unknown RSVP state"):
        await repository.upsert_rsvp(event_id, 4, "eve", "maybe")


@pytest.mark.asyncio
async def test_announcement_registrations_listed_for_published_events(
    repository, complete_event
) -> None:
    event = await _approved_event(repository, complete_event)
    gateway = FakePublicationGateway()
    service = PublicationService(repository, gateway)
    await service.publish(event.id)

    assert await repository.list_announcement_registrations() == [(event.id, 8001)]


def test_rsvp_summary_formats_counts_and_overflow() -> None:
    from music_event_bot.discord.rsvp import rsvp_summary

    assert rsvp_summary({}) is None
    summary = rsvp_summary(
        {"going": [f"user{i}" for i in range(12)], "declined": ["one"]}
    )
    assert summary is not None
    assert "**Going (12)**" in summary
    assert "+2 more" in summary
    assert "**Can't go (1)**: one" in summary
    assert "Interested" not in summary


def test_announcement_embed_adds_whos_in_field_only_when_rsvps_exist() -> None:
    from music_event_bot.discord.rsvp import announcement_embed
    from tests.test_discord_bot import _record

    empty = announcement_embed(_record(), {})
    assert all(field.name != "Who's in" for field in empty.fields)

    populated = announcement_embed(_record(), {"going": ["casey"]})
    fields = {field.name: field.value for field in populated.fields}
    assert "**Going (1)**: casey" in fields["Who's in"]


@pytest.mark.asyncio
async def test_drain_respects_quiet_hours(repository, complete_event) -> None:
    await _queue_of_three(repository, complete_event)
    gateway = FakePublicationGateway()
    service = PublicationService(repository, gateway)

    published = await service.drain_approved(
        limit_per_hour=10,
        start_hour=6,
        end_hour=24,
        timezone=_EASTERN,
        now=_NIGHT_LOCAL,  # 3 AM local: nobody gets pinged
    )
    assert published == 0
    assert gateway.announcement_calls == []


@pytest.mark.asyncio
async def test_drain_publishes_soonest_first_within_budget(
    repository, complete_event
) -> None:
    soonest_first = await _queue_of_three(repository, complete_event)
    gateway = FakePublicationGateway()
    service = PublicationService(repository, gateway)

    # Real clock with a full-day window: the budget check compares against
    # the real timestamps that publish() writes.
    published = await service.drain_approved(
        limit_per_hour=2,
        start_hour=0,
        end_hour=24,
        timezone=_EASTERN,
    )
    assert published == 2
    assert gateway.scheduled_calls == soonest_first[:2]

    # The two publications just made consume this hour's budget entirely.
    again = await service.drain_approved(
        limit_per_hour=2,
        start_hour=0,
        end_hour=24,
        timezone=_EASTERN,
    )
    assert again == 0
    assert len(gateway.scheduled_calls) == 2

    # A pacing value of 0 disables the drain entirely.
    assert (
        await service.drain_approved(
            limit_per_hour=0, start_hour=0, end_hour=24, timezone=_EASTERN
        )
        == 0
    )


@pytest.mark.asyncio
async def test_multiple_roles_tagged_and_fallback_suppressed(
    repository, complete_event
) -> None:
    from dataclasses import replace

    crossover = replace(complete_event, genres=("Punk", "Metal", "Rock"))
    event = await _approved_event(repository, crossover)
    await repository.seed_genre_roles({"punk": 1, "metal": 2, "rock": 99})
    gateway = FakePublicationGateway()
    service = PublicationService(repository, gateway, fallback_role_ids=frozenset({99}))

    await service.publish(event.id)
    # Both specific roles are tagged; the catch-all role is suppressed
    # because specific matches exist.
    assert gateway.announcement_calls == [(event.id, (2, 1), 7001)]


@pytest.mark.asyncio
async def test_fallback_role_used_when_nothing_specific_matches(
    repository, complete_event
) -> None:
    from dataclasses import replace

    generic = replace(complete_event, genres=("Rock",))
    event = await _approved_event(repository, generic)
    await repository.seed_genre_roles({"rock": 99})
    gateway = FakePublicationGateway()
    service = PublicationService(repository, gateway, fallback_role_ids=frozenset({99}))

    await service.publish(event.id)
    assert gateway.announcement_calls == [(event.id, (99,), 7001)]


@pytest.mark.asyncio
async def test_artist_tags_vote_for_roles_when_genres_miss(
    repository, complete_event
) -> None:
    from dataclasses import replace

    # The source supplied no usable genre labels, but the lineup's cached
    # tags still identify the right community role.
    unlabeled = replace(
        complete_event, genres=(), artist="The Body", artists=("The Body", "Dis Fig")
    )
    event = await _approved_event(repository, unlabeled)
    await repository.seed_genre_roles({"metal": 5, "other music": 99})
    await repository.store_artist_tags(
        "the body", [("sludge metal", 100), ("doom metal", 80)]
    )
    await repository.store_tag_mappings(
        {
            "sludge metal": (("metal",), ("metal",)),
            "doom metal": (("metal",), ("metal",)),
        },
        "test-model",
    )
    gateway = FakePublicationGateway()
    service = PublicationService(repository, gateway, fallback_role_ids=frozenset({99}))

    await service.publish(event.id)
    assert gateway.announcement_calls == [(event.id, (5,), 7001)]


@pytest.mark.asyncio
async def test_catchall_pinged_when_nothing_matches_at_all(
    repository, complete_event
) -> None:
    from dataclasses import replace

    mystery = replace(complete_event, genres=(), artists=())
    event = await _approved_event(repository, mystery)
    gateway = FakePublicationGateway()
    service = PublicationService(repository, gateway, fallback_role_ids=frozenset({99}))

    await service.publish(event.id)
    # Publishing silently was the old behavior; the catch-all role now hears
    # about events no mapping recognizes.
    assert gateway.announcement_calls == [(event.id, (99,), 7001)]


@pytest.mark.asyncio
async def test_publish_survives_scheduled_event_cap(repository, complete_event) -> None:
    """At Discord's 100-event cap the announcement still goes out."""

    class CappedGateway(FakePublicationGateway):
        async def create_or_find_scheduled_event(self, event: EventRecord) -> int | None:
            self.scheduled_calls.append(event.id)
            return None

    event = await _approved_event(repository, complete_event)
    gateway = CappedGateway()
    service = PublicationService(repository, gateway)

    published = await service.publish(event.id)
    assert published.status is EventStatus.PUBLISHED
    assert gateway.announcement_calls[0][0] == event.id
    assert gateway.announcement_calls[0][2] is None
    publication = await repository.get_publication(event.id)
    assert publication is not None
    assert publication["scheduled_event_id"] is None
    assert publication["announcement_message_id"] is not None


@pytest.mark.asyncio
async def test_last_resort_pings_music_catchall_only(repository, complete_event) -> None:
    from dataclasses import replace

    # Ticketmaster's "other" genre maps to no role: only the music catch-all
    # should hear about it, not every "other"-bucket community.
    mystery = replace(complete_event, genres=("other",), artists=())
    event = await _approved_event(repository, mystery)
    gateway = FakePublicationGateway()
    service = PublicationService(
        repository,
        gateway,
        fallback_role_ids=frozenset({98, 99}),
        catchall_role_ids=frozenset({99}),
    )

    await service.publish(event.id)
    assert gateway.announcement_calls == [(event.id, (99,), 7001)]


@pytest.mark.asyncio
async def test_publish_records_success_and_reuses_existing_external_resources(
    repository, complete_event
) -> None:
    event = await _approved_event(repository, complete_event)
    await repository.set_genre_role("indie rock", 1234)
    gateway = FakePublicationGateway()
    service = PublicationService(repository, gateway)

    first = await service.publish(event.id)
    second = await service.publish(event.id)
    publication = await repository.get_publication(event.id)

    assert first.status is EventStatus.PUBLISHED
    assert second.status is EventStatus.PUBLISHED
    assert gateway.scheduled_calls == [event.id]
    assert gateway.announcement_calls == [(event.id, (1234,), 7001)]
    assert publication is not None
    assert publication["state"] == "published"
    assert publication["scheduled_event_id"] == "7001"
    assert publication["announcement_message_id"] == "8001"
    assert publication["attempts"] == 2


@pytest.mark.asyncio
async def test_publish_marks_failure_and_retry_preserves_created_scheduled_event(
    repository, complete_event
) -> None:
    event = await _approved_event(repository, complete_event)
    gateway = FakePublicationGateway()
    gateway.announcement_error = RuntimeError("announcement delivery failed")
    service = PublicationService(repository, gateway)

    with pytest.raises(RuntimeError, match="announcement delivery failed"):
        await service.publish(event.id)

    failed_event = await repository.get_event(event.id)
    failed_publication = await repository.get_publication(event.id)
    assert failed_event is not None
    assert failed_event.status is EventStatus.PUBLISH_FAILED
    assert failed_publication is not None
    assert failed_publication["state"] == "failed"
    assert failed_publication["scheduled_event_id"] == "7001"
    assert failed_publication["announcement_message_id"] is None
    assert failed_publication["attempts"] == 1
    assert failed_publication["last_error"] == "announcement delivery failed"

    gateway.announcement_error = None
    retried = await service.publish(event.id)
    completed_publication = await repository.get_publication(event.id)

    assert retried.status is EventStatus.PUBLISHED
    assert gateway.scheduled_calls == [event.id]
    assert gateway.announcement_calls == [(event.id, (), 7001), (event.id, (), 7001)]
    assert completed_publication is not None
    assert completed_publication["state"] == "published"
    assert completed_publication["attempts"] == 2


@pytest.mark.asyncio
async def test_publish_refuses_an_act_blocklisted_after_approval(
    repository, complete_event
) -> None:
    """The blocklist filtered only at ingest, so approval could predate the entry.

    An act added to the roster after its show was already approved sailed
    straight through the publish queue to the community.
    """
    approved = await _approved_event(repository, complete_event)
    gateway = FakePublicationGateway()
    service = PublicationService(
        repository,
        gateway,
        blocklist=Blocklist(
            entries={"the example ensemble": ("The Example Ensemble", "community decision")}
        ),
    )

    with pytest.raises(ValueError, match="blocklist"):
        await service.publish(approved.id)

    # Nothing reached Discord at all -- not even the scheduled event, which is
    # created before the announcement.
    assert gateway.scheduled_calls == []
    assert gateway.announcement_calls == []
    stored = await repository.get_event(approved.id)
    assert stored is not None
    assert stored.status is EventStatus.PUBLISH_FAILED
    publication = await repository.get_publication(approved.id)
    assert publication is not None
    assert "blocklist" in publication["last_error"]


@pytest.mark.asyncio
async def test_retract_pulls_a_published_event_back_out(repository, complete_event) -> None:
    """A roster decision can arrive after the show was already announced."""
    approved = await _approved_event(repository, complete_event)
    service = PublicationService(repository, FakePublicationGateway())
    published = await service.publish(approved.id)
    assert published.status is EventStatus.PUBLISHED

    # reject() refuses this state by design, which left no way to record the
    # decision at all.
    with pytest.raises(ValueError):
        await repository.reject(approved.id, reviewer_id=42, reason="too late")

    await repository.retract(approved.id, reviewer_id=42, reason="blocked act on the bill")

    stored = await repository.get_event(approved.id)
    assert stored is not None
    assert stored.status is EventStatus.REJECTED
    # The publication row survives, so the announcement it points at stays
    # traceable -- taking that down is a separate, external step.
    publication = await repository.get_publication(approved.id)
    assert publication is not None
    assert publication["announcement_message_id"] is not None


@pytest.mark.asyncio
async def test_retract_refuses_an_event_still_in_review(repository, complete_event) -> None:
    """retract() is for what already went out; reject() covers the review queue."""
    stored = await repository.upsert_discovered(
        complete_event, ScoreResult(score=10, reasons=())
    )
    with pytest.raises(ValueError, match="not approved/published"):
        await repository.retract(stored.event.id, reviewer_id=42, reason="wrong tool")


# Downtown Pittsburgh, matching the configured default home point.
_HOME = GeoPoint(40.4406, -79.9959)
_MAIN_CHANNEL = 500
_REGIONAL_CHANNEL = 501
# Mr Smalls in Millvale (~4 mi) and The Dance Cave in Toronto (~220 mi).
_LOCAL_COORDS = (40.4795, -79.9767)
_TORONTO_COORDS = (43.6650, -79.4103)


def _split_service(repository, gateway, **kwargs) -> PublicationService:
    return PublicationService(
        repository,
        gateway,
        announcement_channel_id=_MAIN_CHANNEL,
        regional_announcement_channel_id=_REGIONAL_CHANNEL,
        home=_HOME,
        local_radius_miles=75,
        **kwargs,
    )


async def _approved_at(repository, complete_event, coords, suffix: str) -> EventRecord:
    latitude, longitude = coords if coords else (None, None)
    return await _approved_event(
        repository,
        replace(
            complete_event,
            source_event_id=f"geo-{suffix}",
            title=f"Show {suffix}",
            venue_latitude=latitude,
            venue_longitude=longitude,
        ),
    )


@pytest.mark.asyncio
async def test_distant_show_goes_to_the_regional_channel_without_role_pings(
    repository, complete_event
) -> None:
    """The Toronto complaint: role mentions are a union, so a member cannot
    hold the goth role for local shows and drop it for distant ones. The
    split has to happen before Discord sees the message."""
    event = await _approved_at(repository, complete_event, _TORONTO_COORDS, "toronto")
    await repository.seed_genre_roles({"indie rock": 1})
    gateway = FakePublicationGateway()

    await _split_service(repository, gateway).publish(event.id)

    assert gateway.announcement_calls == [(event.id, (), 7001)]
    assert gateway.announcement_channels == [_REGIONAL_CHANNEL]


@pytest.mark.asyncio
async def test_local_show_keeps_its_role_pings_in_the_main_channel(
    repository, complete_event
) -> None:
    event = await _approved_at(repository, complete_event, _LOCAL_COORDS, "millvale")
    await repository.seed_genre_roles({"indie rock": 1})
    gateway = FakePublicationGateway()

    await _split_service(repository, gateway).publish(event.id)

    assert gateway.announcement_calls == [(event.id, (1,), 7001)]
    assert gateway.announcement_channels == [_MAIN_CHANNEL]


@pytest.mark.asyncio
async def test_show_without_coordinates_is_treated_as_local(
    repository, complete_event
) -> None:
    """A DIY room the address book does not cover is likelier to be in town
    than four states away, and burying a local show is the worse failure."""
    event = await _approved_at(repository, complete_event, None, "unmapped")
    await repository.seed_genre_roles({"indie rock": 1})
    gateway = FakePublicationGateway()

    await _split_service(repository, gateway).publish(event.id)

    assert gateway.announcement_calls == [(event.id, (1,), 7001)]
    assert gateway.announcement_channels == [_MAIN_CHANNEL]


@pytest.mark.asyncio
async def test_without_a_regional_channel_everything_routes_as_before(
    repository, complete_event
) -> None:
    event = await _approved_at(repository, complete_event, _TORONTO_COORDS, "no-split")
    await repository.seed_genre_roles({"indie rock": 1})
    gateway = FakePublicationGateway()
    service = PublicationService(
        repository,
        gateway,
        announcement_channel_id=_MAIN_CHANNEL,
        home=_HOME,
        local_radius_miles=75,
    )

    await service.publish(event.id)

    assert gateway.announcement_calls == [(event.id, (1,), 7001)]
    assert gateway.announcement_channels == [_MAIN_CHANNEL]


@pytest.mark.asyncio
async def test_publication_records_the_channel_it_announced_in(
    repository, complete_event
) -> None:
    """Announcements are re-found by scanning one channel's history for the
    event marker; without the channel on the row, a crash mid-publish would
    rescan the wrong one and post the show twice."""
    event = await _approved_at(repository, complete_event, _TORONTO_COORDS, "recorded")
    gateway = FakePublicationGateway()

    await _split_service(repository, gateway).publish(event.id)

    publication = await repository.get_publication(event.id)
    assert publication is not None
    assert publication["announcement_channel_id"] == str(_REGIONAL_CHANNEL)


@pytest.mark.asyncio
async def test_edits_stay_on_the_card_channel_and_never_add_a_ping(
    repository, complete_event
) -> None:
    """A venue correction can move an event across the local boundary long
    after publication. The message cannot move with it, and a card posted
    silently must not gain a role ping on its next edit."""
    event = await _approved_at(repository, complete_event, _TORONTO_COORDS, "moved")
    await repository.seed_genre_roles({"indie rock": 1})
    gateway = FakePublicationGateway()
    service = _split_service(repository, gateway)
    await service.publish(event.id)

    async with repository.database.connect() as connection:
        await connection.execute(
            "UPDATE events SET venue_latitude = ?, venue_longitude = ? WHERE id = ?",
            (*_LOCAL_COORDS, event.id),
        )
        await connection.commit()
    await service.update_existing(event.id)

    assert gateway.update_channels == [_REGIONAL_CHANNEL]
    assert gateway.update_calls[0][3] == ()


@pytest.mark.asyncio
async def test_legacy_card_without_a_stored_channel_is_edited_in_the_main_channel(
    repository, complete_event
) -> None:
    """Every card published before the split lives in the main channel.
    Routing one of those edits by distance would send an artwork backfill to
    the regional channel to edit a message that is not there."""
    event = await _approved_at(repository, complete_event, _TORONTO_COORDS, "legacy")
    gateway = FakePublicationGateway()
    plain = PublicationService(repository, gateway, announcement_channel_id=_MAIN_CHANNEL)
    await plain.publish(event.id)
    async with repository.database.connect() as connection:
        await connection.execute(
            "UPDATE publications SET announcement_channel_id = NULL WHERE event_id = ?",
            (event.id,),
        )
        await connection.commit()

    await _split_service(repository, gateway).update_existing(event.id)

    assert gateway.update_channels == [_MAIN_CHANNEL]


@pytest.mark.asyncio
async def test_shadow_mode_changes_nothing_that_goes_out(
    repository, complete_event, caplog
) -> None:
    """The point of shadow is that it is measurable and invisible: the
    announcement still pings the roles it always did."""
    event = await _approved_at(repository, complete_event, _LOCAL_COORDS, "shadow")
    await repository.seed_genre_roles({"indie rock": 1})
    await repository.upsert_user_profile(
        7,
        display_name="Avery",
        metro="pittsburgh",
        travel_band="road-trip",
        daily_ping_cap=5,
        source="role-seed",
    )
    await repository.add_user_taste(7, "genre", ("indie rock",), source="role-seed")
    gateway = FakePublicationGateway()
    service = PublicationService(
        repository,
        gateway,
        announcement_channel_id=_MAIN_CHANNEL,
        personal_delivery="shadow",
        bucket_roles={"indie rock": 1},
    )

    with caplog.at_level("INFO"):
        await service.publish(event.id)

    assert gateway.announcement_calls == [(event.id, (1,), 7001)]
    assert "Shadow delivery" in caplog.text and "Avery" in caplog.text


@pytest.mark.asyncio
async def test_shadow_reports_a_member_the_metro_filter_would_drop(
    repository, complete_event, caplog
) -> None:
    event = await _approved_at(repository, complete_event, _TORONTO_COORDS, "shadow-far")
    await repository.seed_genre_roles({"indie rock": 1})
    await repository.upsert_user_profile(
        7,
        display_name="Avery",
        metro="pittsburgh",
        travel_band="in-town",
        daily_ping_cap=5,
        source="role-seed",
    )
    await repository.add_user_taste(7, "genre", ("indie rock",), source="role-seed")
    gateway = FakePublicationGateway()
    service = PublicationService(
        repository,
        gateway,
        announcement_channel_id=_MAIN_CHANNEL,
        personal_delivery="shadow",
        bucket_roles={"indie rock": 1},
    )

    with caplog.at_level("INFO"):
        await service.publish(event.id)

    assert "0 member(s) in range, 1 filtered by metro" in caplog.text


@pytest.mark.asyncio
async def test_delivery_off_does_no_profile_work(repository, complete_event, caplog) -> None:
    event = await _approved_at(repository, complete_event, _LOCAL_COORDS, "no-shadow")
    gateway = FakePublicationGateway()
    with caplog.at_level("INFO"):
        await PublicationService(repository, gateway).publish(event.id)
    assert "Shadow delivery" not in caplog.text
