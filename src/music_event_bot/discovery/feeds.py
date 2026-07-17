from __future__ import annotations

import hashlib
import html
import logging
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import feedparser
import httpx
import recurring_ical_events
from dateutil.parser import parse as parse_datetime
from icalendar import Calendar

from music_event_bot.discovery.base import DiscoveryWindow
from music_event_bot.domain.models import DiscoveredEvent

logger = logging.getLogger(__name__)

# Several venue calendars (e.g. warhol.org) sit behind Cloudflare and reject
# anything that does not look like an ordinary browser — including UAs that
# merely contain the word "bot".
_FEED_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}


def _apply_venue_default(
    venue: str | None,
    location: str | None,
    configured_url: str,
    venue_defaults: dict[str, str],
) -> tuple[str | None, str | None]:
    """Fill missing venue/location from per-feed defaults.

    Single-venue calendars routinely omit LOCATION because the venue is
    implied by the feed itself.
    """
    if venue and location:
        return venue, location
    url_folded = configured_url.casefold()
    for fragment, default_location in venue_defaults.items():
        if fragment in url_folded:
            default_venue = default_location.split(",", 1)[0].strip()
            return venue or default_venue, location or default_location
    return venue, location


def _local_calendar_path(value: str) -> Path | None:
    """Return a filesystem path when the configured value is not a URL.

    Local .ics files let out-of-band curators (e.g. a scheduled email-parsing
    job) hand events to the bot through the ordinary trusted-feed path.
    Windows drive letters parse as single-letter URL schemes, so anything
    that is not explicitly http/https/webcal is treated as a path.
    """
    scheme = urlsplit(value).scheme.casefold()
    if scheme in ("http", "https", "webcal"):
        return None
    return Path(value)


class CalendarSource:
    name = "ics"

    def __init__(
        self,
        urls: tuple[str, ...],
        client: httpx.AsyncClient | None = None,
        venue_defaults: dict[str, str] | None = None,
    ) -> None:
        self.urls = urls
        self._client = client
        self.venue_defaults = venue_defaults or {}

    async def discover(self, window: DiscoveryWindow) -> list[DiscoveredEvent]:
        owned_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(20.0), headers=_FEED_HEADERS, follow_redirects=True
        )
        events: list[DiscoveredEvent] = []
        try:
            for configured_url in self.urls:
                # One broken calendar must not lose events already fetched
                # from the other configured feeds.
                try:
                    local_path = _local_calendar_path(configured_url)
                    if local_path is not None:
                        if not local_path.exists():
                            logger.warning(
                                "Calendar file %s does not exist yet; skipping", local_path
                            )
                            continue
                        content = local_path.read_bytes()
                    else:
                        url = _http_calendar_url(configured_url)
                        response = await client.get(url)
                        response.raise_for_status()
                        content = response.content
                    calendar = Calendar.from_ical(content)
                    components = recurring_ical_events.of(calendar).between(
                        window.starts_at, window.ends_at
                    )
                except Exception as exc:
                    logger.warning("Calendar feed %s failed: %s", configured_url, exc)
                    continue
                for component in components:
                    parsed = self._parse_component(component, configured_url, window)
                    if parsed:
                        events.append(parsed)
        finally:
            if owned_client:
                await client.aclose()
        return events

    def _parse_component(
        self, component: Any, source_url: str, window: DiscoveryWindow
    ) -> DiscoveredEvent | None:
        uid = str(component.get("UID", "")).strip()
        title = str(component.get("SUMMARY", "")).strip()
        if not uid or not title:
            return None

        starts_at, all_day = _ical_datetime(component.get("DTSTART"), window)
        ends_at, _ = _ical_datetime(component.get("DTEND"), window)
        if starts_at and ends_at is None:
            ends_at = starts_at + timedelta(minutes=window.default_event_duration_minutes)
        if ends_at and starts_at and ends_at <= starts_at:
            ends_at = starts_at + timedelta(minutes=window.default_event_duration_minutes)

        location = _clean_ical_text(component.get("LOCATION"))
        venue = location.split(",", 1)[0].strip() if location else None
        venue, location = _apply_venue_default(
            venue, location, source_url, self.venue_defaults
        )
        categories = component.get("CATEGORIES")
        genres: tuple[str, ...] = ()
        if categories:
            decoded = categories.cats if hasattr(categories, "cats") else [str(categories)]
            genres = tuple(str(value) for value in decoded)
        description = _clean_ical_text(component.get("DESCRIPTION"))
        if not genres and description:
            genres = _genres_from_description(description)

        incomplete: list[str] = []
        if all_day:
            incomplete.append("all-day calendar event needs a start time")
            starts_at = None
            ends_at = None
        if starts_at is None:
            incomplete.append("missing start time")
        if not venue:
            incomplete.append("missing venue")
        if not location:
            incomplete.append("missing location")

        recurrence_id = component.get("RECURRENCE-ID")
        occurrence = starts_at.isoformat() if starts_at else str(recurrence_id or "unknown")
        source_event_id = f"{uid}:{occurrence}"
        return DiscoveredEvent(
            source_name=self.name,
            source_event_id=source_event_id,
            title=title,
            venue=venue,
            location=location,
            starts_at=starts_at,
            ends_at=ends_at,
            timezone=str(starts_at.tzinfo) if starts_at else str(window.default_timezone),
            source_url=_clean_ical_text(component.get("URL")) or source_url,
            genres=genres,
            description=description,
            raw={"uid": uid, "source_calendar": source_url},
            incomplete_reasons=tuple(dict.fromkeys(incomplete)),
        )


