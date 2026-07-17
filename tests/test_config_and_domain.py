from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from music_event_bot.config import Settings
from music_event_bot.discovery.ticketmaster import TicketmasterSource
from music_event_bot.domain.geography import (
    GeoPoint,
    destination_point,
    geohash_encode,
    haversine_miles,
)
from music_event_bot.domain.models import DiscoveredEvent, TasteProfile
from music_event_bot.domain.normalization import (
    canonical_fingerprint,
    normalize_genres,
    normalize_text,
    normalize_url,
)
from music_event_bot.domain.scoring import score_event
from music_event_bot.taste.spotify import SpotifyTasteImporter


def test_settings_parses_lists_roles_and_configured_integrations(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "configured.sqlite3",
        preferred_artists=" Björk, The Cure, ",
        preferred_genres="EDM, indie",
        preferred_venues=" Main Hall, Warehouse ",
        admin_user_ids="1, 2, 1",
        reviewer_role_ids="10, 20",
        genre_role_map='{" Indie Rock ": "42", "Electronic": 99}',
        ics_urls="webcal://calendar.example/events.ics, https://other.example/events.ics",
        rss_urls="https://feeds.example/music.xml",
        discord_token="discord-token",
        discord_guild_id=100,
        review_channel_id=200,
        announcement_channel_id=300,
        ticketmaster_api_key="ticketmaster-key",
        discovery_latitude=40.7,
        discovery_longitude=-74.0,
        ticketmaster_country_code="ca",
        spotify_client_id="client-id",
        spotify_client_secret="client-secret",
        spotify_refresh_token="refresh-token",
    )

    assert settings.artists == ("Björk", "The Cure")
    assert settings.genres == ("EDM", "indie")
    assert settings.venues == ("Main Hall", "Warehouse")
    assert settings.admins == frozenset({1, 2})
    assert settings.reviewer_roles == frozenset({10, 20})
    assert settings.role_map == {"indie rock": 42, "electronic": 99}
    assert settings.calendar_urls == (
        "webcal://calendar.example/events.ics",
        "https://other.example/events.ics",
    )
    assert settings.feed_urls == ("https://feeds.example/music.xml",)
    assert settings.discord_configured
    assert settings.ticketmaster_configured
    assert len(settings.ticketmaster_cells) == 1
    assert settings.ticketmaster_cells[0].name == "legacy-home"
    assert settings.ticketmaster_country_code == "CA"
    assert settings.spotify_configured


def test_settings_rejects_invalid_region_configuration() -> None:
    with pytest.raises(ValidationError, match="must be set together"):
        Settings(_env_file=None, discovery_latitude=40.7)
    with pytest.raises(ValidationError, match="at least the start offset"):
        Settings(_env_file=None, discovery_start_offset_days=20, discovery_horizon_days=10)
    with pytest.raises(ValidationError, match="latitude"):
        Settings(_env_file=None, home_latitude=100)
    with pytest.raises(ValidationError, match="two-letter country code"):
        Settings(_env_file=None, ticketmaster_country_code="USA")
    with pytest.raises(ValidationError, match="64-cell limit"):
        Settings(_env_file=None, ticketmaster_cell_radius_miles=10)


@pytest.mark.asyncio
async def test_disabled_integrations_short_circuit_without_network(
    settings: Settings, discovery_window
) -> None:
    assert not settings.ticketmaster_configured
    assert not settings.spotify_configured
    with pytest.raises(ValueError, match="Discord mode requires"):
        settings.require_discord()

    assert await TicketmasterSource(settings).discover(discovery_window) == []
    assert await SpotifyTasteImporter(settings).import_profile() == TasteProfile()


