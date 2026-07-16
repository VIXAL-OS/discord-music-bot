from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from spotipy.exceptions import SpotifyException

from music_event_bot.config import Settings
from music_event_bot.domain.models import TasteProfile
from music_event_bot.storage.repositories import EventRepository
from music_event_bot.taste.enrichment import TasteEnricher, default_tag_fetcher
from music_event_bot.taste.genre_mapping import GenreTagMapper
from music_event_bot.taste.lastfm import LastfmTagFetcher
from music_event_bot.taste.musicbrainz import MusicbrainzTagFetcher
from music_event_bot.taste.spotify import build_profile_from_spotify


def _track(artist: str, *featured: str) -> dict[str, Any]:
    return {"artists": [{"name": name} for name in (artist, *featured)]}


class FakeSpotifyClient:
    def __init__(
        self,
        top_artists: list[dict[str, Any]] | None = None,
        top_tracks: list[dict[str, Any]] | None = None,
        saved_tracks: list[dict[str, Any]] | None = None,
        saved_error: Exception | None = None,
    ) -> None:
        self.top_artists = top_artists or []
        self.top_tracks = top_tracks or []
        self.saved_tracks = saved_tracks or []
        self.saved_error = saved_error

    def current_user_top_artists(self, limit: int, time_range: str) -> dict[str, Any]:
        return {"items": self.top_artists if time_range == "short_term" else []}

    def current_user_top_tracks(self, limit: int, time_range: str) -> dict[str, Any]:
        return {"items": self.top_tracks if time_range == "short_term" else []}

    def current_user_saved_tracks(self, limit: int, offset: int) -> dict[str, Any]:
        if self.saved_error is not None:
            raise self.saved_error
        page = self.saved_tracks[offset : offset + limit]
        has_more = offset + limit < len(self.saved_tracks)
        return {"items": page, "next": "next-page" if has_more else None}


class TestSpotifyProfile:
    def test_liked_songs_threshold_and_heavy_rotation(self) -> None:
        saved = (
            [{"track": _track("Chelsea Wolfe")} for _ in range(12)]
            + [{"track": _track("Some One-Off")} for _ in range(3)]
            # Featured credit must not count toward the featured artist.
            + [{"track": _track("Chelsea Wolfe", "Converge")}]
        )
        client = FakeSpotifyClient(
            top_artists=[{"name": "Godspeed You! Black Emperor", "genres": ["post-rock"]}],
            top_tracks=[_track("Lingua Ignota")],
            saved_tracks=saved,
        )
        profile = build_profile_from_spotify(client, min_liked_tracks=12)
        assert "Chelsea Wolfe" in profile.artists
        assert "Lingua Ignota" in profile.artists
        assert "Godspeed You! Black Emperor" in profile.artists
        assert "Some One-Off" not in profile.artists
        assert "Converge" not in profile.artists
        assert "post-rock" in profile.genres

    def test_saved_tracks_pagination(self) -> None:
        saved = [{"track": _track("Swans")} for _ in range(120)]
        client = FakeSpotifyClient(saved_tracks=saved)
        profile = build_profile_from_spotify(client, min_liked_tracks=100)
        assert profile.artists == ("Swans",)

    def test_missing_library_scope_keeps_top_data(self) -> None:
        client = FakeSpotifyClient(
            top_artists=[{"name": "Have a Nice Life", "genres": []}],
            saved_error=SpotifyException(403, -1, "insufficient scope"),
        )
        profile = build_profile_from_spotify(client, min_liked_tracks=12)
        assert profile.artists == ("Have a Nice Life",)

    def test_other_spotify_errors_propagate(self) -> None:
        client = FakeSpotifyClient(saved_error=SpotifyException(500, -1, "server error"))
        with pytest.raises(SpotifyException):
            build_profile_from_spotify(client, min_liked_tracks=12)


def _lastfm_payload(*tags: tuple[str, int]) -> dict[str, Any]:
    return {"toptags": {"tag": [{"name": name, "count": count} for name, count in tags]}}


