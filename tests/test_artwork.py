from __future__ import annotations

import httpx
import pytest
import respx

from music_event_bot.discovery.artwork import (
    ArtworkResolver,
    _candidate_pages,
    _is_index_page,
    extract_og_image,
)
from music_event_bot.domain.models import DiscoveredEvent


def _event(description: str, source_url: str = "") -> DiscoveredEvent:
    return DiscoveredEvent(
        source_name="ics",
        source_event_id="evt-1",
        title="Some Band",
        venue="Preserving Underground",
        location="1101 5th Ave, New Kensington, PA",
        starts_at=None,
        ends_at=None,
        timezone="America/New_York",
        source_url=source_url,
        genres=(),
        description=description,
    )


def test_extract_og_image_handles_attribute_order_and_relative_urls() -> None:
    both_orders = [
        '<meta property="og:image" content="/flyer.jpg">',
        '<meta content="/flyer.jpg" property="og:image">',
        "<meta name='og:image:url' content='/flyer.jpg'>",
    ]
    for markup in both_orders:
        assert extract_og_image(markup, "https://venue.test/e/1") == "https://venue.test/flyer.jpg"


def test_extract_og_image_falls_back_to_twitter_card() -> None:
    markup = '<meta name="twitter:image" content="https://cdn.test/x.jpg">'
    assert extract_og_image(markup, "https://venue.test/") == "https://cdn.test/x.jpg"


def test_extract_og_image_returns_none_without_a_tag() -> None:
    assert extract_og_image("<html><body>no meta here</body></html>", "https://a.test") is None


def test_candidate_pages_drops_the_calendar_and_deprioritises_drusky() -> None:
    event = _event(
        "Tickets: https://druskyentertainment.com/event/x/ "
        "Info: https://www.preservingconcerts.com/events/x "
        "Source: https://calendar.google.com/calendar/ical/abc/basic.ics"
    )
    pages = _candidate_pages(event)
    assert "calendar.google.com" not in " ".join(pages)
    # The venue's own page must be tried before the promoter's.
    assert pages[0] == "https://www.preservingconcerts.com/events/x"
    assert pages[1] == "https://druskyentertainment.com/event/x/"


def _plain_root(*hosts: str) -> None:
    """Front pages with no og:image, so the site-default check finds nothing."""
    for host in hosts:
        respx.get(f"{host}/").mock(return_value=httpx.Response(200, text="<html></html>"))


@respx.mock
@pytest.mark.asyncio
async def test_resolve_prefers_venue_page_over_promoter() -> None:
    _plain_root("https://www.preservingconcerts.com")
    respx.get("https://www.preservingconcerts.com/events/x").mock(
        return_value=httpx.Response(
            200, text='<meta property="og:image" content="https://cdn.test/flyer.jpg">'
        )
    )
    respx.get("https://cdn.test/flyer.jpg").mock(
        return_value=httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"x")
    )
    drusky = respx.get("https://druskyentertainment.com/event/x/")

    event = _event(
        "https://druskyentertainment.com/event/x/ https://www.preservingconcerts.com/events/x"
    )
    async with httpx.AsyncClient() as client:
        assert await ArtworkResolver(client=client).resolve(event) == "https://cdn.test/flyer.jpg"
    assert not drusky.called


@respx.mock
@pytest.mark.asyncio
async def test_resolve_rejects_non_image_content_type() -> None:
    """Drusky's .jfif uploads return 200 but render as nothing in Discord."""
    _plain_root("https://promoter.test")
    respx.get("https://promoter.test/e").mock(
        return_value=httpx.Response(
            200, text='<meta property="og:image" content="https://promoter.test/f.png">'
        )
    )
    respx.get("https://promoter.test/f.png").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "application/octet-stream"}, content=b"x"
        )
    )
    async with httpx.AsyncClient() as client:
        resolver = ArtworkResolver(client=client)
        assert await resolver.resolve(_event("https://promoter.test/e")) is None


@respx.mock
@pytest.mark.asyncio
async def test_resolve_rejects_known_placeholder_art() -> None:
    placeholder = (
        "https://druskyentertainment.com/wp-content/uploads/"
        "Drusky-Entertainment-Default-Event-Image-1200x627-v2.png"
    )
    respx.get("https://promoter.test/e").mock(
        return_value=httpx.Response(200, text=f'<meta property="og:image" content="{placeholder}">')
    )
    async with httpx.AsyncClient() as client:
        resolver = ArtworkResolver(client=client)
        assert await resolver.resolve(_event("https://promoter.test/e")) is None