def test_normalization_canonical_identity_and_scoring() -> None:
    assert normalize_text("  Björk — LIVE!! ") == "björk live"
    assert normalize_genres(("EDM", "Electronica", "Indie")) == (
        "electronic",
        "indie rock",
    )
    assert (
        normalize_url("HTTPS://Example.COM/Shows?z=last&a=first#details")
        == "https://example.com/Shows?a=first&z=last"
    )

    start_utc = datetime(2026, 7, 12, 20, 0, 45, tzinfo=UTC)
    start_local = datetime(2026, 7, 12, 16, 0, 5, tzinfo=ZoneInfo("America/New_York"))
    canonical = canonical_fingerprint(
        "Björk Live", "Main Hall", start_utc, source_name="source-a", source_event_id="1"
    )
    assert canonical == canonical_fingerprint(
        "  BJÖRK  live ", "main hall", start_local, source_name="source-b", source_event_id="2"
    )
    assert canonical_fingerprint(
        "Untimed", None, None, source_name="source-a", source_event_id="1"
    ) != canonical_fingerprint("Untimed", None, None, source_name="source-a", source_event_id="2")

    event = DiscoveredEvent(
        source_name="test",
        source_event_id="score-1",
        title="Björk Live in Brooklyn",
        venue="Main Hall",
        artist=None,
        genres=("EDM", "Indie"),
    )
    result = score_event(
        event,
        TasteProfile(
            artists=("Björk",),
            genres=("indie", "electronic", "unmatched"),
            venues=("main hall",),
        ),
    )

    assert result.score == 100
    assert result.affinity_score == 100
    assert result.reasons == (
        "artist match: Björk",
        "genre match: electronic, indie",
        "venue match: main hall",
    )

    venue_only = score_event(
        DiscoveredEvent(
            source_name="test",
            source_event_id="venue-1",
            title="Whoever Is Playing Tonight",
            venue="Main Hall",
        ),
        TasteProfile(venues=("main hall",)),
    )
    # A pinned venue alone stays below the 15-point review gate.
    assert venue_only.affinity_score == 10


def test_artist_title_match_requires_word_boundaries() -> None:
    profile = TasteProfile(artists=("Low",))
    inside_word = DiscoveredEvent(
        source_name="test",
        source_event_id="wb-1",
        title="The Slow Death, Shallowater",
    )
    assert score_event(inside_word, profile).affinity_score == 0

    standalone = DiscoveredEvent(
        source_name="test",
        source_event_id="wb-2",
        title="An Evening With Low",
    )
    result = score_event(standalone, profile)
    assert result.affinity_score == 60
    assert result.reasons == ("artist match: Low",)


def test_structured_lineup_beats_title_heuristics() -> None:
    profile = TasteProfile(artists=("Low", "200 Stab Wounds"))

    # A support slot anywhere on the bill is an exact match.
    support_slot = DiscoveredEvent(
        source_name="test",
        source_event_id="bill-1",
        title="Despised Icon at The Phoenix",
        artist="Despised Icon",
        artists=("Despised Icon", "200 Stab Wounds", "TEETH"),
    )
    result = score_event(support_slot, profile)
    assert result.affinity_score == 60
    assert result.reasons == ("artist match: 200 Stab Wounds",)

    # When the lineup is known, title text is not consulted: another band
    # whose name contains "Low" as a word no longer matches.
    other_band = DiscoveredEvent(
        source_name="test",
        source_event_id="bill-2",
        title="All Time Low",
        artist="All Time Low",
        artists=("All Time Low",),
    )
    assert score_event(other_band, profile).affinity_score == 0

    # Marketing text in the title is also ignored once a lineup exists.
    marketing = DiscoveredEvent(
        source_name="test",
        source_event_id="bill-3",
        title="Eddie 9V - Low tix warning!",
        artist="Eddie 9V",
        artists=("Eddie 9V",),
    )
    assert score_event(marketing, profile).affinity_score == 0


def test_tribute_titles_demoted_unless_local() -> None:
    from music_event_bot.domain.geography import GeoPoint

    profile = TasteProfile(artists=("Depeche Mode",))
    home = GeoPoint(40.44, -79.99)  # Pittsburgh

    def tribute(source_event_id: str, latitude: float, longitude: float) -> DiscoveredEvent:
        return DiscoveredEvent(
            source_name="test",
            source_event_id=source_event_id,
            title="STRANGELOVE - The Depeche Mode Experience",
            venue_latitude=latitude,
            venue_longitude=longitude,
        )

    # A tribute night in town can still reach review, at reduced weight.
    nearby = score_event(tribute("trib-1", 40.45, -79.98), profile, home=home)
    assert nearby.affinity_score == 25
    assert "possible tribute: Depeche Mode (local)" in nearby.reasons

    # The same act two hours away cannot pass the gate on this evidence.
    cleveland = score_event(tribute("trib-2", 41.50, -81.58), profile, home=home)
    assert cleveland.affinity_score == 10
    assert "possible tribute: Depeche Mode" in cleveland.reasons

    # A structured lineup naming the artist is trusted even when the title
    # carries a tribute-flavored word.
    real = DiscoveredEvent(
        source_name="test",
        source_event_id="trib-3",
        title="Depeche Mode: Memento Mori Revisited",
        artists=("Depeche Mode",),
        venue_latitude=41.50,
        venue_longitude=-81.58,
    )
    result = score_event(real, profile, home=home)
    assert result.affinity_score == 60
    assert "artist match: Depeche Mode" in result.reasons


