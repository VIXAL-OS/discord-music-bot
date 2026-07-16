from __future__ import annotations

import json
from pathlib import Path
from typing import Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from music_event_bot.domain.geography import CoverageCell, GeoPoint, generate_coverage_cells
from music_event_bot.domain.normalization import normalize_genre


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _csv_ints(value: str) -> frozenset[int]:
    parsed: set[int] = set()
    for item in _csv(value):
        try:
            parsed.add(int(item))
        except ValueError as exc:
            raise ValueError(
                f"Expected a comma-separated list of integer IDs, got {item!r}"
            ) from exc
    return frozenset(parsed)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MUSICBOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    database_path: Path = Path("data/events.db")
    default_timezone: str = "America/New_York"
    discovery_start_offset_days: int = 1
    discovery_horizon_days: int = 180
    default_event_duration_minutes: int = 180
    discovery_cron: str = "17 3 * * *"
    review_sync_interval_minutes: int = 15
    minimum_match_score: int = 0
    minimum_affinity_score: int = 15
    # Maximum brand-new review cards posted per sync cycle, highest score
    # first, so a large backlog drains gradually instead of flooding the
    # channel. Already-posted cards are always kept up to date. 0 = no cap.
    review_post_batch_size: int = 25

    discord_token: SecretStr | None = None
    discord_guild_id: int | None = None
    review_channel_id: int | None = None
    announcement_channel_id: int | None = None
    admin_user_ids: str = ""
    reviewer_role_ids: str = ""
    genre_role_map: str = "{}"

    preferred_artists: str = ""
    preferred_genres: str = ""
    preferred_venues: str = ""

    ticketmaster_api_key: SecretStr | None = None
    home_latitude: float = 40.4406
    home_longitude: float = -79.9959
    max_travel_radius_miles: int = 350
    ticketmaster_cell_radius_miles: int = 90
    discovery_latitude: float | None = None
    discovery_longitude: float | None = None
    discovery_radius_miles: int = 75
    ticketmaster_country_code: str = "US"
    ticketmaster_max_pages: int = 2

    ics_urls: str = ""
    rss_urls: str = ""
    squarespace_urls: str = ""

    spotify_client_id: str | None = None
    spotify_client_secret: SecretStr | None = None
    spotify_redirect_uri: str = "http://127.0.0.1:8888/callback"
    spotify_refresh_token: SecretStr | None = None
    spotify_liked_artist_min_tracks: int = 12
    # Re-import the Spotify library after this many hours; between imports the
    # profile is served from the taste_preferences cache. 0 = import every run.
    spotify_refresh_hours: int = 24

    lastfm_api_key: SecretStr | None = None
    lastfm_min_tag_weight: int = 10
    lastfm_max_tags_per_artist: int = 8
    # Re-fetch an artist's tags after this many days and merge the results
    # into the stored set (existing tags are never removed, so an evolving
    # artist accumulates old and new genres). 0 = fetch once, never refresh.
    lastfm_tag_cache_days: int = 90

    # Keyless fallback tag source, used only when no Last.fm key is set.
    musicbrainz_enabled: bool = True

    # Genres too broad to justify review on their own. They (and all genres
    # produced by upward mapping) count as weak scoring evidence only.
    umbrella_genres: str = (
        "rock, pop, alternative, alternative rock, alt rock, indie, indie rock, "
        "electronic, dance, dance/electronic, edm, country, folk, americana, jazz, "
        "blues, soul, funk, rnb, r&b, hip hop, rap, latin, world, reggae, classical, "
        "classic rock, singer-songwriter, new age, adult contemporary, easy listening, "
        "pop rock, dance pop, top 40, oldies"
    )
    # Deployment-specific additions, appended to the defaults above.
    umbrella_genres_extra: str = ""

    anthropic_api_key: SecretStr | None = None
    genre_map_model: str = "claude-sonnet-5"

    @field_validator("default_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown IANA timezone: {value}") from exc
        return value

    @field_validator(
        "discovery_horizon_days",
        "default_event_duration_minutes",
        "review_sync_interval_minutes",
        "max_travel_radius_miles",
        "ticketmaster_cell_radius_miles",
        "discovery_radius_miles",
        "ticketmaster_max_pages",
        "spotify_liked_artist_min_tracks",
        "lastfm_max_tags_per_artist",
    )
    @classmethod
    def validate_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("Value must be greater than zero")
        return value

    @field_validator("minimum_match_score", "minimum_affinity_score", "lastfm_min_tag_weight")
    @classmethod
    def validate_score(cls, value: int) -> int:
        if not 0 <= value <= 100:
            raise ValueError("score thresholds must be between 0 and 100")
        return value

    @field_validator("discovery_start_offset_days")
    @classmethod
    def validate_start_offset(cls, value: int) -> int:
        if value < 0:
            raise ValueError("discovery_start_offset_days cannot be negative")
        return value

    @field_validator("lastfm_tag_cache_days")
    @classmethod
    def validate_tag_cache_days(cls, value: int) -> int:
        if value < 0:
            raise ValueError("lastfm_tag_cache_days cannot be negative (0 means keep forever)")
        return value

    @field_validator("spotify_refresh_hours")
    @classmethod
    def validate_spotify_refresh_hours(cls, value: int) -> int:
        if value < 0:
            raise ValueError("spotify_refresh_hours cannot be negative (0 means every run)")
        return value

    @field_validator("review_post_batch_size")
    @classmethod
    def validate_review_post_batch_size(cls, value: int) -> int:
        if value < 0:
            raise ValueError("review_post_batch_size cannot be negative (0 means no cap)")
        return value

    @field_validator("ticketmaster_country_code")
    @classmethod
    def validate_country_code(cls, value: str) -> str:
        normalized = value.strip().upper()
        if len(normalized) != 2 or not normalized.isalpha():
            raise ValueError("ticketmaster_country_code must be a two-letter country code")
        return normalized

    @model_validator(mode="after")
    def validate_location_pair(self) -> Self:
        if (self.discovery_latitude is None) != (self.discovery_longitude is None):
            raise ValueError("discovery_latitude and discovery_longitude must be set together")
        GeoPoint(self.home_latitude, self.home_longitude)
        if self.discovery_latitude is not None and self.discovery_longitude is not None:
            GeoPoint(self.discovery_latitude, self.discovery_longitude)
        if self.discovery_horizon_days < self.discovery_start_offset_days:
            raise ValueError("discovery_horizon_days must be at least the start offset")
        _ = self.ticketmaster_cells
        return self

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.default_timezone)

    @property
    def admins(self) -> frozenset[int]:
        return _csv_ints(self.admin_user_ids)

    @property
    def reviewer_roles(self) -> frozenset[int]:
        return _csv_ints(self.reviewer_role_ids)

    @property
    def artists(self) -> tuple[str, ...]:
        return _csv(self.preferred_artists)

    @property
    def genres(self) -> tuple[str, ...]:
        return _csv(self.preferred_genres)

    @property
    def venues(self) -> tuple[str, ...]:
        return _csv(self.preferred_venues)

    @property
    def calendar_urls(self) -> tuple[str, ...]:
        return _csv(self.ics_urls)

    @property
    def feed_urls(self) -> tuple[str, ...]:
        return _csv(self.rss_urls)

    @property
    def squarespace_event_urls(self) -> tuple[str, ...]:
        return _csv(self.squarespace_urls)

    @property
    def role_map(self) -> dict[str, int]:
        try:
            raw = json.loads(self.genre_role_map)
        except json.JSONDecodeError as exc:
            raise ValueError("genre_role_map must be a JSON object") from exc
        if not isinstance(raw, dict):
            raise ValueError("genre_role_map must be a JSON object")
        result: dict[str, int] = {}
        for genre, role_id in raw.items():
            result[str(genre).strip().casefold()] = int(role_id)
        return result

    @property
    def home_point(self) -> GeoPoint:
        if self.discovery_latitude is not None and self.discovery_longitude is not None:
            return GeoPoint(self.discovery_latitude, self.discovery_longitude)
        return GeoPoint(self.home_latitude, self.home_longitude)

    @property
    def ticketmaster_cells(self) -> tuple[CoverageCell, ...]:
        if self.discovery_latitude is not None and self.discovery_longitude is not None:
            return (
                CoverageCell(
                    "legacy-home",
                    self.home_point,
                    self.discovery_radius_miles,
                ),
            )
        return generate_coverage_cells(
            self.home_point,
            self.max_travel_radius_miles,
            self.ticketmaster_cell_radius_miles,
        )

    @property
    def discord_configured(self) -> bool:
        return all(
            (
                self.discord_token,
                self.discord_guild_id,
                self.review_channel_id,
                self.announcement_channel_id,
            )
        ) and bool(self.admins or self.reviewer_roles)

    def require_discord(self) -> None:
        if self.discord_configured:
            return
        raise ValueError(
            "Discord mode requires MUSICBOT_DISCORD_TOKEN, MUSICBOT_DISCORD_GUILD_ID, "
            "MUSICBOT_REVIEW_CHANNEL_ID, MUSICBOT_ANNOUNCEMENT_CHANNEL_ID, and at least "
            "one admin user or reviewer role ID"
        )

    @property
    def ticketmaster_configured(self) -> bool:
        return bool(self.ticketmaster_api_key and self.ticketmaster_cells)

    @property
    def spotify_configured(self) -> bool:
        return bool(
            self.spotify_client_id
            and self.spotify_client_secret
            and self.spotify_refresh_token
        )

    @property
    def umbrella_genre_set(self) -> frozenset[str]:
        return frozenset(
            normalize_genre(genre)
            for genre in _csv(self.umbrella_genres) + _csv(self.umbrella_genres_extra)
        )

    @property
    def lastfm_configured(self) -> bool:
        return self.lastfm_api_key is not None

    @property
    def anthropic_configured(self) -> bool:
        return self.anthropic_api_key is not None
