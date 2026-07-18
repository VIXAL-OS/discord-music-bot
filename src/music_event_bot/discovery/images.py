"""Shared rules for artwork that must never reach an announcement embed.

Every rule here is decidable from the URL alone, so sources can apply it while
parsing without paying for a network round trip.
"""

from __future__ import annotations

from urllib.parse import urlsplit

# Ticketmaster serves attraction art under /dam/a/ and event art under /dam/e/, but
# falls back to /dam/c/ "category" assets -- generic genre stock photos (an anonymous
# guitarist in smoke, a laser show, a tambourine close-up) reused verbatim across
# every unrelated show in the genre. Drusky Entertainment keeps a house placeholder
# that serves the same purpose. Neither depicts the act.
_PLACEHOLDER_MARKERS = (
    "/dam/c/",
    "drusky-entertainment-default-event-image",
)

# Drusky uploads flyers as .jfif, and its server returns them as
# application/octet-stream alongside X-Content-Type-Options: nosniff, which forbids
# clients from sniffing the real type from the bytes. Discord will not render those,
# so the embed comes out blank -- worse than having no image at all, because an empty
# image_url is visible in review while a blank render is not.
_UNRENDERABLE_SUFFIXES = (".jfif", ".jpe")


def is_publishable_image(url: str | None) -> bool:
    """True when the URL is worth storing as an event's artwork."""
    if not url:
        return False
    lowered = url.lower()
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
        return False
    path = urlsplit(lowered).path
    return not path.endswith(_UNRENDERABLE_SUFFIXES)
