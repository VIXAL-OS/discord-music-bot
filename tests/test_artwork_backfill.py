from __future__ import annotations

from dataclasses import replace

import httpx
import pytest
import respx

from music_event_bot.discovery.artwork import ArtworkResolver
from music_event_bot.domain.models import DiscoveredEvent, ScoreResult
from music_event_bot.services.artwork_backfill import ArtworkBackfill
from music_event_bot.storage.repositories import EventRepository


async def _store(repository: EventRepository, event: DiscoveredEvent) -> str:
    """Store an event approved, one of the statuses the backfill scans.

    Publishing outright would need a Discord publication row these tests have no
    use for; approval reaches the same code path.
    """
    result = await repository.upsert_discovered(event, ScoreResult(score=90, reasons=()))
    await repository.approve(result.event.id, reviewer_id=1)
    return result.event.id


def _plain_root(*hosts: str) -> None:
    for host in hosts:
        respx.get(f"{host}/").mock(return_value=httpx.Response(200, text="<html></html>"))


@respx.mock
@pytest.mark.asyncio
async def test_backfill_reports_without_writing_by_default(
    repository: EventRepository, complete_event: DiscoveredEvent
) -> None:
    _plain_root("https://venue.test")
    respx.get("https://venue.test/event/a").mock(
        return_value=httpx.Response(
            200, text='<meta property="og:image" content="https://cdn.test/flyer.jpg">'
        )
    )
    respx.get("https://cdn.test/flyer.jpg").mock(
        return_value=httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"x")
    )
    event_id = await _store(
        repository, replace(complete_event, source_url="https://venue.test/event/a")
    )

    async with httpx.AsyncClient() as client:
        summary = await ArtworkBackfill(repository, ArtworkResolver(client=client)).run()

    assert summary.without_image == 1
    assert summary.matches == {event_id: "https://cdn.test/flyer.jpg"}
    assert summary.applied == 0
    stored = await repository.get_event(event_id)
    assert stored is not None and stored.image_url is None


@respx.mock
@pytest.mark.asyncio
async def test_backfill_writes_images_with_apply(
    repository: EventRepository, complete_event: DiscoveredEvent
) -> None:
    _plain_root("https://venue.test")
    respx.get("https://venue.test/event/a").mock(
        return_value=httpx.Response(
            200, text='<meta property="og:image" content="https://cdn.test/flyer.jpg">'
        )
    )
    respx.get("https://cdn.test/flyer.jpg").mock(
        return_value=httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"x")
    )
    event_id = await _store(
        repository, replace(complete_event, source_url="https://venue.test/event/a")
    )

    async with httpx.AsyncClient() as client:
        summary = await ArtworkBackfill(repository, ArtworkResolver(client=client)).run(apply=True)

    assert summary.applied == 1
    stored = await repository.get_event(event_id)
    assert stored is not None and stored.image_url == "https://cdn.test/flyer.jpg"


@respx.mock
@pytest.mark.asyncio
async def test_backfill_refuses_art_shared_across_events(
    repository: EventRepository, complete_event: DiscoveredEvent
) -> None:
    """The Poetry Lounge case: one venue wordmark served on every event page.

    Discovery hands it to whichever event asks first, because nothing proves it
    generic until a second event turns up with it. Resolving every event before
    writing any of them closes that window.
    """
    _plain_root("https://venue.test")
    for slug in ("a", "b"):
        respx.get(f"https://venue.test/event/{slug}").mock(
            return_value=httpx.Response(
                200, text='<meta property="og:image" content="https://cdn.test/logo.png">'
            )
        )
    respx.get("https://cdn.test/logo.png").mock(
        return_value=httpx.Response(200, headers={"content-type": "image/png"}, content=b"x")
    )
    first = await _store(
        repository,
        replace(
            complete_event,
            source_event_id="a",
            title="First Band",
            source_url="https://venue.test/event/a",
        ),
    )
    second = await _store(
        repository,
        replace(
            complete_event,
            source_event_id="b",
            title="Second Band",
            source_url="https://venue.test/event/b",
        ),
    )

    async with httpx.AsyncClient() as client:
        summary = await ArtworkBackfill(repository, ArtworkResolver(client=client)).run(apply=True)

    assert summary.applied == 0
    assert summary.shared_art_skipped == 1
    assert summary.matches == {}
    for event_id in (first, second):
        stored = await repository.get_event(event_id)
        assert stored is not None and stored.image_url is None


@respx.mock
@pytest.mark.asyncio
async def test_backfill_leaves_events_that_already_have_art(
    repository: EventRepository, complete_event: DiscoveredEvent
) -> None:
    page = respx.get("https://venue.test/event/a")
    await _store(
        repository,
        replace(
            complete_event,
            source_url="https://venue.test/event/a",
            image_url="https://cdn.test/existing.jpg",
        ),
    )

    async with httpx.AsyncClient() as client:
        summary = await ArtworkBackfill(repository, ArtworkResolver(client=client)).run(apply=True)

    assert summary.without_image == 0
    assert not page.called