class TestLastfmTagFetcher:
    @respx.mock
    async def test_filters_junk_weight_and_caps(self, tmp_path: Path) -> None:
        settings = Settings(
            _env_file=None,
            database_path=tmp_path / "db.sqlite3",
            lastfm_api_key="key",
            lastfm_min_tag_weight=10,
            lastfm_max_tags_per_artist=3,
        )
        respx.get("https://ws.audioscrobbler.com/2.0/").mock(
            return_value=httpx.Response(
                200,
                json=_lastfm_payload(
                    ("Dungeon Synth", 100),
                    ("seen live", 95),
                    ("martial industrial", 80),
                    ("Coil", 70),  # matches the artist name
                    ("darkwave", 60),
                    ("witch house", 50),
                    ("obscure", 5),  # below weight threshold
                ),
            )
        )
        fetcher = LastfmTagFetcher(settings)
        tags = await fetcher.top_tags("Coil")
        assert tags == [
            ("dungeon synth", 100),
            ("martial industrial", 80),
            ("darkwave", 60),
        ]

    @respx.mock
    async def test_unknown_artist_returns_empty(self, tmp_path: Path) -> None:
        settings = Settings(
            _env_file=None, database_path=tmp_path / "db.sqlite3", lastfm_api_key="key"
        )
        respx.get("https://ws.audioscrobbler.com/2.0/").mock(
            return_value=httpx.Response(
                200, json={"error": 6, "message": "The artist you supplied could not be found"}
            )
        )
        fetcher = LastfmTagFetcher(settings)
        assert await fetcher.top_tags("Nonexistent") == []

    async def test_without_key_returns_empty(self, settings: Settings) -> None:
        fetcher = LastfmTagFetcher(settings)
        assert await fetcher.top_tags("Coil") == []


def _spotify_settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        database_path=tmp_path / "spotify.sqlite3",
        spotify_client_id="client-id",
        spotify_client_secret="client-secret",
        spotify_refresh_token="refresh-token",
        **overrides,
    )


class TestCachedSpotifyImport:
    async def test_fresh_cache_skips_spotify_entirely(
        self, repository: EventRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from music_event_bot.taste.spotify import SpotifyTasteImporter

        await repository.store_taste_preferences("artist", {"Coil"}, "spotify")
        importer = SpotifyTasteImporter(_spotify_settings(tmp_path), repository)

        def explode() -> TasteProfile:
            raise AssertionError("Spotify should not be contacted while the cache is fresh")

        monkeypatch.setattr(importer, "_import_sync", explode)
        profile = await importer.import_profile()
        assert profile.artists == ("Coil",)

    async def test_import_failure_falls_back_to_cache(
        self, repository: EventRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from music_event_bot.taste.spotify import SpotifyTasteImporter

        await repository.store_taste_preferences("artist", {"Coil"}, "spotify")
        settings = _spotify_settings(tmp_path, spotify_refresh_hours=0)
        importer = SpotifyTasteImporter(settings, repository)

        def rate_limited() -> TasteProfile:
            raise SpotifyException(429, -1, "rate limited")

        monkeypatch.setattr(importer, "_import_sync", rate_limited)
        profile = await importer.import_profile()
        assert profile.artists == ("Coil",)

    async def test_successful_import_merges_and_persists(
        self, repository: EventRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from music_event_bot.taste.spotify import SpotifyTasteImporter

        await repository.store_taste_preferences("artist", {"Coil"}, "spotify")
        settings = _spotify_settings(tmp_path, spotify_refresh_hours=0)
        importer = SpotifyTasteImporter(settings, repository)
        monkeypatch.setattr(
            importer, "_import_sync", lambda: TasteProfile(artists=("Swans",))
        )
        profile = await importer.import_profile()
        assert profile.artists == ("Coil", "Swans")
        assert await repository.get_taste_preferences("artist", "spotify") == (
            "Coil",
            "Swans",
        )


class TestMusicbrainzTagFetcher:
    @respx.mock
    async def test_parses_search_result_tags(self, settings: Settings) -> None:
        respx.get("https://musicbrainz.org/ws/2/artist").mock(
            return_value=httpx.Response(
                200,
                json={
                    "artists": [
                        {
                            "id": "mbid-1",
                            "name": "Coil",
                            "score": 100,
                            "tags": [
                                {"name": "Industrial", "count": 6},
                                {"name": "seen live", "count": 9},
                                {"name": "dark ambient", "count": 3},
                                {"name": "untagged", "count": 0},
                            ],
                        }
                    ]
                },
            )
        )
        fetcher = MusicbrainzTagFetcher(settings)
        tags = await fetcher.top_tags("Coil")
        assert tags == [("industrial", 6), ("dark ambient", 3)]

    @respx.mock
    async def test_low_confidence_match_is_ignored(self, settings: Settings) -> None:
        respx.get("https://musicbrainz.org/ws/2/artist").mock(
            return_value=httpx.Response(
                200,
                json={
                    "artists": [
                        {
                            "id": "mbid-2",
                            "name": "Completely Different Band",
                            "score": 55,
                            "tags": [{"name": "polka", "count": 4}],
                        }
                    ]
                },
            )
        )
        fetcher = MusicbrainzTagFetcher(settings)
        assert await fetcher.top_tags("Obscure Basement Act") == []

    @respx.mock
    async def test_no_results_returns_empty(self, settings: Settings) -> None:
        respx.get("https://musicbrainz.org/ws/2/artist").mock(
            return_value=httpx.Response(200, json={"artists": []})
        )
        fetcher = MusicbrainzTagFetcher(settings)
        assert await fetcher.top_tags("Nonexistent") == []


class TestDefaultTagFetcher:
    def test_lastfm_key_wins(self, tmp_path: Path) -> None:
        settings = Settings(
            _env_file=None, database_path=tmp_path / "db.sqlite3", lastfm_api_key="key"
        )
        assert isinstance(default_tag_fetcher(settings), LastfmTagFetcher)

    def test_musicbrainz_is_keyless_fallback(self, settings: Settings) -> None:
        assert isinstance(default_tag_fetcher(settings), MusicbrainzTagFetcher)

    def test_disabled_fallback_yields_none(self, tmp_path: Path) -> None:
        settings = Settings(
            _env_file=None,
            database_path=tmp_path / "db.sqlite3",
            musicbrainz_enabled=False,
        )
        assert default_tag_fetcher(settings) is None


def _fake_claude_response(mappings: list[dict[str, Any]]) -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=json.dumps({"mappings": mappings}))],
    )


