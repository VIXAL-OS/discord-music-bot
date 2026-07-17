from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from music_event_bot.domain.models import EventRecord, EventStatus, ScoreResult
from music_event_bot.services.publishing import PublicationService

_EASTERN = ZoneInfo("America/New_York")
# 2026-07-17 12:00 EDT / 03:00 EDT expressed in UTC.
_NOON_LOCAL = datetime(2026, 7, 17, 16, 0, tzinfo=UTC)
_NIGHT_LOCAL = datetime(2026, 7, 17, 7, 0, tzinfo=UTC)


class FakePublicationGateway:
    def __init__(self) -> None:
        self.scheduled_calls: list[str] = []
        self.announcement_calls: list[tuple[str, tuple[int, ...]]] = []
        self.update_calls: list[tuple[str, int, int, tuple[int, ...]]] = []
        self.announcement_error: Exception | None = None

    async def create_or_find_scheduled_event(self, event: EventRecord) -> int:
        self.scheduled_calls.append(event.id)
        return 7001

    async def create_or_find_announcement(
        self, event: EventRecord, role_ids: tuple[int, ...]
    ) -> int:
        self.announcement_calls.append((event.id, role_ids))
        if self.announcement_error is not None:
            raise self.announcement_error
        return 8001

    async def update_published_event(
        self,
        event: EventRecord,
        scheduled_event_id: int,
        announcement_message_id: int,
        role_ids: tuple[int, ...],
    ) -> None:
        self.update_calls.append(
            (event.id, scheduled_event_id, announcement_message_id, role_ids)
        )


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
    assert gateway.announcement_calls == [(event.id, (2, 1))]


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
    assert gateway.announcement_calls == [(event.id, (99,))]


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
    assert gateway.announcement_calls == [(event.id, (1234,))]
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
    assert gateway.announcement_calls == [(event.id, ()), (event.id, ())]
    assert completed_publication is not None
    assert completed_publication["state"] == "published"
    assert completed_publication["attempts"] == 2
