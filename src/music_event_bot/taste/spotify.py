from __future__ import annotations

import asyncio
import logging
import secrets
import time
import webbrowser
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from music_event_bot.config import Settings
from music_event_bot.domain.models import TasteProfile

_SPOTIFY_SCOPE = "user-top-read user-library-read"

logger = logging.getLogger(__name__)


def _primary_artist_name(track: dict[str, Any]) -> str | None:
    track_artists = track.get("artists") or []
    if track_artists and isinstance(track_artists[0], dict):
        name = track_artists[0].get("name")
        if name:
            return str(name)
    return None


def _liked_song_artists(client: Any, min_tracks: int) -> set[str]:
    counts: Counter[str] = Counter()
    display: dict[str, str] = {}
    offset = 0
    while True:
        page = client.current_user_saved_tracks(limit=50, offset=offset)
        items = page.get("items", [])
        if not items:
            break
        for item in items:
            track = item.get("track") or {}
            name = _primary_artist_name(track)
            if name:
                key = name.casefold()
                counts[key] += 1
                display.setdefault(key, name)
        offset += len(items)
        if page.get("next") is None:
            break
    return {display[key] for key, count in counts.items() if count >= min_tracks}


def build_profile_from_spotify(client: Any, *, min_liked_tracks: int) -> TasteProfile:
    from spotipy.exceptions import SpotifyException

    artists: set[str] = set()
    genres: set[str] = set()
    for time_range in ("short_term", "medium_term", "long_term"):
        artist_page = client.current_user_top_artists(limit=50, time_range=time_range)
        for artist in artist_page.get("items", []):
            artists.add(artist["name"])
            genres.update(artist.get("genres", []))
        track_page = client.current_user_top_tracks(limit=50, time_range=time_range)
        for track in track_page.get("items", []):
            name = _primary_artist_name(track)
            if name:
                artists.add(name)
    try:
        liked = _liked_song_artists(client, min_liked_tracks)
    except SpotifyException as exc:
        if exc.http_status in (401, 403):
            logger.warning(
                "Spotify denied access to Liked Songs (HTTP %s). The stored refresh token "
                "predates the user-library-read scope; re-run `music-event-bot spotify-auth` "
                "and update MUSICBOT_SPOTIFY_REFRESH_TOKEN. Continuing with top artists and "
                "top tracks only.",
                exc.http_status,
            )
        else:
            raise
    else:
        artists.update(liked)
        logger.info(
            "Spotify liked-songs import found %d artists with at least %d saved tracks",
            len(liked),
            min_liked_tracks,
        )
    return TasteProfile(artists=tuple(sorted(artists)), genres=tuple(sorted(genres)))


class SpotifyTasteImporter:
    """Import the Spotify taste profile, backed by the taste_preferences cache.

    A full import walks the entire Liked Songs library (potentially hundreds
    of requests), so imported artists/genres are persisted and reused for
    ``spotify_refresh_hours`` between imports. Persisted values are merge-only
    so past favorites are never forgotten, and any import failure — including
    rate limiting — falls back to the cache instead of blocking startup.
    """

    def __init__(self, settings: Settings, repository: Any | None = None) -> None:
        self.settings = settings
        self.repository = repository

    async def import_profile(self) -> TasteProfile:
        from requests import RequestException
        from spotipy.exceptions import SpotifyException

        if not self.settings.spotify_configured:
            return TasteProfile()
        stored_artists: tuple[str, ...] = ()
        stored_genres: tuple[str, ...] = ()
        if self.repository is not None:
            stored_artists = await self.repository.get_taste_preferences("artist", "spotify")
            stored_genres = await self.repository.get_taste_preferences("genre", "spotify")
            last_import = await self.repository.latest_taste_preference_update("spotify")
            refresh_hours = self.settings.spotify_refresh_hours
            if (
                last_import is not None
                and refresh_hours > 0
                and datetime.now(UTC) - last_import < timedelta(hours=refresh_hours)
            ):
                return TasteProfile(artists=stored_artists, genres=stored_genres)
        try:
            imported = await asyncio.to_thread(self._import_sync)
        except (SpotifyException, RequestException) as exc:
            logger.warning(
                "Spotify import failed (%s); continuing with %d cached artists",
                exc,
                len(stored_artists),
            )
            return TasteProfile(artists=stored_artists, genres=stored_genres)
        if self.repository is not None:
            await self.repository.store_taste_preferences(
                "artist", set(imported.artists), "spotify"
            )
            await self.repository.store_taste_preferences(
                "genre", set(imported.genres), "spotify"
            )
        return TasteProfile(
            artists=tuple(sorted(set(stored_artists) | set(imported.artists))),
            genres=tuple(sorted(set(stored_genres) | set(imported.genres))),
        )

    def _import_sync(self) -> TasteProfile:
        import spotipy
        from spotipy.cache_handler import MemoryCacheHandler
        from spotipy.oauth2 import SpotifyOAuth

        client_secret = self.settings.spotify_client_secret
        refresh_token = self.settings.spotify_refresh_token
        if client_secret is None or refresh_token is None or not self.settings.spotify_client_id:
            return TasteProfile()
        oauth = SpotifyOAuth(
            client_id=self.settings.spotify_client_id,
            client_secret=client_secret.get_secret_value(),
            redirect_uri=self.settings.spotify_redirect_uri,
            scope=_SPOTIFY_SCOPE,
            open_browser=False,
            cache_handler=MemoryCacheHandler(),
        )
        token_info = oauth.refresh_access_token(refresh_token.get_secret_value())
        # No retries: a rate-limited request must fail fast so the caller can
        # fall back to the cached profile, not sleep out the Retry-After
        # header in-process (Spotify has sent waits of a day or more).
        client = spotipy.Spotify(
            auth=token_info["access_token"],
            requests_timeout=15,
            retries=0,
            status_retries=0,
        )
        return build_profile_from_spotify(
            client,
            min_liked_tracks=self.settings.spotify_liked_artist_min_tracks,
        )