class FakeAnthropicClient:
    def __init__(self, response: SimpleNamespace) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = response

        async def create(**kwargs: Any) -> SimpleNamespace:
            self.calls.append(kwargs)
            return self._response

        self.messages = SimpleNamespace(create=create)


class TestGenreTagMapper:
    async def test_maps_tags_and_backfills_missing(self, tmp_path: Path) -> None:
        settings = Settings(
            _env_file=None,
            database_path=tmp_path / "db.sqlite3",
            anthropic_api_key="key",
        )
        client = FakeAnthropicClient(
            _fake_claude_response(
                [
                    {
                        "tag": "martial industrial",
                        "buckets": ["industrial"],
                        "broad_genres": ["industrial", "experimental"],
                    },
                    {"tag": "not requested", "buckets": ["noise"], "broad_genres": []},
                ]
            )
        )
        mapper = GenreTagMapper(settings, client=client)
        result = await mapper.map_tags(
            ["martial industrial", "witch house"], ("industrial", "noise")
        )
        assert result["martial industrial"] == (
            ("industrial",),
            ("experimental", "industrial"),
        )
        # Requested but unreturned tags become empty mappings, not retries.
        assert result["witch house"] == ((), ())
        assert "not requested" not in result
        assert client.calls[0]["model"] == "claude-sonnet-5"
        assert client.calls[0]["output_config"]["format"]["type"] == "json_schema"

    async def test_refusal_skips_chunk(self, tmp_path: Path) -> None:
        settings = Settings(
            _env_file=None, database_path=tmp_path / "db.sqlite3", anthropic_api_key="key"
        )
        client = FakeAnthropicClient(
            SimpleNamespace(stop_reason="refusal", content=[])
        )
        mapper = GenreTagMapper(settings, client=client)
        assert await mapper.map_tags(["darkwave"], ("goth",)) == {}

    async def test_without_key_returns_empty(self, settings: Settings) -> None:
        mapper = GenreTagMapper(settings)
        assert await mapper.map_tags(["darkwave"], ("goth",)) == {}


class TestTagStorage:
    async def test_artist_tag_round_trip(self, repository: EventRepository) -> None:
        await repository.store_artist_tags("coil", [("dungeon synth", 100), ("darkwave", 60)])
        await repository.store_artist_tags("swans", [("no wave", 90)])
        freshness = await repository.get_artist_tag_freshness()
        assert set(freshness) == {"coil", "swans"}
        assert freshness["coil"][1] == 2
        assert freshness["swans"][1] == 1
        tags = await repository.get_tags_for_artists({"coil"})
        assert tags == {"dungeon synth", "darkwave"}
        # Re-storing merges: new tags accumulate, existing tags survive.
        await repository.store_artist_tags("coil", [("industrial", 80)])
        assert await repository.get_tags_for_artists({"coil"}) == {
            "dungeon synth",
            "darkwave",
            "industrial",
        }
        freshness = await repository.get_artist_tag_freshness()
        assert freshness["coil"][1] == 3

    async def test_tags_union_across_sources(self, repository: EventRepository) -> None:
        await repository.store_artist_tags("coil", [("darkwave", 60)], "lastfm")
        await repository.store_artist_tags("coil", [("industrial", 5)], "musicbrainz")
        assert await repository.get_tags_for_artists({"coil"}) == {"darkwave", "industrial"}
        assert await repository.get_tags_for_artists({"coil"}, "lastfm") == {"darkwave"}

    async def test_tag_mapping_round_trip(self, repository: EventRepository) -> None:
        await repository.store_tag_mappings(
            {
                "witch house": (("edm & raves", "goth"), ("electronic",)),
                "not a genre": ((), ()),
            },
            model="claude-sonnet-5",
        )
        mappings = await repository.get_tag_mappings({"witch house", "not a genre", "other"})
        assert mappings["witch house"] == (("edm & raves", "goth"), ("electronic",))
        assert mappings["not a genre"] == ((), ())
        assert "other" not in mappings


