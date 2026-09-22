"""The metros members can pick as their home, and how far they will travel.

A named metro rather than a home point and a radius, for two reasons. It
needs no geocoder — the choice list is the whole vocabulary — and under
public mentions it is coarse enough to say out loud: "Avery watches
Cleveland" is something they would tell you anyway, where a precise point
plus radius approximates where they sleep.

The roster covers the metros inside the discovery radius that actually book
shows the server cares about. Adding one is a line here; nothing else needs
to change.
"""

from __future__ import annotations

from dataclasses import dataclass

from music_event_bot.domain.geography import GeoPoint, haversine_miles


@dataclass(frozen=True, slots=True)
class Metro:
    key: str
    label: str
    center: GeoPoint


@dataclass(frozen=True, slots=True)
class TravelBand:
    key: str
    label: str
    radius_miles: int


METROS: tuple[Metro, ...] = (
    Metro("pittsburgh", "Pittsburgh", GeoPoint(40.4406, -79.9959)),
    Metro("cleveland", "Cleveland", GeoPoint(41.4993, -81.6944)),
    Metro("columbus", "Columbus", GeoPoint(39.9612, -82.9988)),
    Metro("buffalo", "Buffalo", GeoPoint(42.8864, -78.8784)),
    Metro("toronto", "Toronto", GeoPoint(43.6532, -79.3832)),
    Metro("detroit", "Detroit", GeoPoint(42.3314, -83.0458)),
    Metro("philadelphia", "Philadelphia", GeoPoint(39.9526, -75.1652)),
    Metro("new-york", "New York", GeoPoint(40.7128, -74.0060)),
    Metro("baltimore", "Baltimore", GeoPoint(39.2904, -76.6122)),
    Metro("washington-dc", "Washington DC", GeoPoint(38.9072, -77.0369)),
    Metro("morgantown", "Morgantown", GeoPoint(39.6295, -79.9559)),
)

# "How far will you go for a show", not a precision setting. road_trip is the
# widest band on purpose: it is what every member effectively has today, so
# seeding everyone into it changes nobody's coverage.
TRAVEL_BANDS: tuple[TravelBand, ...] = (
    TravelBand("in-town", "In town", 40),
    TravelBand("day-trip", "Day trip", 120),
    TravelBand("road-trip", "Road trip", 350),
)

DEFAULT_TRAVEL_BAND = "road-trip"

_METROS_BY_KEY = {metro.key: metro for metro in METROS}
_BANDS_BY_KEY = {band.key: band for band in TRAVEL_BANDS}


def metro(key: str) -> Metro:
    try:
        return _METROS_BY_KEY[key]
    except KeyError:
        raise ValueError(
            f"Unknown metro {key!r}; expected one of {', '.join(sorted(_METROS_BY_KEY))}"
        ) from None


def travel_band(key: str) -> TravelBand:
    try:
        return _BANDS_BY_KEY[key]
    except KeyError:
        raise ValueError(
            f"Unknown travel band {key!r}; expected one of {', '.join(sorted(_BANDS_BY_KEY))}"
        ) from None


def nearest_metro(point: GeoPoint) -> Metro:
    """The metro a coordinate sits in, for defaulting a seeded profile."""
    return min(METROS, key=lambda candidate: haversine_miles(point, candidate.center))