_GENRES_LINE_RE = re.compile(r"(?im)^\s*genres?\s*:\s*(.+)$")


def _genres_from_description(description: str) -> tuple[str, ...]:
    """Genres from a "Genres: a, b" line in an event description.

    Google Calendar has no category field, so curation tasks that write
    events onto the calendar label genres in prose instead.
    """
    match = _GENRES_LINE_RE.search(description)
    if not match:
        return ()
    return tuple(part.strip() for part in match.group(1).split(",") if part.strip())


_HTML_BREAK_RE = re.compile(r"(?i)<\s*br\s*/?>|<\s*/(?:p|div|li|h[1-6]|tr)\s*>")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_EVENT_DATE_RE = re.compile(
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s*"
    rf"(?P<month>{'|'.join(_MONTHS)})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?,?\s*(?P<year>\d{{4}})"
)
# Flattened venue-newsletter text: "..., 2026Thunderbird Music Hall4053 Butler
# Street, Pittsburgh, PADoors @ 7pm..." — venue letters sit between the year
# and the street number.
_VENUE_ADDRESS_RE = re.compile(
    r"\d{4}\s*(?P<venue>[A-Za-z][A-Za-z&'.\- ]{2,60}?)\s*"
    r"(?P<address>\d{1,5}\s+[A-Za-z0-9.\- ]+"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Way|Drive|Dr|Lane|Ln)\.?,?\s*"
    r"[A-Za-z.\- ]+,\s*[A-Z]{2}(?:\s*\d{5})?)"
)
_SHOW_TIME_RE = re.compile(
    r"(?i)show\s*@\s*(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<half>[ap])\.?m?"
)
_DOORS_TIME_RE = re.compile(
    r"(?i)doors\s*@\s*(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<half>[ap])\.?m?"
)


def _clean_feed_html(value: str) -> str:
    """Turn feed HTML into readable text: entities decoded, breaks preserved."""
    text = _HTML_BREAK_RE.sub("\n", value)
    text = _HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text).replace("\xa0", " ")
    # Feeds that flatten everything onto one line still get breaks at the
    # obvious section boundaries.
    for token in ("Doors @", "Show @", "AGE RESTRICTION", "Ticket Tier Info", "https://", "http://"):
        text = text.replace(token, f"\n{token}")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    collapsed: list[str] = []
    for line in lines:
        if line or (collapsed and collapsed[-1]):
            collapsed.append(line)
    return "\n".join(collapsed).strip()


def _extract_start_from_text(text: str, window: DiscoveryWindow) -> datetime | None:
    date_match = _EVENT_DATE_RE.search(text)
    if not date_match:
        return None
    time_match = _SHOW_TIME_RE.search(text) or _DOORS_TIME_RE.search(text)
    if not time_match:
        return None
    hour = int(time_match["hour"]) % 12
    if time_match["half"].lower() == "p":
        hour += 12
    try:
        return datetime(
            int(date_match["year"]),
            _MONTHS.index(date_match["month"]) + 1,
            int(date_match["day"]),
            hour,
            int(time_match["minute"] or 0),
            tzinfo=window.default_timezone,
        )
    except ValueError:
        return None


def _extract_venue_location_from_text(text: str) -> tuple[str | None, str | None]:
    match = _VENUE_ADDRESS_RE.search(text)
    if not match:
        return None, None
    venue = match["venue"].strip(" -&'.")
    address = " ".join(match["address"].split())
    return venue or None, address or None