class TestGenreRoleAliases:
    async def test_aliases_seed_without_overriding_explicit_roles(
        self, repository: EventRepository
    ) -> None:
        await repository.store_tag_mappings(
            {
                "dance electronic": (("edm & raves",), ("electronic",)),
                "witch house": (("edm & raves", "goth"), ()),
                "metal": (("metal",), ()),
            },
            model="test",
        )
        role_map = {"edm & raves": 111, "metal": 222}
        await repository.seed_genre_roles(role_map)
        added = await repository.seed_genre_role_aliases(role_map)
        # "metal" already has an explicit bucket row; "witch house" is
        # ambiguous (two buckets); only "dance electronic" is aliased.
        assert added == 1
        assert await repository.get_role_for_genres(("Dance/Electronic",)) == 111
        assert await repository.get_role_for_genres(("Witch House",)) is None
        assert await repository.get_role_for_genres(("Metal",)) == 222

        # Manual overrides always win over re-seeded aliases.
        await repository.set_genre_role("dance electronic", 999)
        assert await repository.seed_genre_role_aliases(role_map) == 0
        assert await repository.get_role_for_genres(("Dance/Electronic",)) == 999


class StubFetcher:
    source = "lastfm"

    def __init__(self, tags: dict[str, list[tuple[str, int]]]) -> None:
        self.tags = tags
        self.calls: list[str] = []

    async def top_tags(self, artist: str) -> list[tuple[str, int]]:
        self.calls.append(artist)
        return self.tags.get(artist, [])


class StubMapper:
    def __init__(self, mappings: dict[str, tuple[tuple[str, ...], tuple[str, ...]]]) -> None:
        self.mappings = mappings
        self.calls: list[list[str]] = []

    async def map_tags(
        self, tags: list[str], buckets: tuple[str, ...]
    ) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
        self.calls.append(list(tags))
        return {tag: self.mappings.get(tag, ((), ())) for tag in tags}


