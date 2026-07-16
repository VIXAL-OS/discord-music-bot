from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_RADIUS_MILES = 3958.7613
MAX_COVERAGE_CELLS = 64
_COVERAGE_SPACING_FACTOR = 0.95
_GEOHASH_ALPHABET = "0123456789bcdefghjkmnpqrstuvwxyz"


@dataclass(frozen=True, slots=True)
class GeoPoint:
    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if not -90 <= self.latitude <= 90:
            raise ValueError("latitude must be between -90 and 90")
        if not -180 <= self.longitude <= 180:
            raise ValueError("longitude must be between -180 and 180")


@dataclass(frozen=True, slots=True)
class CoverageCell:
    name: str
    center: GeoPoint
    radius_miles: int

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("coverage cell name cannot be empty")
        if self.radius_miles <= 0:
            raise ValueError("coverage cell radius must be positive")


def haversine_miles(first: GeoPoint, second: GeoPoint) -> float:
    lat1 = math.radians(first.latitude)
    lat2 = math.radians(second.latitude)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(second.longitude - first.longitude)
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(min(1.0, max(0.0, value))))


def destination_point(origin: GeoPoint, distance_miles: float, bearing_degrees: float) -> GeoPoint:
    angular_distance = distance_miles / EARTH_RADIUS_MILES
    bearing = math.radians(bearing_degrees)
    latitude = math.radians(origin.latitude)
    longitude = math.radians(origin.longitude)

    destination_latitude = math.asin(
        math.sin(latitude) * math.cos(angular_distance)
        + math.cos(latitude) * math.sin(angular_distance) * math.cos(bearing)
    )
    destination_longitude = longitude + math.atan2(
        math.sin(bearing) * math.sin(angular_distance) * math.cos(latitude),
        math.cos(angular_distance) - math.sin(latitude) * math.sin(destination_latitude),
    )
    normalized_longitude = (math.degrees(destination_longitude) + 540) % 360 - 180
    return GeoPoint(math.degrees(destination_latitude), normalized_longitude)


def generate_coverage_cells(
    home: GeoPoint,
    max_radius_miles: int,
    cell_radius_miles: int,
) -> tuple[CoverageCell, ...]:
    if max_radius_miles <= 0 or cell_radius_miles <= 0:
        raise ValueError("travel and cell radii must be positive")
    if cell_radius_miles >= max_radius_miles:
        return (CoverageCell("home-15222", home, cell_radius_miles),)

    horizontal_spacing = (
        math.sqrt(3) * cell_radius_miles * _COVERAGE_SPACING_FACTOR
    )
    vertical_spacing = 1.5 * cell_radius_miles * _COVERAGE_SPACING_FACTOR
    center_limit = max_radius_miles + cell_radius_miles
    max_row = math.ceil(center_limit / vertical_spacing)
    candidates: list[tuple[int, int, float, float]] = []

    for row in range(-max_row, max_row + 1):
        north_offset = row * vertical_spacing
        row_offset = horizontal_spacing / 2 if row % 2 else 0.0
        max_column = math.ceil((center_limit + abs(row_offset)) / horizontal_spacing) + 1
        for column in range(-max_column, max_column + 1):
            east_offset = column * horizontal_spacing + row_offset
            distance = math.hypot(east_offset, north_offset)
            if distance <= center_limit:
                candidates.append((row, column, east_offset, north_offset))
                if len(candidates) > MAX_COVERAGE_CELLS:
                    raise ValueError(
                        f"coverage settings exceed the {MAX_COVERAGE_CELLS}-cell limit; "
                        "increase ticketmaster_cell_radius_miles"
                    )

    cells: list[CoverageCell] = []
    for row, column, east_offset, north_offset in candidates:
        if row == 0 and column == 0:
            name = "home-15222"
            center = home
        else:
            name = f"regional-r{row:+03d}-c{column:+03d}"
            distance = math.hypot(east_offset, north_offset)
            bearing = math.degrees(math.atan2(east_offset, north_offset)) % 360
            center = destination_point(home, distance, bearing)
        cells.append(CoverageCell(name, center, cell_radius_miles))

    return tuple(
        sorted(cells, key=lambda cell: (cell.name != "home-15222", cell.name))
    )


def geohash_encode(point: GeoPoint, precision: int = 9) -> str:
    if precision <= 0:
        raise ValueError("geohash precision must be positive")
    latitude_range = [-90.0, 90.0]
    longitude_range = [-180.0, 180.0]
    bits = (16, 8, 4, 2, 1)
    bit_index = 0
    character = 0
    even = True
    encoded: list[str] = []

    while len(encoded) < precision:
        active_range = longitude_range if even else latitude_range
        value = point.longitude if even else point.latitude
        midpoint = (active_range[0] + active_range[1]) / 2
        if value >= midpoint:
            character |= bits[bit_index]
            active_range[0] = midpoint
        else:
            active_range[1] = midpoint
        even = not even
        if bit_index < 4:
            bit_index += 1
        else:
            encoded.append(_GEOHASH_ALPHABET[character])
            bit_index = 0
            character = 0
    return "".join(encoded)
