from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from music_event_bot.discovery.arcane import ArcaneCitySource, _local_datetime
from music_event_bot.discovery.base import DiscoveryWindow

EASTERN = ZoneInfo("America/New_York")


def _window() -> DiscoveryWindow:
    return DiscoveryWindow(
        starts_at=datetime(2026, 7, 19, 0, 0, tzinfo=EASTERN),
        ends_at=datetime(2026, 9, 19, 0, 0, tzinfo=EASTERN),
        default_timezone=EASTERN,
        default_event_duration_minutes=180,
    )


def _listing(*events: dict) -> str:
    payload = {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "mainEntity": {
            "@type": "ItemList",
            "itemListElement": [
                {"@type": "ListItem", "position": index, "item": event}
                for index, event in enumerate(events, start=1)
            ],
        },
    }
    return f'<script type="application/ld+json">{json.dumps(payload)}</script>'


def _detail(event: dict) -> str:
    body = {"@context": "https://schema.org", "@type": "Event", **event}
    return f'<script type="application/ld+json">{json.dumps(body)}</script>'


def _empty_pages(first: int = 2, last: int = 12) -> None:
    for page in range(first, last + 1):
        respx.get(f"https://arcane.city/events?page={page}").mock(
            return_value=httpx.Response(200, text=_listing())
        )


def test_fixed_winter_offset_is_read_as_local_wall_clock() -> None:
    """arcane.city stamps -0500 year-round, so summer events claim a winter offset.

    Taken literally a 1PM show becomes 2PM Eastern. The wall-clock half is the
    half they mean.
    """
    parsed = _local_datetime("2026-07-19T13:00:00-0500", EASTERN)
    assert parsed == datetime(2026, 7, 19, 13, 0, tzinfo=EASTERN)
    assert parsed.utcoffset().total_seconds() == -4 * 3600


def test_local_datetime_rejects_unparsable_values() -> None:
    assert _local_datetime("not a date", EASTERN) is None
    assert _local_datetime(None, EASTERN) is None


@respx.mock
@pytest.mark.asyncio
async def test_discover_reads_lineup_and_artwork_from_the_event_page() -> None:
    respx.get("https://arcane.city/events?page=1").mock(
        return_value=httpx.Response(
            200,
            text=_listing(
                {
                    "@type": "Event",
                    "name": "C Powers, Nick Boyd, Naeem",
                    "startDate": "2026-08-29T23:00:00-0500",
                    "url": "https://arcane.city/events/c-powers",
                    "location": {"@type": "Place", "name": "Hot Mass"},
                }
            ),
        )
    )
    _empty_pages()
    respx.get("https://arcane.city/events/c-powers").mock(
        return_value=httpx.Response(
            200,
            text=_detail(
                {
                    "name": "C Powers, Nick Boyd, Naeem",
                    "startDate": "2026-08-29T23:00:00-0500",
                    "endDate": "2026-08-30T06:00:00-0500",
                    "image": ["https://cdn.arcane.test/flyer.webp"],
                    "location": {"@type": "Place", "name": "Hot Mass"},
                    "description": "All night long.",
                    "performer": [
                        {"@type": "PerformingGroup", "name": "C Powers"},
                        {"@type": "PerformingGroup", "name": "Nick Boyd"},
                    ],
                }
            ),
        )
    )

    async with httpx.AsyncClient() as client:
        events = await ArcaneCitySource(client=client).discover(_window())

    assert len(events) == 1
    event = events[0]
    assert event.title == "C Powers, Nick Boyd, Naeem"
    assert event.venue == "Hot Mass"
    assert event.image_url == "https://cdn.arcane.test/flyer.webp"
    assert event.artists == ("C Powers", "Nick Boyd")
    assert event.artist == "C Powers"
    assert event.starts_at == datetime(2026, 8, 29, 23, 0, tzinfo=EASTERN)
    assert event.ends_at == datetime(2026, 8, 30, 6, 0, tzinfo=EASTERN)
    assert event.source_event_id == "c-powers"


@respx.mock
@pytest.mark.asyncio
async def test_events_outside_the_window_are_dropped() -> None:
    respx.get("https://arcane.city/events?page=1").mock(
        return_value=httpx.Response(
            200,
            text=_listing(
                {
                    "@type": "Event",
                    "name": "Too Early",
                    "startDate": "2026-07-01T20:00:00-0500",
                    "url": "https://arcane.city/events/early",
                },
                {
                    "@type": "Event",
                    "name": "Too Late",
                    "startDate": "2026-12-01T20:00:00-0500",
                    "url": "https://arcane.city/events/late",
                },
            ),
        )
    )
    _empty_pages()
    detail = respx.get("https://arcane.city/events/late")

    async with httpx.AsyncClient() as client:
        events = await ArcaneCitySource(client=client).discover(_window())

    assert events == []
    assert not detail.called