@respx.mock
@pytest.mark.asyncio
async def test_pages_are_fetched_once_per_run() -> None:
    """Venue pages repeat across a night's events; the cache must absorb that."""
    _plain_root("https://venue.test")
    page = respx.get("https://venue.test/e").mock(
        return_value=httpx.Response(
            200, text='<meta property="og:image" content="https://cdn.test/a.jpg">'
        )
    )
    image = respx.get("https://cdn.test/a.jpg").mock(
        return_value=httpx.Response(200, headers={"content-type": "image/png"}, content=b"x")
    )
    resolver = ArtworkResolver()
    async with httpx.AsyncClient() as client:
        resolver._client = client
        for _ in range(3):
            assert await resolver.resolve(_event("https://venue.test/e")) == "https://cdn.test/a.jpg"
    assert page.call_count == 1
    assert image.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_unreachable_page_is_not_fatal() -> None:
    respx.get("https://venue.test/e").mock(side_effect=httpx.ConnectError("boom"))
    async with httpx.AsyncClient() as client:
        assert await ArtworkResolver(client=client).resolve(_event("https://venue.test/e")) is None


@pytest.mark.asyncio
async def test_event_without_links_makes_no_requests() -> None:
    assert await ArtworkResolver().resolve(_event("No links in this description.")) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://venue.test/",
        "https://venue.test",
        "https://venue.test/calendar.html",
        "https://venue.test/events",
        "https://venue.test/shows/",
    ],
)
def test_listing_indexes_are_recognised(url: str) -> None:
    assert _is_index_page(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://venue.test/event/the-voodoo-child/",
        "https://venue.test/events/eyehategod-1",
        "https://venue.test/calendar/2026-07-26-some-band",
    ],
)
def test_per_show_pages_are_not_indexes(url: str) -> None:
    assert not _is_index_page(url)


@respx.mock
@pytest.mark.asyncio
async def test_index_page_art_is_never_used() -> None:
    """A calendar index's og:image is the venue's header, not a show flyer."""
    index = respx.get("https://venue.test/calendar.html").mock(
        return_value=httpx.Response(
            200, text='<meta property="og:image" content="https://cdn.test/header.jpg">'
        )
    )
    async with httpx.AsyncClient() as client:
        resolver = ArtworkResolver(client=client)
        assert await resolver.resolve(_event("https://venue.test/calendar.html")) is None
    assert not index.called


@respx.mock
@pytest.mark.asyncio
async def test_site_wide_social_image_is_rejected() -> None:
    """When the front page carries the same art, it describes the venue."""
    respx.get("https://venue.test/").mock(
        return_value=httpx.Response(
            200, text='<meta property="og:image" content="https://cdn.test/logo.png">'
        )
    )
    respx.get("https://venue.test/event/a").mock(
        return_value=httpx.Response(
            200, text='<meta property="og:image" content="https://cdn.test/logo.png">'
        )
    )
    async with httpx.AsyncClient() as client:
        resolver = ArtworkResolver(client=client)
        assert await resolver.resolve(_event("https://venue.test/event/a")) is None


@respx.mock
@pytest.mark.asyncio
async def test_banner_shared_by_two_shows_is_refused_after_the_first() -> None:
    """Some sites banner every per-show page; two claims prove it is not show art."""
    _plain_root("https://venue.test")
    for slug in ("a", "b"):
        respx.get(f"https://venue.test/event/{slug}").mock(
            return_value=httpx.Response(
                200, text='<meta property="og:image" content="https://cdn.test/banner.jpg">'
            )
        )
    respx.get("https://cdn.test/banner.jpg").mock(
        return_value=httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"x")
    )
    async with httpx.AsyncClient() as client:
        resolver = ArtworkResolver(client=client)
        first = await resolver.resolve(_event("https://venue.test/event/a"))
        second = await resolver.resolve(_event("https://venue.test/event/b"))
    assert first == "https://cdn.test/banner.jpg"
    assert second is None
    # The first caller was handed it before the duplicate proved it generic, so
    # callers that can still take it back are told which images to drop.
    assert resolver.shared_images == {"https://cdn.test/banner.jpg"}


@respx.mock
@pytest.mark.asyncio
async def test_unshared_art_is_not_reported_as_venue_art() -> None:
    _plain_root("https://venue.test")
    respx.get("https://venue.test/event/a").mock(
        return_value=httpx.Response(
            200, text='<meta property="og:image" content="https://cdn.test/flyer.jpg">'
        )
    )
    respx.get("https://cdn.test/flyer.jpg").mock(
        return_value=httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"x")
    )
    async with httpx.AsyncClient() as client:
        resolver = ArtworkResolver(client=client)
        assert await resolver.resolve(_event("https://venue.test/event/a"))
        assert resolver.shared_images == frozenset()
