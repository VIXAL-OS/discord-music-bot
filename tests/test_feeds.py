from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from music_event_bot.discovery.feeds import CalendarSource, FeedSource

_THUNDERBIRD_SUMMARY = (
    "Angela Autumn &#160; Monday, July 20, 2026Thunderbird Music Hall"
    "4053 Butler Street, Pittsburgh, PADoors @ 7:00 PMShow @ 8:00 PM"
    "AGE RESTRICTION: 18+ or with legal guardian "
    "Angela Autumn is a country folk artist releasing &#8220;Cowboy Jack Clementine&#8221; (2023)."
)


@pytest.mark.asyncio
async def test_venue_newsletter_entries_are_cleaned_and_completed(discovery_window) -> None:
    from music_event_bot.discovery.feeds import FeedSource

    entry = {
        "title": "Angela Autumn",
        "id": "https://thunderbirdmusichall.com/?p=1",
        "link": "https://thunderbirdmusichall.com/event/angela-autumn/",
        "summary": _THUNDERBIRD_SUMMARY,
        "tags": [{"term": "Country"}, {"term": "Folk"}],
    }
    feed_url = "https://thunderbirdmusichall.com/shows/feed/"
    source = FeedSource((feed_url,))
    event = source._parse_entry(entry, feed_url, discovery_window)

    assert event is not None
    assert event.venue == "Thunderbird Music Hall"
    assert event.location == "4053 Butler Street, Pittsburgh, PA"
    assert event.starts_at is not None
    assert (event.starts_at.year, event.starts_at.month, event.starts_at.day) == (2026, 7, 20)
    assert (event.starts_at.hour, event.starts_at.minute) == (20, 0)
    assert event.incomplete_reasons == ()
    # Entities decoded, section breaks restored, nothing left HTML-escaped.
    assert event.description is not None
    assert "&#160;" not in event.description
    assert "“Cowboy Jack Clementine”" in event.description
    assert "\nDoors @ 7:00 PM" in event.description
    assert event.genres == ("Country", "Folk")


@pytest.mark.asyncio
async def test_prose_extraction_leaves_truly_incomplete_entries_alone(
    discovery_window,
) -> None:
    from music_event_bot.discovery.feeds import FeedSource

    entry = {
        "title": "Mystery Announcement",
        "id": "https://venue.example/?p=2",
        "summary": "Something wicked this way comes. Stay tuned.",
    }
    source = FeedSource(("https://venue.example/feed/",))
    event = source._parse_entry(entry, "https://venue.example/feed/", discovery_window)
    assert event is not None
    assert event.starts_at is None
    assert event.venue is None
    assert "feed entry has no structured event start time" in event.incomplete_reasons


@pytest.mark.asyncio
async def test_single_venue_calendar_gets_default_location(
    discovery_window, tmp_path: Path
) -> None:
    no_location = _ALL_DAY_ICS.replace(b"LOCATION:Festival Grounds, Brooklyn, NY\n", b"")
    local = tmp_path / "poetry.ics"
    local.write_bytes(no_location)
    source = CalendarSource(
        (str(local),),
        venue_defaults={"poetry.ics": "Poetry Lounge, 313 North Avenue, Millvale, PA"},
    )
    events = await source.discover(discovery_window)
    assert len(events) == 1
    assert events[0].venue == "Poetry Lounge"
    assert events[0].location == "Poetry Lounge, 313 North Avenue, Millvale, PA"
    # Explicit locations always win over the default.
    with_location = CalendarSource(
        (str(tmp_path / "other.ics"),),
        venue_defaults={"other.ics": "Poetry Lounge, 313 North Avenue, Millvale, PA"},
    )
    (tmp_path / "other.ics").write_bytes(_ALL_DAY_ICS)
    kept = await with_location.discover(discovery_window)
    assert kept[0].venue == "Festival Grounds"


@pytest.mark.asyncio
async def test_past_feed_entries_are_skipped(discovery_window) -> None:
    from music_event_bot.discovery.feeds import FeedSource

    entry = {
        "title": "Angela Autumn",
        "id": "https://thunderbirdmusichall.com/?p=3",
        "summary": _THUNDERBIRD_SUMMARY.replace("Monday, July 20, 2026", "Tuesday, June 16, 2026"),
    }
    source = FeedSource(("https://thunderbirdmusichall.com/shows/feed/",))
    assert source._parse_entry(entry, "https://x/feed/", discovery_window) is None


@pytest.mark.asyncio
async def test_calendar_source_reads_local_files(discovery_window, tmp_path: Path) -> None:
    local = tmp_path / "email-events.ics"
    local.write_bytes(_ALL_DAY_ICS)
    events = await CalendarSource((str(local),)).discover(discovery_window)
    assert len(events) == 1
    assert events[0].source_event_id.startswith("all-day-1:")

    # A configured file that does not exist yet is skipped, not fatal.
    missing = CalendarSource((str(tmp_path / "not-written-yet.ics"),))
    assert await missing.discover(discovery_window) == []


