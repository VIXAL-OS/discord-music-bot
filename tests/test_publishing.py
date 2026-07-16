from __future__ import annotations

import pytest

from music_event_bot.domain.models import EventRecord, EventStatus, ScoreResult
from music_event_bot.services.publishing import PublicationService


class FakePublicationGateway:
    def __init__(self) -> None:
        self.scheduled_calls: list[str] = []
        self.announcement_calls: list[tuple[str, int | None]] = []
        self.update_calls: list[tuple[str, int, int, int | None]] = []
        self.announcement_error: Exception | None = None

    async def create_or_find_scheduled_event(self, event: EventRecord) -> int:
        self.scheduled_calls.append(event.id)
        return 7001

    async def create_or_find_announcement(self, event: EventRecord, role_id: int | None) -> int:
        self.announcement_calls.append((event.id, role_id))
        if self.announcement_error is not None:
            raise self.announcement_error
        return 8001

    async def update_published_event(
        self,
        event: EventRecord,
        scheduled_event_id: int,
        announcement_message_id: int,
        role_id: int | None,
    ) -> None:
        self.update_calls.append((event.id, scheduled_event_id, announcement_message_id, role_id))


async def _approved_event(repository, complete_event) -> EventRecord:
    stored = await repository.upsert_discovered(complete_event, ScoreResult(score=10, reasons=()))
    return await repository.approve(stored.event.id, reviewer_id=42)


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
    assert gateway.announcement_calls == [(event.id, 1234)]
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
    assert gateway.announcement_calls == [(event.id, None), (event.id, None)]
    assert completed_publication is not None
    assert completed_publication["state"] == "published"
    assert completed_publication["attempts"] == 2