@dataclass(slots=True)
class _CallbackResult:
    code: str | None = None
    error: str | None = None


def _parse_loopback_redirect(redirect_uri: str) -> tuple[str, int, str]:
    parsed = urlsplit(redirect_uri)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Spotify redirect URI has an invalid port") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
        or not parsed.path.startswith("/")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "MUSICBOT_SPOTIFY_REDIRECT_URI must be an HTTP loopback URI such as "
            "http://127.0.0.1:8888/callback; register it exactly in Spotify"
        )
    return "127.0.0.1", port, parsed.path


def _callback_handler(
    expected_path: str,
    expected_state: str,
    result: _CallbackResult,
) -> type[BaseHTTPRequestHandler]:
    class SpotifyCallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            if parsed.path != expected_path:
                self._respond(404, "Not found", "This is not the Spotify callback path.")
                return

            parameters = parse_qs(parsed.query, keep_blank_values=True)
            states = parameters.get("state", [])
            if (
                len(states) != 1
                or not states[0]
                or not secrets.compare_digest(states[0], expected_state)
            ):
                result.error = "Spotify callback state did not match; authorization was cancelled"
                self._respond(
                    400,
                    "Authorization cancelled",
                    "The Spotify authorization state was invalid. Return to the terminal.",
                )
                return

            errors = parameters.get("error", [])
            if errors:
                result.error = "Spotify authorization was denied"
                self._respond(
                    400,
                    "Authorization denied",
                    "Spotify authorization was denied. Return to the terminal.",
                )
                return

            codes = parameters.get("code", [])
            if len(codes) != 1 or not codes[0]:
                result.error = "Spotify callback did not contain one authorization code"
                self._respond(
                    400,
                    "Authorization failed",
                    "Spotify did not provide an authorization code. Return to the terminal.",
                )
                return

            result.code = codes[0]
            self._respond(
                200,
                "Authorization complete",
                "Spotify authorization is complete. You may close this tab.",
            )

        def _respond(self, status: int, title: str, message: str) -> None:
            body = (
                "<!doctype html><html><head><meta charset='utf-8'>"
                f"<title>{title}</title></head><body><h1>{title}</h1>"
                f"<p>{message}</p></body></html>"
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return SpotifyCallbackHandler


def _wait_for_callback(
    server: HTTPServer,
    result: _CallbackResult,
    timeout_seconds: int,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    while result.code is None and result.error is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                f"Spotify authorization timed out after {timeout_seconds} seconds"
            )
        server.timeout = min(0.5, remaining)
        server.handle_request()
    if result.error is not None:
        raise ValueError(result.error)
    if result.code is None:
        raise RuntimeError("Spotify authorization ended without a code")
    return result.code


def authorize_spotify(
    settings: Settings,
    *,
    timeout_seconds: int = 300,
    open_browser: bool = True,
) -> str:
    from spotipy.cache_handler import MemoryCacheHandler
    from spotipy.oauth2 import SpotifyOAuth

    if not settings.spotify_client_id or not settings.spotify_client_secret:
        raise ValueError(
            "Set MUSICBOT_SPOTIFY_CLIENT_ID and MUSICBOT_SPOTIFY_CLIENT_SECRET first"
        )
    if timeout_seconds <= 0:
        raise ValueError("Spotify authorization timeout must be greater than zero")

    host, port, callback_path = _parse_loopback_redirect(
        settings.spotify_redirect_uri
    )
    state = secrets.token_urlsafe(32)
    result = _CallbackResult()
    handler = _callback_handler(callback_path, state, result)
    try:
        server = HTTPServer((host, port), handler)
    except OSError as exc:
        raise RuntimeError(
            f"Could not listen for Spotify on {host}:{port}; close the program using "
            "that port or change the registered redirect URI"
        ) from exc

    try:
        oauth = SpotifyOAuth(
            client_id=settings.spotify_client_id,
            client_secret=settings.spotify_client_secret.get_secret_value(),
            redirect_uri=settings.spotify_redirect_uri,
            state=state,
            scope=_SPOTIFY_SCOPE,
            open_browser=False,
            cache_handler=MemoryCacheHandler(),
        )
        authorization_url = oauth.get_authorize_url(state=state)
        print("Open this URL in a browser and approve access:\n")
        print(authorization_url)
        if open_browser:
            try:
                webbrowser.open(authorization_url)
            except (OSError, webbrowser.Error):
                pass

        code = _wait_for_callback(server, result, timeout_seconds)
        token_info = oauth.get_access_token(code, check_cache=False)
    finally:
        server.server_close()

    refresh_token = token_info.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("Spotify did not return a refresh token")
    return str(refresh_token)