@pytest.mark.asyncio
async def test_one_failing_calendar_does_not_lose_the_others(
    discovery_window, tmp_path: Path
) -> None:
    good = tmp_path / "good.ics"
    good.write_bytes(_ALL_DAY_ICS)
    with respx.mock() as router:
        router.get("https://blocked.example.test/events.ics").mock(
            return_value=httpx.Response(403)
        )
        async with httpx.AsyncClient() as client:
            events = await CalendarSource(
                ("https://blocked.example.test/events.ics", str(good)), client
            ).discover(discovery_window)
    assert len(events) == 1
    assert events[0].source_event_id.startswith("all-day-1:")

_ALL_DAY_ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Example//Music Events//EN
BEGIN:VEVENT
UID:all-day-1
SUMMARY:All Day Music Festival
DTSTART;VALUE=DATE:20260712
LOCATION:Festival Grounds, Brooklyn, NY
CATEGORIES:Rock,Indie
DESCRIPTION:Outdoor sets all day.
END:VEVENT
END:VCALENDAR
"""

_INCOMPLETE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example Music News</title>
    <item>
      <title>Unstructured concert notice</title>
      <guid>rss-1</guid>
      <link>https://news.example.test/notices/concert</link>
      <description>Details will be announced later.</description>
    </item>
  </channel>
</rss>
"""


@pytest.mark.asyncio
async def test_calendar_all_day_event_is_retained_as_incomplete(discovery_window) -> None:
    configured_url = "webcal://calendar.example.test/events.ics"
    with respx.mock(assert_all_called=True) as router:
        route = router.get("https://calendar.example.test/events.ics").mock(
            return_value=httpx.Response(200, content=_ALL_DAY_ICS)
        )
        async with httpx.AsyncClient() as client:
            events = await CalendarSource((configured_url,), client).discover(discovery_window)

    assert route.called
    assert len(events) == 1
    event = events[0]
    assert event.source_event_id.startswith("all-day-1:")
    assert event.venue == "Festival Grounds"
    assert event.location == "Festival Grounds, Brooklyn, NY"
    assert event.starts_at is None
    assert event.ends_at is None
    assert event.incomplete_reasons == (
        "all-day calendar event needs a start time",
        "missing start time",
    )


@pytest.mark.asyncio
async def test_rss_unstructured_entry_is_retained_as_incomplete(discovery_window) -> None:
    feed_url = "https://feeds.example.test/music.xml"
    with respx.mock(assert_all_called=True) as router:
        route = router.get(feed_url).mock(return_value=httpx.Response(200, content=_INCOMPLETE_RSS))
        async with httpx.AsyncClient() as client:
            events = await FeedSource((feed_url,), client).discover(discovery_window)

    assert route.called
    assert len(events) == 1
    event = events[0]
    assert event.source_event_id == "rss-1"
    assert event.source_url == "https://news.example.test/notices/concert"
    assert event.starts_at is None
    assert event.ends_at is None
    assert event.venue is None
    assert event.location is None
    assert event.incomplete_reasons == (
        "feed entry has no structured event start time",
        "feed entry has no structured venue",
        "feed entry has no structured location",
    )


_GENRE_LINE_ICS = (
    b"BEGIN:VCALENDAR\r\n"
    b"VERSION:2.0\r\n"
    b"PRODID:-//test//EN\r\n"
    b"BEGIN:VEVENT\r\n"
    b"UID:diy-1\r\n"
    b"DTSTART:20260715T230000Z\r\n"
    b"DTEND:20260716T020000Z\r\n"
    b"SUMMARY:Trash Palace + Gridfailure\r\n"
    b"LOCATION:Mr. Roboto Project\x5c, 5106 Penn Ave\x5c, Pittsburgh\x5c, PA\r\n"
    b"DESCRIPTION:All ages DIY show.\x5cnGenres: grindcore\x5c, powerviolence"
    b"\x5cnDoors 7 PM.\r\n"
    b"END:VEVENT\r\n"
    b"END:VCALENDAR\r\n"
)


@pytest.mark.asyncio
async def test_calendar_description_genre_line(discovery_window, tmp_path: Path) -> None:
    """Curation tasks label genres in prose; the parser lifts them out."""
    local = tmp_path / "diy.ics"
    local.write_bytes(_GENRE_LINE_ICS)
    events = await CalendarSource((str(local),)).discover(discovery_window)
    assert len(events) == 1
    event = events[0]
    assert event.genres == ("grindcore", "powerviolence")
    assert event.venue == "Mr. Roboto Project"
    assert "DIY show" in (event.description or "")
