from __future__ import annotations

from music_event_bot.domain.geography import GeoPoint, haversine_miles
from music_event_bot.domain.models import DiscoveredEvent, EventRecord, ScoreResult, TasteProfile
from music_event_bot.domain.normalization import normalize_genre, normalize_text


def score_event(
    event: DiscoveredEvent | EventRecord,
    profile: TasteProfile,
    *,
    home: GeoPoint | None = None,
    max_travel_radius_miles: int = 350,
) -> ScoreResult:
    title = normalize_text(event.title)
    artist = normalize_text(event.artist)
    venue = normalize_text(event.venue)
    event_genres = {normalize_genre(genre) for genre in event.genres}

    affinity_score = 0
    reasons: list[str] = []

    # Prefer exact matching against the structured lineup (every act on the
    # bill). Fall back to word-boundary title matching only when the source
    # provided no lineup — title text also names other bands and marketing
    # copy ("All Time Low", "LOW TICKETS"), so it stays a last resort.
    event_artists = {normalize_text(name) for name in event.artists}
    event_artists.discard("")
    title_padded = f" {title} "
    for preferred in profile.artists:
        preferred_normalized = normalize_text(preferred)
        if not preferred_normalized:
            continue
        if (
            preferred_normalized == artist
            or preferred_normalized in event_artists
            or (
                not event_artists
                and f" {preferred_normalized} " in title_padded
            )
        ):
            affinity_score += 60
            reasons.append(f"artist match: {preferred}")
            break

    # One match per normalized genre, so alias spellings ("alt rock",
    # "alternative", "alternative rock") cannot stack.
    strong_by_norm: dict[str, str] = {}
    for preferred in profile.genres:
        normalized = normalize_genre(preferred)
        if normalized in event_genres and normalized not in strong_by_norm:
            strong_by_norm[normalized] = preferred
    matching_genres = sorted(strong_by_norm.values())
    if matching_genres:
        affinity_score += min(30, 15 * len(matching_genres))
        reasons.append(f"genre match: {', '.join(matching_genres)}")

    weak_by_norm: dict[str, str] = {}
    for preferred in profile.weak_genres:
        normalized = normalize_genre(preferred)
        if (
            normalized in event_genres
            and normalized not in strong_by_norm
            and normalized not in weak_by_norm
        ):
            weak_by_norm[normalized] = preferred
    weak_matches = sorted(weak_by_norm.values())
    if weak_matches:
        affinity_score += min(10, 5 * len(weak_matches))
        reasons.append(f"related genre match: {', '.join(weak_matches)}")

    # A pinned venue is corroboration, not a ticket in: 10 points cannot pass
    # the review gate without a matching artist or genre.
    for preferred in profile.venues:
        preferred_normalized = normalize_text(preferred)
        if preferred_normalized and preferred_normalized == venue:
            affinity_score += 10
            reasons.append(f"venue match: {preferred}")
            break

    location_bonus = 0
    distance_miles: float | None = None
    if (
        home is not None
        and event.venue_latitude is not None
        and event.venue_longitude is not None
        and max_travel_radius_miles > 0
    ):
        venue_point = GeoPoint(event.venue_latitude, event.venue_longitude)
        distance_miles = haversine_miles(home, venue_point)
        if distance_miles <= max_travel_radius_miles:
            remaining = max(0.0, 1 - distance_miles / max_travel_radius_miles)
            location_bonus = round(20 * remaining**2)
            if location_bonus:
                reasons.append(
                    f"distance preference: +{location_bonus} "
                    f"({distance_miles:.0f} mi from home)"
                )

    return ScoreResult(
        score=min(affinity_score + location_bonus, 100),
        reasons=tuple(reasons),
        affinity_score=affinity_score,
        location_bonus=location_bonus,
        distance_miles=distance_miles,
    )
