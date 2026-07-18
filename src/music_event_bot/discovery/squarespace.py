from __future__ import annotations

import html
import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from music_event_bot.discovery.base import DiscoveryWindow
from music_event_bot.discovery.feeds import _FEED_HEADERS
from music_event_bot.discovery.images import is_publishable_image
from music_event_bot.domain.models import DiscoveredEvent

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


def _json_url(configured_url: str) -> str:
    parts = urlsplit(configured_url)
    query = parts.query
    if "format=json" not in query:
        query = f"{query}&format=json" if query else "format=json"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def _strip_html(value: str | None) -> str | None:
    if not value:
        return None
    text = html.unescape(_TAG_RE.sub(" ", value))
    text = " ".join(text.split())
    return text or None


def _epoch_ms(value: Any) -> datetime | None:
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=UTC)


class SquarespaceSource:
    """Discover events from Squarespace event collections.

    Squarespace sites expose collections as JSON via ``?format=json`` even
    when they offer no ICS/RSS feed. Like the calendar/RSS sources, these are
    hand-curated venue feeds, so they are not subject to the affinity gate.
    """

    name = "squarespace"

    def __init__(self, urls: tuple[str, ...], client: httpx.AsyncClient | None = None) -> None:
        self.urls = urls
        self._client = client

    async def discover(self, window: DiscoveryWindow) -> list[DiscoveredEvent]:
        owned_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(20.0), headers=_FEED_HEADERS, follow_redirects=True
        )
        events: list[DiscoveredEvent] = []
        try:
            for configured_url in self.urls:
                try:
                    response = await client.get(_json_url(configured_url))
                    response.raise_for_status()
                    payload = response.json()
                except Exception as exc:
                    logger.warning("Squarespace feed %s failed: %s", configured_url, exc)
                    continue
                if not isinstance(payload, dict):
                    logger.warning(
                        "Squarespace feed %s returned non-JSON content", configured_url
                    )
                    continue
                events.extend(self._parse_collection(payload, configured_url, window))
        finally:
            if owned_client:
                await client.aclose()
        return events

    def _parse_collection(
        self, payload: dict[str, Any], configured_url: str, window: DiscoveryWindow
    ) -> list[DiscoveredEvent]:
        website = payload.get("website", {})
        site_title = (
            str(website.get("siteTitle", "")).strip()
            if isinstance(website, dict)
            else ""
        )
        base = urlsplit(configured_url)
        events: list[DiscoveredEvent] = []
        # Squarespace event collections return "upcoming"/"past" arrays;
        # other collection types return a flat "items" array.
        items = payload.get("items")
        if not isinstance(items, list):
            items = payload.get("upcoming", [])
        if not isinstance(items, list):
            return events
        for item in items:
            if not isinstance(item, dict):
                continue
            parsed = self._parse_item(item, site_title, base, configured_url, window)
            if parsed is not None:
                events.append(parsed)
        return events

    def _parse_item(
        self,
        item: dict[str, Any],
        site_title: str,
        base: Any,
        configured_url: str,
        window: DiscoveryWindow,
    ) -> DiscoveredEvent | None:
        item_id = str(item.get("id", "")).strip()
        title = html.unescape(str(item.get("title", ""))).strip()
        if not item_id or not title:
            return None
        starts_at = _epoch_ms(item.get("startDate"))
        if starts_at is not None and not window.starts_at <= starts_at <= window.ends_at:
            return None
        ends_at = _epoch_ms(item.get("endDate"))
        if ends_at is not None and starts_at is not None and ends_at <= starts_at:
            ends_at = None

        location_data = item.get("location", {})
        address_parts: list[str] = []
        venue = site_title or None
        if isinstance(location_data, dict):
            address_title = str(location_data.get("addressTitle", "")).strip()
            if address_title:
                venue = address_title
            for key in ("addressLine1", "addressLine2"):
                value = str(location_data.get(key, "")).strip()
                if value:
                    address_parts.append(value)
        location = ", ".join(address_parts) if address_parts else venue

        full_url = str(item.get("fullUrl", "")).strip()
        url = (
            urlunsplit((base.scheme, base.netloc, full_url, "", ""))
            if full_url.startswith("/")
            else full_url or configured_url
        )

        incomplete: list[str] = []
        if starts_at is None:
            incomplete.append("missing start time")
        if not venue:
            incomplete.append("missing venue")
        if not location:
            incomplete.append("missing location")

        asset_url = str(item.get("assetUrl") or "") or None
        categories = item.get("categories", [])
        tags = item.get("tags", [])
        genres = tuple(
            str(value).strip()
            for value in [*categories, *tags]
            if isinstance(value, str) and str(value).strip()
        )

        return DiscoveredEvent(
            source_name=self.name,
            source_event_id=item_id,
            title=title,
            venue=venue,
            location=location,
            starts_at=starts_at,
            ends_at=ends_at,
            timezone=str(window.default_timezone),
            source_url=url,
            genres=genres,
            description=_strip_html(item.get("excerpt")),
            image_url=asset_url if is_publishable_image(asset_url) else None,
            raw={"squarespace_url": configured_url, "item_id": item_id},
            incomplete_reasons=tuple(incomplete),
        )
