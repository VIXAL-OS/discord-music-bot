from __future__ import annotations

from unittest.mock import PropertyMock, patch

import httpx
import pytest
import respx

from music_event_bot.config import Settings
from music_event_bot.discovery.ticketmaster import TicketmasterSource
from music_event_bot.domain.geography import CoverageCell, GeoPoint, geohash_encode


def _ticketmaster_event(event_id: str, name: str, **overrides):
    event = {
        "id": event_id,
        "name": name,
        "url": "https://tickets.example.test/events/123",
        "dates": {
            "status": {"code": "onsale"},
            "start": {"dateTime": "2026-07-12T00:00:00Z"},
            "end": {"dateTime": "2026-07-12T03:00:00Z"},
        },
        "_embedded": {
            "venues": [
                {
                    "name": "Example Arena",
                    "timezone": "America/New_York",
                    "address": {"line1": "1 Music Way"},
                    "city": {"name": "Brooklyn"},
                    "state": {"stateCode": "NY"},
                    "postalCode": "11201",
                    "country": {"countryCode": "US"},
                    "location": {"latitude": "40.7", "longitude": "-74.0"},
                }
            ],
            "attractions": [{"name": "The Headliners"}, {"name": "The Openers"}],
        },
        "classifications": [
            {"genre": {"name": "Rock"}, "subGenre": {"name": "Indie Rock"}}
        ],
        "images": [
            {"ratio": "4_3", "width": 1600, "url": "https://images.example.test/4x3.jpg"},
            {"ratio": "16_9", "width": 640, "url": "https://images.example.test/small.jpg"},
            {"ratio": "16_9", "width": 1920, "url": "https://images.example.test/large.jpg"},
        ],
        "priceRanges": [{"min": 25, "max": 75, "currency": "USD"}],
        "info": "Doors at 7 PM",
        "pleaseNote": "All ages",
    }
    event.update(overrides)
    return event


def test_duplicate_info_and_please_note_kept_once(tmp_path, discovery_window) -> None:
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "events.sqlite3",
        ticketmaster_api_key="test-api-key",
        discovery_latitude=40.7,
        discovery_longitude=-74.0,
    )
    source = TicketmasterSource(settings)
    raw = _ticketmaster_event(
        "dup-1",
        "Dup Show",
        info="Doors 7 PM. Rain or shine.",
        pleaseNote="Doors 7 PM. Rain or shine.",
    )
    parsed = source._parse_event(raw, discovery_window, settings.ticketmaster_cells[0])
    assert parsed is not None
    assert parsed.description == "Doors 7 PM. Rain or shine.\n\nListed price range: 25–75 USD"


@pytest.mark.asyncio
async def test_ticketmaster_parses_events_and_follows_pages(tmp_path, discovery_window) -> None:
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "events.sqlite3",
        ticketmaster_api_key="test-api-key",
        discovery_latitude=40.7,
        discovery_longitude=-74.0,
        ticketmaster_max_pages=2,
    )
    first_page = {
        "_embedded": {
            "events": [
                _ticketmaster_event("valid-1", "The Headliners"),
                _ticketmaster_event(
                    "cancelled-1",
                    "Cancelled Show",
                    dates={"status": {"code": "cancelled"}},
                ),
            ]
        },
        "page": {"totalPages": 2},
    }
    second_page = {
        "_embedded": {
            "events": [
                _ticketmaster_event(
                    "malformed-coordinates",
                    "Coordinates TBA Show",
                    _embedded={
                        "venues": [
                            {
                                "name": "Unmapped Hall",
                                "timezone": "America/New_York",
                                "city": {"name": "Pittsburgh"},
                                "location": "not-a-coordinate-object",
                            }
                        ],
                        "attractions": [{"name": "The Headliners"}],
                    },
                ),
                _ticketmaster_event(
                    "incomplete-1",
                    "Date TBA Show",
                    dates={"status": {"code": "onsale"}, "start": {}},
                ),
            ]
        },
        "page": {"totalPages": 2},
    }

    with respx.mock(assert_all_called=True) as router:
        route = router.get(TicketmasterSource.base_url).mock(
            side_effect=[
                httpx.Response(200, json=first_page),
                httpx.Response(200, json=second_page),
            ]
        )
        async with httpx.AsyncClient() as client:
            events = await TicketmasterSource(settings, client).discover(discovery_window)

    assert route.call_count == 2
    assert [call.request.url.params["page"] for call in route.calls] == ["0", "1"]
    assert route.calls[0].request.url.params["classificationName"] == "music"
    assert route.calls[0].request.url.params["geoPoint"] == geohash_encode(settings.home_point)
    assert route.calls[0].request.url.params["countryCode"] == "US"
    assert "latlong" not in route.calls[0].request.url.params

    assert [event.source_event_id for event in events] == [
        "valid-1",
        "malformed-coordinates",
        "incomplete-1",
    ]
    parsed = events[0]
    assert parsed.artist == "The Headliners"
    assert parsed.artists == ("The Headliners", "The Openers")
    assert parsed.venue == "Example Arena"
    assert parsed.location == "1 Music Way, Brooklyn, NY, 11201, US"
    assert parsed.venue_latitude == 40.7
    assert parsed.venue_longitude == -74.0
    assert parsed.starts_at is not None
    assert parsed.starts_at.isoformat() == "2026-07-11T20:00:00-04:00"
    assert parsed.ends_at is not None
    assert parsed.genres == ("Indie Rock", "Rock")
    assert parsed.image_url == "https://images.example.test/large.jpg"
    assert parsed.description == "Doors at 7 PM\n\nAll ages\n\nListed price range: 25–75 USD"

    malformed_coordinates = events[1]
    assert malformed_coordinates.venue_latitude is None
    assert malformed_coordinates.venue_longitude is None

    incomplete = events[2]
    assert incomplete.starts_at is None
    assert incomplete.incomplete_reasons == ("missing start time",)


