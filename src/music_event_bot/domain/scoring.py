from __future__ import annotations

from music_event_bot.domain.geography import GeoPoint, haversine_miles
from music_event_bot.domain.models import DiscoveredEvent, EventRecord, ScoreResult, TasteProfile
from music_event_bot.domain.normalization import normalize_genre, normalize_text

# Words that mark a title as an homage rather than the artist themselves.
# Only consulted for title-fallback matches; exact lineup matches are trusted.
_TRIBUTE_MARKERS = (
    "tribute",
    "experience",
    "plays ",
    "the music of",
    "celebration of",
    "songs of",
    "revue",
    "revisited",
)
_LOCAL_TRIBUTE_MILES = 40


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

    # Distance is computed first so tribute demotion can reason about it.
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

    # Prefer exact matching against the structured lineup (every act on the
    # bill). Fall back to word-boundary title matching only when the source
    # provided no lineup — title text also names other bands and marketing
    # copy ("All Time Low", "LOW TICKETS"), so it stays a last resort.
    event_artists = {normalize_text(name) for name in event.artists}
    event_artists.discard("")
    title_padded = f" {title} "
    title_is_tribute = any(marker in title for marker in _TRIBUTE_MARKERS)
    matched_artist = False
    for preferred in profile.artists:
        preferred_normalized = normalize_text(preferred)
        if not preferred_normalized:
            continue
        exact = preferred_normalized == artist or preferred_normalized in event_artists
        via_title = not event_artists and f" {preferred_normalized} " in title_padded
        if not exact and not via_title:
            continue
        if via_title and title_is_tribute:
            # "STRANGELOVE - The Depeche Mode Experience" is not Depeche
            # Mode. A local tribute night can still reach review; a distant
            # one cannot pass the gate on this evidence alone.
            local = distance_miles is not None and distance_miles <= _LOCAL_TRIBUTE_MILES
            affinity_score += 25 if local else 10
            reasons.append(
                f"possible tribute: {preferred}" + (" (local)" if local else "")
            )
        else:
            affinity_score += 60
            reasons.append(f"artist match: {preferred}")
        matched_artist = True
        break

    # One match per normalized genre, so alias spellings ("alt rock",
    # "alternative", "alternative rock") cannot stack.
    strong_by_norm: dict[str, str] = {}
    for preferred in profile.genres:
        normalized = normalize_genre(preferred)
        if normalized in event_genres and normalized not in strong_by_norm:
            strong_by_norm[normalized] = preferred
    matching_genres = sorted(strong_by_norm.values())
    # A genre named in the title is evidence too: club nights and DIY
    # listings ("Obsidian Goth Night", "Emo Night") carry no tag metadata.
    title_genre_by_norm: dict[str, str] = {}
    for preferred in profile.genres:
        normalized = normalize_genre(preferred)
        if (
            normalized
            and normalized not in strong_by_norm
            and normalized not in title_genre_by_norm
            and f" {normalized} " in title_padded
        ):
            title_genre_by_norm[normalized] = preferred
    title_genre_matches = sorted(title_genre_by_norm.values())
    strong_count = len(matching_genres) + len(title_genre_matches)
    if strong_count:
        affinity_score += min(30, 15 * strong_count)
        if matching_genres:
            reasons.append(f"genre match: {', '.join(matching_genres)}")
        if title_genre_matches:
            reasons.append(f"genre in title: {', '.join(title_genre_matches)}")

    weak_by_norm: dict[str, str] = {}
    for preferred in profile.weak_genres:
        normalized = normalize_genre(preferred)
        if (
            normalized in event_genres
            and normalized not in strong_by_norm
            and normalized not in title_genre_by_norm
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

    # Negative evidence from review history: a headliner whose genre-matched
    # events were rejected gets docked, not banned — strong enough evidence
    # elsewhere on the bill (a liked support act) still surfaces the show.
    if not matched_artist and profile.demoted_artists:
        demoted: dict[str, str] = {}
        for name in profile.demoted_artists:
            normalized = normalize_text(name)
            if normalized:
                demoted.setdefault(normalized, name)
        lineup = (normalize_text(name) for name in event.artists)
        headliner = artist or next((name for name in lineup if name), "")
        hit = demoted.get(headliner, "")
        if not hit and not event_artists and not artist:
            hit = next(
                (
                    display
                    for normalized, display in demoted.items()
                    if f" {normalized} " in title_padded
                ),
                "",
            )
        if hit:
            affinity_score = max(0, affinity_score - 15)
            reasons.append(f"previously rejected artist: {hit} (-15)")

    if location_bonus and distance_miles is not None:
        reasons.append(
            f"distance preference: +{location_bonus} ({distance_miles:.0f} mi from home)"
        )

    return ScoreResult(
        score=min(affinity_score + location_bonus, 100),
        reasons=tuple(reasons),
        affinity_score=affinity_score,
        location_bonus=location_bonus,
        distance_miles=distance_miles,
    )
