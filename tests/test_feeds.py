from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from music_event_bot.discovery.feeds import CalendarSource, FeedSource


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
