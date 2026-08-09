from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from music_event_bot.discovery.squarespace import SquarespaceSource


def _payload(*items: dict[str, Any]) -> dict[str, Any]:
    # Real Squarespace event collections use "upcoming"/"past", not "items".
    return {
        "website": {"siteTitle": "The Goldmark"},
        "upcoming": list(items),
        "past": [_past_item()],
    }


def _past_item() -> dict[str, Any]:
    return {
        "id": "past1",
        "title": "Old Show",
        "startDate": int(datetime(2026, 6, 1, tzinfo=UTC).timestamp() * 1000),
    }


def _item(**overrides: Any) -> dict[str, Any]:
    item = {
        "id": "abc123",
        "title": "Night of Noise &amp; Friends",
        "startDate": int(datetime(2026, 7, 20, 1, 0, tzinfo=UTC).timestamp() * 1000),
        "endDate": int(datetime(2026, 7, 20, 4, 0, tzinfo=UTC).timestamp() * 1000),
        "fullUrl": "/events/night-of-noise",
        "assetUrl": "https://images.squarespace-cdn.com/poster.jpg",
        "excerpt": "<p>Loud <em>music</em> downstairs.</p>",
        "location": {
            "addressTitle": "",
            "addressLine1": "4517 Butler St",
            "addressLine2": "Pittsburgh, PA",
        },
        "categories": ["Live Music"],
        "tags": ["noise"],
    }
    item.update(overrides)
    return item


@pytest.mark.asyncio
async def test_squarespace_parses_event_collection(discovery_window) -> None:
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://www.thegoldmark.com/events", params={"format": "json"}
        ).mock(return_value=httpx.Response(200, json=_payload(_item())))
        async with httpx.AsyncClient() as client:
            events = await SquarespaceSource(
                ("https://www.thegoldmark.com/events",), client
            ).discover(discovery_window)

    assert len(events) == 1
    event = events[0]
    assert event.source_name == "squarespace"
    assert event.source_event_id == "abc123"
    assert event.title == "Night of Noise & Friends"
    assert event.venue == "The Goldmark"
    assert event.location == "4517 Butler St, Pittsburgh, PA"
    assert event.starts_at == datetime(2026, 7, 20, 1, 0, tzinfo=UTC)
    assert event.source_url == "https://www.thegoldmark.com/events/night-of-noise"
    assert event.description == "Loud music downstairs."
    assert event.genres == ("Live Music", "noise")
    assert event.incomplete_reasons == ()


@pytest.mark.asyncio
async def test_squarespace_window_filter_and_missing_dates(discovery_window) -> None:
    outside = _item(
        id="outside",
        startDate=int(datetime(2027, 1, 1, tzinfo=UTC).timestamp() * 1000),
    )
    undated = _item(id="undated", startDate=None, endDate=None)
    with respx.mock() as router:
        router.get(
            "https://www.thegoldmark.com/events", params={"format": "json"}
        ).mock(return_value=httpx.Response(200, json=_payload(outside, undated)))
        async with httpx.AsyncClient() as client:
            events = await SquarespaceSource(
                ("https://www.thegoldmark.com/events",), client
            ).discover(discovery_window)

    # The out-of-window event is dropped; the undated one is held for editing.
    assert [event.source_event_id for event in events] == ["undated"]
    assert "missing start time" in events[0].incomplete_reasons


@pytest.mark.asyncio
async def test_one_failing_squarespace_site_does_not_lose_the_others(
    discovery_window,
) -> None:
    with respx.mock() as router:
        router.get(
            "https://broken.example.test/events", params={"format": "json"}
        ).mock(return_value=httpx.Response(403))
        router.get(
            "https://www.thegoldmark.com/events", params={"format": "json"}
        ).mock(return_value=httpx.Response(200, json=_payload(_item())))
        async with httpx.AsyncClient() as client:
            events = await SquarespaceSource(
                (
                    "https://broken.example.test/events",
                    "https://www.thegoldmark.com/events",
                ),
                client,
            ).discover(discovery_window)

    assert len(events) == 1
    assert events[0].source_event_id == "abc123"


@pytest.mark.asyncio
async def test_squarespace_decodes_entities_in_venue_and_address(discovery_window) -> None:
    """Only the item title was being unescaped, so venues arrived HTML-escaped.

    A promoter collection surfaced "Don&#39;t Let the Scene Go Down On Me!
    Collective" as a venue. The fingerprint is title|venue|start, so the same
    room written escaped and unescaped is two different venues.
    """
    payload = _payload(
        _item(
            location={
                "addressTitle": "Don&#39;t Let the Scene Go Down On Me!",
                "addressLine1": "Smith &amp; Wesson Ave",
                "addressLine2": "Pittsburgh, PA",
            }
        )
    )
    payload["website"] = {"siteTitle": "Roboto &amp; Friends"}
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://www.thegoldmark.com/events", params={"format": "json"}
        ).mock(return_value=httpx.Response(200, json=payload))
        async with httpx.AsyncClient() as client:
            events = await SquarespaceSource(
                ("https://www.thegoldmark.com/events",), client
            ).discover(discovery_window)

    assert len(events) == 1
    event = events[0]
    assert event.venue == "Don't Let the Scene Go Down On Me!"
    assert event.location is not None
    assert "Smith & Wesson Ave" in event.location
    assert "&#39;" not in (event.venue or "")
    assert "&amp;" not in (event.location or "")


@pytest.mark.asyncio
async def test_squarespace_site_title_is_decoded_when_no_address_title(
    discovery_window,
) -> None:
    """The site title is the venue fallback, so it needs decoding too."""
    payload = _payload(_item(location={"addressTitle": "", "addressLine1": "1 Main St"}))
    payload["website"] = {"siteTitle": "Gooski&#39;s"}
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://www.thegoldmark.com/events", params={"format": "json"}
        ).mock(return_value=httpx.Response(200, json=payload))
        async with httpx.AsyncClient() as client:
            events = await SquarespaceSource(
                ("https://www.thegoldmark.com/events",), client
            ).discover(discovery_window)

    assert events[0].venue == "Gooski's"
