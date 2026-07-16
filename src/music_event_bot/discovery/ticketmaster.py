from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from dateutil.parser import isoparse

from music_event_bot.config import Settings
from music_event_bot.discovery.base import DiscoveryWindow
from music_event_bot.domain.geography import (
    CoverageCell,
    GeoPoint,
    geohash_encode,
    haversine_miles,
)
from music_event_bot.domain.models import DiscoveredEvent


class TicketmasterSource:
    name = "ticketmaster"
    requires_affinity = True
    base_url = "https://app.ticketmaster.com/discovery/v2/events.json"

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._client = client
        self._last_request_at = 0.0
        self.last_diagnostics: dict[str, Any] = {}

    async def discover(self, window: DiscoveryWindow) -> list[DiscoveredEvent]:
        if not self.settings.ticketmaster_configured:
            return []

        owned_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(20.0))
        events_by_id: dict[str, DiscoveredEvent] = {}
        pages_fetched = 0
        duplicates_removed = 0
        successful_cells = 0
        cell_errors: dict[str, str] = {}
        cells = self.settings.ticketmaster_cells

        try:
            for cell in cells:
                try:
                    for page in range(self.settings.ticketmaster_max_pages):
                        response = await self._request(client, self._params(window, page, cell))
                        pages_fetched += 1
                        payload = response.json()
                        if not isinstance(payload, dict):
                            raise ValueError("Ticketmaster returned a non-object response")
                        embedded = payload.get("_embedded", {})
                        raw_events = (
                            embedded.get("events", [])
                            if isinstance(embedded, dict)
                            else []
                        )
                        for raw_event in raw_events:
                            if not isinstance(raw_event, dict):
                                continue
                            parsed = self._parse_event(raw_event, window, cell)
                            if parsed is None:
                                continue
                            existing = events_by_id.get(parsed.source_event_id)
                            if existing is None:
                                events_by_id[parsed.source_event_id] = parsed
                            else:
                                duplicates_removed += 1
                                raw = dict(existing.raw)
                                zones = list(raw.get("_query_cells", []))
                                if cell.name not in zones:
                                    zones.append(cell.name)
                                raw["_query_cells"] = zones
                                events_by_id[parsed.source_event_id] = replace(existing, raw=raw)

                        page_data = payload.get("page", {})
                        total_pages = int(page_data.get("totalPages", 1))
                        if page + 1 >= total_pages:
                            break
                    successful_cells += 1
                except Exception as exc:
                    cell_errors[cell.name] = self._diagnostic_error(exc)
        finally:
            if owned_client:
                await client.aclose()

        self.last_diagnostics = {
            "cells_configured": len(cells),
            "cells_succeeded": successful_cells,
            "pages_fetched": pages_fetched,
            "duplicates_removed": duplicates_removed,
            "cell_errors": cell_errors,
        }
        if cells and successful_cells == 0 and cell_errors:
            raise RuntimeError(f"All Ticketmaster coverage cells failed: {cell_errors}")
        return list(events_by_id.values())

    def _params(
        self, window: DiscoveryWindow, page: int, cell: CoverageCell
    ) -> dict[str, str | int]:
        api_key = self.settings.ticketmaster_api_key
        if api_key is None:
            raise RuntimeError("Ticketmaster is not configured")
        params: dict[str, str | int] = {
            "apikey": api_key.get_secret_value(),
            "classificationName": "music",
            "geoPoint": geohash_encode(cell.center),
            "radius": cell.radius_miles,
            "unit": "miles",
            "startDateTime": window.starts_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endDateTime": window.ends_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "size": 200,
            "page": page,
            "sort": "date,asc",
        }
        if cell.name == "legacy-home":
            params["countryCode"] = self.settings.ticketmaster_country_code
        return params

    async def _request(
        self, client: httpx.AsyncClient, params: dict[str, str | int]
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                await self._respect_rate_limit()
                response = await client.get(self.base_url, params=params)
                response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                last_error = exc
                retryable = not isinstance(exc, httpx.HTTPStatusError) or (
                    exc.response.status_code == 429 or exc.response.status_code >= 500
                )
                if not retryable or attempt == 2:
                    raise
                await asyncio.sleep(0.5 * (2**attempt))
        raise RuntimeError("Ticketmaster request failed") from last_error

    @staticmethod
    def _diagnostic_error(exc: Exception) -> str:
        if isinstance(exc, httpx.HTTPStatusError):
            return f"Ticketmaster returned HTTP {exc.response.status_code}"
        if isinstance(exc, httpx.RequestError):
            return f"Ticketmaster request failed ({type(exc).__name__})"
        return f"{type(exc).__name__}: {exc}"

    async def _respect_rate_limit(self) -> None:
        loop = asyncio.get_running_loop()
        elapsed = loop.time() - self._last_request_at
        if elapsed < 0.55:
            await asyncio.sleep(0.55 - elapsed)
        self._last_request_at = loop.time()

    def _parse_event(
        self,
        raw: dict[str, Any],
        window: DiscoveryWindow,
        cell: CoverageCell,
    ) -> DiscoveredEvent | None:
        event_id = str(raw.get("id", "")).strip()
        title = str(raw.get("name", "")).strip()
        if not event_id or not title:
            return None

        status_code = raw.get("dates", {}).get("status", {}).get("code")
        if status_code == "cancelled":
            return None

        embedded = raw.get("_embedded", {})
        venues = embedded.get("venues", []) if isinstance(embedded, dict) else []
        venue_data = venues[0] if venues and isinstance(venues[0], dict) else {}
        venue = venue_data.get("name")
        coordinates = self._parse_coordinates(venue_data.get("location"))
        if coordinates is not None:
            distance = haversine_miles(self.settings.home_point, coordinates)
            if distance > self.settings.max_travel_radius_miles:
                return None

        timezone_name = venue_data.get("timezone") or self.settings.default_timezone
        try:
            event_zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            event_zone = window.default_timezone
            timezone_name = str(event_zone)

        starts_at = self._parse_start(raw.get("dates", {}).get("start", {}), event_zone)
        if starts_at and not window.starts_at <= starts_at <= window.ends_at:
            return None
        ends_at = self._parse_start(raw.get("dates", {}).get("end", {}), event_zone)
        if ends_at and starts_at and ends_at <= starts_at:
            ends_at = None

        attractions = embedded.get("attractions", []) if isinstance(embedded, dict) else []
        artist_names = tuple(
            str(attraction["name"]).strip()
            for attraction in attractions
            if isinstance(attraction, dict) and attraction.get("name")
        )
        artist = artist_names[0] if artist_names else None
        classifications = raw.get("classifications", [])
        genres: set[str] = set()
        for classification in classifications:
            if not isinstance(classification, dict):
                continue
            for key in ("genre", "subGenre"):
                group = classification.get(key, {})
                name = group.get("name") if isinstance(group, dict) else None
                if name and name.casefold() != "undefined":
                    genres.add(name)

        address_parts = [
            venue_data.get("address", {}).get("line1"),
            venue_data.get("city", {}).get("name"),
            venue_data.get("state", {}).get("stateCode")
            or venue_data.get("state", {}).get("name"),
            venue_data.get("postalCode"),
            venue_data.get("country", {}).get("countryCode"),
        ]
        location = ", ".join(str(part) for part in address_parts if part)

        images = raw.get("images", [])
        images_16_9 = [image for image in images if image.get("ratio") == "16_9"] or images
        image_url = None
        if images_16_9:
            image_url = max(images_16_9, key=lambda image: int(image.get("width", 0))).get("url")

        price = None
        ranges = raw.get("priceRanges", [])
        if ranges:
            selected = ranges[0]
            minimum = selected.get("min")
            maximum = selected.get("max")
            currency = selected.get("currency", "")
            if minimum is not None and maximum is not None:
                price = f"{minimum:g}–{maximum:g} {currency}".strip()

        # info and pleaseNote frequently carry identical text; keep one copy.
        description_parts: list[str] = []
        for part in (raw.get("info"), raw.get("pleaseNote")):
            text = str(part).strip() if part else ""
            if text and text not in description_parts:
                description_parts.append(text)
        if price:
            description_parts.append(f"Listed price range: {price}")
        description = "\n\n".join(description_parts) or None

        incomplete: list[str] = []
        if starts_at is None:
            incomplete.append("missing start time")
        if not venue:
            incomplete.append("missing venue")
        if not location:
            incomplete.append("missing location")

        retained_raw = dict(raw)
        retained_raw["_query_cells"] = [cell.name]
        return DiscoveredEvent(
            source_name=self.name,
            source_event_id=event_id,
            title=title,
            artist=artist,
            artists=artist_names,
            venue=venue,
            location=location or None,
            starts_at=starts_at,
            ends_at=ends_at,
            timezone=timezone_name,
            source_url=raw.get("url"),
            genres=tuple(sorted(genres)),
            description=description,
            image_url=image_url,
            venue_latitude=coordinates.latitude if coordinates else None,
            venue_longitude=coordinates.longitude if coordinates else None,
            raw=retained_raw,
            incomplete_reasons=tuple(incomplete),
        )

    @staticmethod
    def _parse_coordinates(data: Any) -> GeoPoint | None:
        if not isinstance(data, dict):
            return None
        latitude = data.get("latitude")
        longitude = data.get("longitude")
        if latitude is None or longitude is None:
            return None
        try:
            return GeoPoint(float(latitude), float(longitude))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_start(data: dict[str, Any], timezone: ZoneInfo) -> datetime | None:
        if data.get("dateTime"):
            return isoparse(data["dateTime"]).astimezone(timezone)
        local_date = data.get("localDate")
        local_time = data.get("localTime")
        if not local_date or not local_time:
            return None
        return datetime.fromisoformat(f"{local_date}T{local_time}").replace(tzinfo=timezone)