class FeedSource:
    name = "rss"

    def __init__(
        self,
        urls: tuple[str, ...],
        client: httpx.AsyncClient | None = None,
        venue_defaults: dict[str, str] | None = None,
    ) -> None:
        self.urls = urls
        self._client = client
        self.venue_defaults = venue_defaults or {}

    async def discover(self, window: DiscoveryWindow) -> list[DiscoveredEvent]:
        owned_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(20.0), headers=_FEED_HEADERS, follow_redirects=True
        )
        events: list[DiscoveredEvent] = []
        try:
            for feed_url in self.urls:
                try:
                    response = await client.get(feed_url)
                    response.raise_for_status()
                    parsed = feedparser.parse(response.content)
                except Exception as exc:
                    logger.warning("RSS feed %s failed: %s", feed_url, exc)
                    continue
                for entry in parsed.entries:
                    event = self._parse_entry(entry, feed_url, window)
                    if event:
                        events.append(event)
        finally:
            if owned_client:
                await client.aclose()
        return events

    def _parse_entry(
        self, entry: Any, feed_url: str, window: DiscoveryWindow
    ) -> DiscoveredEvent | None:
        title = str(entry.get("title", "")).strip()
        if not title:
            return None
        link = entry.get("link")
        source_id = entry.get("id") or entry.get("guid") or link
        if not source_id:
            source_id = hashlib.sha256(f"{feed_url}|{title}".encode()).hexdigest()

        starts_at = _entry_datetime(entry, window)
        ends_at = _entry_datetime(entry, window, end=True)
        if starts_at and ends_at is None:
            ends_at = starts_at + timedelta(minutes=window.default_event_duration_minutes)
        venue = _entry_value(entry, "venue", "event_venue", "ev_venue")
        location = _entry_value(entry, "location", "event_location", "ev_location")
        if not location:
            location = venue
        genres_raw = _entry_value(entry, "genre", "genres", "category")
        genres = tuple(
            part.strip() for part in str(genres_raw or "").split(",") if part.strip()
        )
        if not genres:
            # feedparser normalizes <category> elements into entry.tags.
            tags = entry.get("tags") or []
            genres = tuple(
                str(tag.get("term")).strip()
                for tag in tags
                if isinstance(tag, dict) and str(tag.get("term") or "").strip()
            )

        raw_summary = entry.get("summary") or entry.get("description")
        description = _clean_feed_html(str(raw_summary)) if raw_summary else None

        # Venue newsletters (e.g. Thunderbird's WordPress feed) put the date,
        # venue, address, and showtime in prose rather than structured fields.
        # Recover them from the cleaned text before declaring the entry
        # incomplete.
        if description:
            if starts_at is None:
                starts_at = _extract_start_from_text(description, window)
                if starts_at and ends_at is None:
                    ends_at = starts_at + timedelta(
                        minutes=window.default_event_duration_minutes
                    )
            if not venue or not location:
                extracted_venue, extracted_address = _extract_venue_location_from_text(
                    description
                )
                venue = venue or extracted_venue
                location = location or extracted_address or venue

        venue, location = _apply_venue_default(
            venue, location, feed_url, self.venue_defaults
        )

        # Venue feeds keep past shows listed; only ingest what falls in the
        # discovery window. Undated entries still come through for editing.
        if starts_at is not None and not window.starts_at <= starts_at <= window.ends_at:
            return None

        incomplete: list[str] = []
        if starts_at is None:
            incomplete.append("feed entry has no structured event start time")
        if not venue:
            incomplete.append("feed entry has no structured venue")
        if not location:
            incomplete.append("feed entry has no structured location")

        return DiscoveredEvent(
            source_name=self.name,
            source_event_id=str(source_id),
            title=title,
            venue=str(venue) if venue else None,
            location=str(location) if location else None,
            starts_at=starts_at,
            ends_at=ends_at,
            timezone=str(starts_at.tzinfo) if starts_at else str(window.default_timezone),
            source_url=str(link) if link else feed_url,
            genres=genres,
            description=description,
            raw={"feed_url": feed_url, "entry": dict(entry)},
            incomplete_reasons=tuple(incomplete),
        )


def _http_calendar_url(url: str) -> str:
    return "https://" + url[len("webcal://") :] if url.startswith("webcal://") else url


def _clean_ical_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\\n", "\n").strip()
    return text or None


def _ical_datetime(value: Any, window: DiscoveryWindow) -> tuple[datetime | None, bool]:
    if value is None:
        return None, False
    decoded = value.dt if hasattr(value, "dt") else value
    if isinstance(decoded, datetime):
        if decoded.tzinfo is None:
            decoded = decoded.replace(tzinfo=window.default_timezone)
        return decoded, False
    if isinstance(decoded, date):
        return datetime.combine(decoded, time.min, tzinfo=window.default_timezone), True
    return None, False


def _entry_value(entry: Any, *names: str) -> Any:
    for name in names:
        value = entry.get(name)
        if value not in (None, ""):
            return value
    return None


def _entry_datetime(
    entry: Any, window: DiscoveryWindow, *, end: bool = False
) -> datetime | None:
    names = (
        ("event_end", "dtend", "end_time", "ev_enddate")
        if end
        else ("event_start", "dtstart", "start_time", "ev_startdate")
    )
    value = _entry_value(entry, *names)
    if value is None:
        return None
    try:
        parsed = parse_datetime(str(value))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=window.default_timezone)
    return parsed