@pytest.mark.asyncio
async def test_regional_cells_deduplicate_overlapping_results(
    tmp_path, discovery_window
) -> None:
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "events.sqlite3",
        ticketmaster_api_key="test-api-key",
        ticketmaster_max_pages=2,
    )
    cells = (
        CoverageCell("cell-a", GeoPoint(40.4406, -79.9959), 90),
        CoverageCell("cell-b", GeoPoint(41.0, -80.5), 90),
    )
    event = _ticketmaster_event(
        "duplicate-1",
        "Overlapping Show",
        _embedded={
            "venues": [
                {
                    "name": "Pittsburgh Hall",
                    "timezone": "America/New_York",
                    "address": {"line1": "1 Local Way"},
                    "city": {"name": "Pittsburgh"},
                    "state": {"stateCode": "PA"},
                    "country": {"countryCode": "US"},
                    "location": {"latitude": "40.44", "longitude": "-79.99"},
                }
            ],
            "attractions": [{"name": "Overlap Artist"}],
        },
    )
    first_page = {"_embedded": {"events": [event]}, "page": {"totalPages": 2}}
    empty_page = {"_embedded": {"events": []}, "page": {"totalPages": 2}}
    second_cell = {"_embedded": {"events": [event]}, "page": {"totalPages": 1}}

    with patch.object(Settings, "ticketmaster_cells", new_callable=PropertyMock) as cells_mock:
        cells_mock.return_value = cells
        with respx.mock(assert_all_called=True) as router:
            route = router.get(TicketmasterSource.base_url).mock(
                side_effect=[
                    httpx.Response(200, json=first_page),
                    httpx.Response(200, json=empty_page),
                    httpx.Response(200, json=second_cell),
                ]
            )
            async with httpx.AsyncClient() as client:
                source = TicketmasterSource(settings, client)
                events = await source.discover(discovery_window)

    assert route.call_count == 3
    assert [call.request.url.params["page"] for call in route.calls] == ["0", "1", "0"]
    assert len(events) == 1
    assert events[0].raw["_query_cells"] == ["cell-a", "cell-b"]
    assert source.last_diagnostics["duplicates_removed"] == 1
    assert all("countryCode" not in call.request.url.params for call in route.calls)


@pytest.mark.asyncio
async def test_request_failures_do_not_expose_consumer_key(
    tmp_path, discovery_window
) -> None:
    consumer_key = "super-secret-consumer-key"
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "events.sqlite3",
        ticketmaster_api_key=consumer_key,
        discovery_latitude=40.4406,
        discovery_longitude=-79.9959,
        ticketmaster_max_pages=1,
    )

    with respx.mock(assert_all_called=True) as router:
        router.get(TicketmasterSource.base_url).mock(
            return_value=httpx.Response(401, json={"error": "unauthorized"})
        )
        async with httpx.AsyncClient() as client:
            source = TicketmasterSource(settings, client)
            with pytest.raises(RuntimeError) as error:
                await source.discover(discovery_window)

    assert consumer_key not in str(error.value)
    assert consumer_key not in str(source.last_diagnostics)
    assert source.last_diagnostics["cell_errors"] == {
        "legacy-home": "Ticketmaster returned HTTP 401"
    }
