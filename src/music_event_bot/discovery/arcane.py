"""Discover Pittsburgh events from arcane.city.

Arcane City is a community-run listing that covers the underground bookings the
ticketing APIs never see -- secret-location after-hours parties, DIY spaces, and
one-off promoter nights -- and it carries the official flyer for most of them.
Resident Advisor holds much of the same material but rejects every scripted
request, so this is the only reachable route to it.

Every page embeds schema.org JSON-LD, so there is no markup to track: the listing
is an ItemList of Events and each event page repeats itself as a full Event with
the lineup and artwork attached.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from music_event_bot.discovery.base import DiscoveryWindow
from music_event_bot.discovery.images import is_publishable_image
from music_event_bot.domain.models import DiscoveredEvent

logger = logging.getLogger(__name__)

BASE_URL = "https://arcane.city"

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_LD_JSON_RE = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)

# Trailing UTC offset, or a Z, on an ISO-8601 timestamp.
_OFFSET_RE = re.compile(r"(?:Z|[+-]\d{2}:?\d{2})$")

# The listing is chronological, so paging stops as soon as it passes the window.
# This only bounds a listing that never does.
_MAX_PAGES = 12

# Detail pages are fetched per in-window event; a handful at a time is plenty to
# keep a run short without leaning on a volunteer-run site.
_DETAIL_CONCURRENCY = 5


def _local_datetime(value: str | None, timezone: ZoneInfo) -> datetime | None:
    """Read an arcane.city timestamp as local wall-clock time.

    Their timestamps carry a fixed -0500 offset year-round, so every summer event
    claims a winter offset: a show described as 1PM is published as
    ``13:00:00-0500``, which is 2PM once resolved against Eastern daylight time.
    The wall-clock half is the half they mean, so the offset is dropped and the
    configured local zone attached instead.
    """
    if not value:
        return None
    naive = _OFFSET_RE.sub("", value.strip())
    try:
        parsed = datetime.fromisoformat(naive)
    except ValueError:
        logger.debug("Unparsable arcane.city timestamp %r", value)
        return None
    return parsed.replace(tzinfo=timezone)


def _ld_blocks(html: str) -> list[Any]:
    blocks: list[Any] = []
    for raw in _LD_JSON_RE.findall(html):
        try:
            # strict=False: descriptions are pasted straight from promoters and
            # keep their raw newlines and tabs, which strict JSON forbids inside a
            # string. Refusing those drops the whole block -- and with it the
            # flyer and lineup -- over whitespace.
            blocks.append(json.loads(raw, strict=False))
        except json.JSONDecodeError:
            continue
    return blocks


def _listed_events(html: str) -> list[dict[str, Any]]:
    """The Event objects from a listing page's ItemList."""
    for block in _ld_blocks(html):
        if not isinstance(block, dict):
            continue
        entries = block.get("mainEntity", {}).get("itemListElement")
        if not isinstance(entries, list):
            continue
        return [
            entry["item"]
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("item"), dict)
        ]
    return []


def _detail_event(html: str) -> dict[str, Any] | None:
    for block in _ld_blocks(html):
        if isinstance(block, dict) and block.get("@type") == "Event":
            return block
    return None


def _first_image(value: Any) -> str | None:
    if isinstance(value, str):
        candidate = value
    elif isinstance(value, list) and value:
        candidate = value[0] if isinstance(value[0], str) else None
    else:
        candidate = None
    return candidate if is_publishable_image(candidate) else None


def _performers(value: Any) -> tuple[str, ...]:
    entries = value if isinstance(value, list) else [value]
    names = []
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            name = entry["name"].strip()
            if name and name not in names:
                names.append(name)
    return tuple(names)


def _place(value: Any) -> tuple[str | None, str | None]:
    """Venue name and street address, when the listing gives one.

    Most entries name the room and stop there; the address book fills the rest
    for venues we already know.
    """
    if not isinstance(value, dict):
        return None, None
    venue = value.get("name") if isinstance(value.get("name"), str) else None
    address = value.get("address")
    if isinstance(address, dict):
        parts = [
            address.get(key)
            for key in ("streetAddress", "addressLocality", "addressRegion", "postalCode")
            if isinstance(address.get(key), str)
        ]
        location = ", ".join(part for part in parts if part) or None
    elif isinstance(address, str):
        location = address
    else:
        location = None
    return venue, location