@respx.mock
@pytest.mark.asyncio
async def test_paging_stops_once_the_listing_passes_the_window() -> None:
    """The listing is chronological, so a later page cannot come back in range."""
    respx.get("https://arcane.city/events?page=1").mock(
        return_value=httpx.Response(
            200,
            text=_listing(
                {
                    "@type": "Event",
                    "name": "Way Out",
                    "startDate": "2026-12-01T20:00:00-0500",
                    "url": "https://arcane.city/events/way-out",
                }
            ),
        )
    )
    second = respx.get("https://arcane.city/events?page=2")

    async with httpx.AsyncClient() as client:
        assert await ArcaneCitySource(client=client).discover(_window()) == []
    assert not second.called


@respx.mock
@pytest.mark.asyncio
async def test_placeholder_artwork_is_not_carried_over() -> None:
    respx.get("https://arcane.city/events?page=1").mock(
        return_value=httpx.Response(
            200,
            text=_listing(
                {
                    "@type": "Event",
                    "name": "Some Show",
                    "startDate": "2026-08-01T20:00:00-0500",
                    "url": "https://arcane.city/events/some-show",
                }
            ),
        )
    )
    _empty_pages()
    respx.get("https://arcane.city/events/some-show").mock(
        return_value=httpx.Response(
            200,
            text=_detail(
                {
                    "name": "Some Show",
                    "startDate": "2026-08-01T20:00:00-0500",
                    "image": ["https://cdn.test/poster.jfif"],
                }
            ),
        )
    )

    async with httpx.AsyncClient() as client:
        events = await ArcaneCitySource(client=client).discover(_window())

    assert len(events) == 1
    assert events[0].image_url is None


@respx.mock
@pytest.mark.asyncio
async def test_an_unreachable_detail_page_still_yields_the_listed_event() -> None:
    respx.get("https://arcane.city/events?page=1").mock(
        return_value=httpx.Response(
            200,
            text=_listing(
                {
                    "@type": "Event",
                    "name": "Listing Only",
                    "startDate": "2026-08-01T20:00:00-0500",
                    "url": "https://arcane.city/events/listing-only",
                    "location": {"@type": "Place", "name": "Some Room"},
                    "description": "From the listing.",
                }
            ),
        )
    )
    _empty_pages()
    respx.get("https://arcane.city/events/listing-only").mock(
        return_value=httpx.Response(503)
    )

    async with httpx.AsyncClient() as client:
        events = await ArcaneCitySource(client=client).discover(_window())

    assert len(events) == 1
    assert events[0].venue == "Some Room"
    assert events[0].description == "From the listing."
    assert events[0].image_url is None


@respx.mock
@pytest.mark.asyncio
async def test_raw_newlines_in_a_description_do_not_lose_the_flyer() -> None:
    """Promoter blurbs are pasted in with their newlines intact.

    Strict JSON forbids a literal control character inside a string, so a lone
    newline in the description would otherwise discard the whole block, taking
    the artwork and lineup with it.
    """
    respx.get("https://arcane.city/events?page=1").mock(
        return_value=httpx.Response(
            200,
            text=_listing(
                {
                    "@type": "Event",
                    "name": "Hot Mass",
                    "startDate": "2026-08-29T23:00:00-0500",
                    "url": "https://arcane.city/events/hot-mass",
                }
            ),
        )
    )
    _empty_pages()
    respx.get("https://arcane.city/events/hot-mass").mock(
        return_value=httpx.Response(
            200,
            text=(
                '<script type="application/ld+json">'
                '{"@context":"https://schema.org","@type":"Event","name":"Hot Mass",'
                '"startDate":"2026-08-29T23:00:00-0500",'
                '"description":"Doors 11pm.\nAdvance tickets required.",'
                '"image":["https://cdn.arcane.test/flyer.webp"]}'
                "</script>"
            ),
        )
    )

    async with httpx.AsyncClient() as client:
        events = await ArcaneCitySource(client=client).discover(_window())

    assert len(events) == 1
    assert events[0].image_url == "https://cdn.arcane.test/flyer.webp"


@respx.mock
@pytest.mark.asyncio
async def test_source_faces_the_affinity_gate() -> None:
    """A whole-city listing gets no curation credit."""
    assert ArcaneCitySource().requires_affinity is True