class TestTasteEnricher:
    async def test_enriches_and_caches(
        self, repository: EventRepository, tmp_path: Path
    ) -> None:
        settings = Settings(
            _env_file=None,
            database_path=tmp_path / "enrich.sqlite3",
            lastfm_api_key="key",
            anthropic_api_key="key",
            genre_role_map='{"Industrial":"1","Goth":"2"}',
        )
        fetcher = StubFetcher({"Coil": [("dungeon synth", 100), ("darkwave", 60)]})
        mapper = StubMapper(
            {
                "dungeon synth": (("goth",), ("ambient", "gothic")),
                "darkwave": (("goth", "industrial"), ("gothic",)),
            }
        )
        enricher = TasteEnricher(settings, repository, fetcher=fetcher, mapper=mapper)
        profile = TasteProfile(artists=("Coil",), genres=("noise",))

        enriched = await enricher.enrich(profile)
        assert enriched.artists == ("Coil",)
        # Niche tags are strong evidence; bucket names and upward-mapped broad
        # genres are weak evidence.
        assert set(enriched.genres) == {"noise", "dungeon synth", "darkwave"}
        assert set(enriched.weak_genres) == {"goth", "industrial", "ambient", "gothic"}
        assert fetcher.calls == ["Coil"]
        # One mapping call covering the artist tags plus the broad vocabulary
        # (mapped once so genre->role aliases can be seeded).
        assert len(mapper.calls) == 1
        assert {"darkwave", "dungeon synth"} <= set(mapper.calls[0])

        # Second run is fully served by the SQLite caches.
        again = await enricher.enrich(profile)
        assert set(again.genres) == set(enriched.genres)
        assert set(again.weak_genres) == set(enriched.weak_genres)
        assert fetcher.calls == ["Coil"]
        assert len(mapper.calls) == 1

    async def test_tags_survive_without_anthropic_key(
        self, repository: EventRepository, tmp_path: Path
    ) -> None:
        settings = Settings(
            _env_file=None,
            database_path=tmp_path / "enrich2.sqlite3",
            lastfm_api_key="key",
        )
        fetcher = StubFetcher({"Swans": [("no wave", 90)]})
        mapper = StubMapper({})
        enricher = TasteEnricher(settings, repository, fetcher=fetcher, mapper=mapper)
        enriched = await enricher.enrich(TasteProfile(artists=("Swans",)))
        assert "no wave" in enriched.genres
        assert mapper.calls == []

    async def test_no_artists_short_circuits(
        self, repository: EventRepository, settings: Settings
    ) -> None:
        enricher = TasteEnricher(settings, repository)
        profile = TasteProfile()
        assert await enricher.enrich(profile) is profile

    async def test_umbrella_tags_become_weak_and_manual_genres_stay_strong(
        self, repository: EventRepository, tmp_path: Path
    ) -> None:
        settings = Settings(
            _env_file=None,
            database_path=tmp_path / "tiers.sqlite3",
            lastfm_api_key="key",
            anthropic_api_key="key",
            genre_role_map='{"Metal":"1"}',
        )
        fetcher = StubFetcher({"Boris": [("drone metal", 90), ("rock", 70)]})
        mapper = StubMapper({"drone metal": (("metal",), ("doom metal", "rock"))})
        enricher = TasteEnricher(settings, repository, fetcher=fetcher, mapper=mapper)
        profile = TasteProfile(artists=("Boris",), genres=("doom metal",))

        enriched = await enricher.enrich(profile)
        # "rock" is an umbrella tag → weak; "drone metal" is niche → strong;
        # manual "doom metal" stays strong and is removed from the weak tier
        # even though the mapping also produced it.
        assert set(enriched.genres) == {"doom metal", "drone metal"}
        assert set(enriched.weak_genres) == {"metal", "rock"}

    @staticmethod
    async def _backdate_fetch(repository: EventRepository, artist_normalized: str) -> None:
        async with repository.database.connect() as connection:
            await connection.execute(
                "UPDATE artist_tag_fetches SET fetched_at = ? WHERE artist_normalized = ?",
                ("2020-01-01T00:00:00+00:00", artist_normalized),
            )
            await connection.commit()

    async def test_zero_cache_days_never_refetches(
        self, repository: EventRepository, tmp_path: Path
    ) -> None:
        settings = Settings(
            _env_file=None,
            database_path=tmp_path / "keep.sqlite3",
            lastfm_api_key="key",
            lastfm_tag_cache_days=0,
        )
        await repository.store_artist_tags("coil", [("darkwave", 60)])
        await self._backdate_fetch(repository, "coil")
        fetcher = StubFetcher({"Coil": [("industrial", 80)]})
        enricher = TasteEnricher(settings, repository, fetcher=fetcher, mapper=StubMapper({}))
        enriched = await enricher.enrich(TasteProfile(artists=("Coil",)))
        assert fetcher.calls == []
        assert "darkwave" in enriched.genres

    async def test_refresh_merges_new_tags_with_old(
        self, repository: EventRepository, tmp_path: Path
    ) -> None:
        settings = Settings(
            _env_file=None,
            database_path=tmp_path / "refresh.sqlite3",
            lastfm_api_key="key",
            lastfm_tag_cache_days=30,
        )
        await repository.store_artist_tags("coil", [("darkwave", 60)])
        await self._backdate_fetch(repository, "coil")
        fetcher = StubFetcher({"Coil": [("industrial", 80)]})
        enricher = TasteEnricher(settings, repository, fetcher=fetcher, mapper=StubMapper({}))
        enriched = await enricher.enrich(TasteProfile(artists=("Coil",)))
        assert fetcher.calls == ["Coil"]
        # The artist's style evolved: the new tag arrives, the old one stays.
        assert "industrial" in enriched.genres
        assert "darkwave" in enriched.genres

    async def test_refresh_never_wipes_tags_with_empty_result(
        self, repository: EventRepository, tmp_path: Path
    ) -> None:
        settings = Settings(
            _env_file=None,
            database_path=tmp_path / "guard.sqlite3",
            lastfm_api_key="key",
            lastfm_tag_cache_days=30,
        )
        await repository.store_artist_tags("coil", [("darkwave", 60)])
        await self._backdate_fetch(repository, "coil")
        fetcher = StubFetcher({})  # Last.fm suddenly returns nothing for Coil
        enricher = TasteEnricher(settings, repository, fetcher=fetcher, mapper=StubMapper({}))
        enriched = await enricher.enrich(TasteProfile(artists=("Coil",)))
        assert fetcher.calls == ["Coil"]
        assert "darkwave" in enriched.genres