class ArcaneCitySource:
    """Discover events from arcane.city's public listing.

    Unlike the venue feeds, this is a whole-city calendar covering theatre and
    comedy alongside music, so it carries no curation of its own and must clear
    the affinity gate like Ticketmaster does.
    """

    name = "arcane-city"
    requires_affinity = True

    def __init__(
        self,
        base_url: str = BASE_URL,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._timeout = timeout
        self.last_diagnostics: dict[str, Any] = {}

    async def discover(self, window: DiscoveryWindow) -> list[DiscoveredEvent]:
        owned = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            headers={"User-Agent": _BROWSER_UA},
        )
        try:
            return await self._discover(client, window)
        finally:
            if owned:
                await client.aclose()

    async def _discover(
        self, client: httpx.AsyncClient, window: DiscoveryWindow
    ) -> list[DiscoveredEvent]:
        listed: list[tuple[dict[str, Any], datetime]] = []
        pages_read = 0
        for page in range(1, _MAX_PAGES + 1):
            html = await self._get(client, f"{self.base_url}/events?page={page}")
            if html is None:
                break
            entries = _listed_events(html)
            if not entries:
                break
            pages_read += 1
            past_window = False
            for entry in entries:
                starts_at = _local_datetime(entry.get("startDate"), window.default_timezone)
                if starts_at is None:
                    continue
                if starts_at > window.ends_at:
                    past_window = True
                    continue
                if starts_at >= window.starts_at:
                    listed.append((entry, starts_at))
            if past_window:
                break

        semaphore = asyncio.Semaphore(_DETAIL_CONCURRENCY)

        async def build(entry: dict[str, Any], starts_at: datetime) -> DiscoveredEvent | None:
            async with semaphore:
                return await self._event(client, entry, starts_at, window)

        results = await asyncio.gather(
            *(build(entry, starts_at) for entry, starts_at in listed)
        )
        events = [event for event in results if event is not None]
        self.last_diagnostics = {
            "pages_read": pages_read,
            "listed_in_window": len(listed),
            "events": len(events),
            "with_artwork": sum(1 for event in events if event.image_url),
        }
        return events

    async def _event(
        self,
        client: httpx.AsyncClient,
        entry: dict[str, Any],
        starts_at: datetime,
        window: DiscoveryWindow,
    ) -> DiscoveredEvent | None:
        title = entry.get("name")
        url = entry.get("url")
        if not isinstance(title, str) or not isinstance(url, str) or not url:
            return None

        # The listing omits the lineup, the end time, and the flyer -- the whole
        # reason for coming here -- so the event's own page is always read.
        detail: dict[str, Any] = {}
        html = await self._get(client, url)
        if html is not None:
            detail = _detail_event(html) or {}

        venue, location = _place(detail.get("location") or entry.get("location"))
        ends_at = _local_datetime(detail.get("endDate"), window.default_timezone)
        if ends_at is not None and ends_at <= starts_at:
            ends_at = None
        artists = _performers(detail.get("performer"))
        description = detail.get("description") or entry.get("description")

        return DiscoveredEvent(
            source_name=self.name,
            source_event_id=url.rsplit("/", 1)[-1] or url,
            title=title.strip(),
            source_url=url,
            artist=artists[0] if artists else None,
            artists=artists,
            venue=venue.strip() if isinstance(venue, str) else None,
            location=location,
            starts_at=starts_at,
            ends_at=ends_at,
            timezone=str(window.default_timezone),
            description=description if isinstance(description, str) else None,
            image_url=_first_image(detail.get("image")),
            raw={"url": url},
        )

    async def _get(self, client: httpx.AsyncClient, url: str) -> str | None:
        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.debug("arcane.city request failed for %s: %s", url, exc)
            return None
        return response.text
