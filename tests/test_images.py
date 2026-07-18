from __future__ import annotations

import pytest

from music_event_bot.discovery.images import is_publishable_image


@pytest.mark.parametrize(
    "url",
    [
        "https://s1.ticketm.net/dam/a/441/51fb10a1-real.jpg",
        "https://s1.ticketm.net/dam/e/d87/2b35db84-event.jpg",
        "https://f4.bcbits.com/img/a2451177505_10.jpg",
        "https://i.ticketweb.com/i/00/12/89/18/24_Edp.jpg?v=3",
        "https://druskyentertainment.com/wp-content/uploads/2026/05/kyle-gordon.jpg",
    ],
)
def test_real_artwork_is_kept(url: str) -> None:
    assert is_publishable_image(url)


@pytest.mark.parametrize(
    "url",
    [
        # Ticketmaster generic genre stock art.
        "https://s1.ticketm.net/dam/c/e7c/86478562-stock.jpg",
        "https://S1.TICKETM.NET/DAM/C/f50/96fa13be-stock.jpg",
        # Drusky's house placeholder.
        "https://druskyentertainment.com/wp-content/uploads/2024/01/"
        "Drusky-Entertainment-Default-Event-Image-1200x627-v2.png",
        # Served as application/octet-stream with nosniff, so Discord renders nothing.
        "https://druskyentertainment.com/wp-content/uploads/2026/05/kyle-gordon.jfif",
        "https://druskyentertainment.com/wp-content/uploads/2026/02/spg.JFIF?ver=2",
        "https://example.test/flyer.jpe",
        None,
        "",
    ],
)
def test_unpublishable_artwork_is_rejected(url: str | None) -> None:
    assert not is_publishable_image(url)


def test_query_string_does_not_hide_the_suffix() -> None:
    """A cache-buster must not smuggle a .jfif past the extension check."""
    assert not is_publishable_image("https://host.test/a.jfif?w=900&q=85")
    # ...but a .jfif appearing only as a query value is not the asset's own type.
    assert is_publishable_image("https://host.test/real.jpg?ref=a.jfif")