def test_genre_alias_spellings_count_once() -> None:
    event = DiscoveredEvent(
        source_name="test",
        source_event_id="alias-1",
        title="Alias Night",
        genres=("Alternative",),
    )
    result = score_event(
        event,
        TasteProfile(genres=("alt rock", "alternative", "alternative rock")),
    )
    assert result.affinity_score == 15
    assert result.reasons == ("genre match: alt rock",)


def test_weak_genres_boost_but_cannot_pass_the_review_gate() -> None:
    event = DiscoveredEvent(
        source_name="test",
        source_event_id="score-2",
        title="Generic Arena Night",
        genres=("Rock", "Pop", "Country"),
    )
    # Weak-only evidence caps at 10 affinity — below the default gate of 15.
    weak_only = score_event(
        event, TasteProfile(weak_genres=("rock", "pop", "country"))
    )
    assert weak_only.affinity_score == 10
    assert weak_only.reasons == ("related genre match: country, pop, rock",)

    # A strong niche match plus weak corroboration stacks.
    niche_event = DiscoveredEvent(
        source_name="test",
        source_event_id="score-3",
        title="Basement Show",
        genres=("Industrial", "Rock"),
    )
    combined = score_event(
        niche_event,
        TasteProfile(genres=("industrial",), weak_genres=("rock", "industrial")),
    )
    assert combined.affinity_score == 20
    assert combined.reasons == (
        "genre match: industrial",
        "related genre match: rock",
    )


def test_umbrella_genre_set_normalizes_aliases(tmp_path) -> None:
    settings = Settings(_env_file=None, database_path=tmp_path / "db.sqlite3")
    umbrella = settings.umbrella_genre_set
    assert "alternative rock" in umbrella  # "alt rock" and "alternative" collapse into it
    assert "electronic" in umbrella  # from "edm"
    assert "hip hop" in umbrella  # from "rap"
    assert "industrial" not in umbrella
    assert "metal" not in umbrella

    extra = Settings(
        _env_file=None,
        database_path=tmp_path / "db2.sqlite3",
        umbrella_genres_extra="Indie Folk, gospel",
    )
    assert "indie folk" in extra.umbrella_genre_set
    assert "gospel" in extra.umbrella_genre_set
    assert "alternative rock" in extra.umbrella_genre_set  # defaults still apply


def test_regional_coverage_and_distance_scoring() -> None:
    settings = Settings(_env_file=None)
    home = settings.home_point
    city_points = {
        "Youngstown": GeoPoint(41.0998, -80.6495),
        "Morgantown": GeoPoint(39.6295, -79.9559),
        "Harrisburg": GeoPoint(40.2732, -76.8867),
        "Cleveland": GeoPoint(41.4993, -81.6944),
        "Columbus": GeoPoint(39.9612, -82.9988),
        "Philadelphia": GeoPoint(39.9526, -75.1652),
        "Buffalo": GeoPoint(42.8864, -78.8784),
        "Toronto": GeoPoint(43.6532, -79.3832),
    }
    assert len(settings.ticketmaster_cells) == 31
    for point in city_points.values():
        nearest = min(haversine_miles(cell.center, point) for cell in settings.ticketmaster_cells)
        assert nearest <= settings.ticketmaster_cell_radius_miles

    for radius in range(0, settings.max_travel_radius_miles + 1, 10):
        for bearing in range(0, 360, 5):
            point = destination_point(home, radius, bearing)
            nearest = min(
                haversine_miles(cell.center, point)
                for cell in settings.ticketmaster_cells
            )
            assert nearest <= settings.ticketmaster_cell_radius_miles

    local_unrelated = DiscoveredEvent(
        source_name="test",
        source_event_id="local",
        title="Unrelated Local Show",
        venue_latitude=home.latitude,
        venue_longitude=home.longitude,
    )
    distant_favorite = DiscoveredEvent(
        source_name="test",
        source_event_id="distant",
        title="Favorite Artist in Philadelphia",
        artist="Favorite Artist",
        venue_latitude=city_points["Philadelphia"].latitude,
        venue_longitude=city_points["Philadelphia"].longitude,
    )
    local_score = score_event(local_unrelated, TasteProfile(), home=home)
    distant_score = score_event(
        distant_favorite,
        TasteProfile(artists=("Favorite Artist",)),
        home=home,
    )
    assert local_score.affinity_score == 0
    assert local_score.location_bonus == 20
    assert distant_score.affinity_score == 60
    assert distant_score.score > local_score.score
    assert geohash_encode(GeoPoint(42.6, -5.6), precision=5) == "ezs42"
