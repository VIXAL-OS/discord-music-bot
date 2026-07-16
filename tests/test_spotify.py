from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

import pytest

from music_event_bot import cli
from music_event_bot.config import Settings
from music_event_bot.taste.spotify import (
    SpotifyTasteImporter,
    _callback_handler,
    _CallbackResult,
    _parse_loopback_redirect,
    _wait_for_callback,
    authorize_spotify,
)


def _request(url: str) -> int:
    try:
        with urlopen(url, timeout=3) as response:  # noqa: S310
            return response.status
    except HTTPError as exc:
        return exc.code


def _callback_url(server, **parameters: str | list[str]) -> str:
    query = urlencode(parameters, doseq=True)
    return f"http://127.0.0.1:{server.server_port}/callback?{query}"


def _available_port() -> int:
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
    finally:
        listener.close()


def test_parse_loopback_redirect_requires_explicit_ipv4_loopback() -> None:
    assert _parse_loopback_redirect("http://127.0.0.1:8888/callback") == (
        "127.0.0.1",
        8888,
        "/callback",
    )

    invalid = (
        "http://localhost:8888/callback",
        "https://127.0.0.1:8888/callback",
        "http://0.0.0.0:8888/callback",
        "http://127.0.0.1/callback",
        "http://127.0.0.1:8888/callback?extra=true",
    )
    for redirect_uri in invalid:
        with pytest.raises(ValueError, match="HTTP loopback URI"):
            _parse_loopback_redirect(redirect_uri)


def test_callback_server_returns_authorization_code() -> None:
    from http.server import HTTPServer

    result = _CallbackResult()
    server = HTTPServer(
        ("127.0.0.1", 0),
        _callback_handler("/callback", "expected-state", result),
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            request = executor.submit(
                _request,
                _callback_url(server, state="expected-state", code="authorization-code"),
            )
            assert _wait_for_callback(server, result, 2) == "authorization-code"
            assert request.result() == 200
    finally:
        server.server_close()


@pytest.mark.parametrize(
    ("parameters", "message"),
    [
        ({"code": "code"}, "state did not match"),
        ({"state": "wrong", "code": "code"}, "state did not match"),
        (
            {"state": ["expected-state", "expected-state"], "code": "code"},
            "state did not match",
        ),
        ({"state": "expected-state", "error": "access_denied"}, "was denied"),
        ({"state": "expected-state"}, "did not contain one authorization code"),
        (
            {"state": "expected-state", "code": ["first", "second"]},
            "did not contain one authorization code",
        ),
    ],
)
def test_callback_server_rejects_invalid_responses(
    parameters: dict[str, str | list[str]], message: str
) -> None:
    from http.server import HTTPServer

    result = _CallbackResult()
    server = HTTPServer(
        ("127.0.0.1", 0),
        _callback_handler("/callback", "expected-state", result),
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            request = executor.submit(_request, _callback_url(server, **parameters))
            with pytest.raises(ValueError, match=message):
                _wait_for_callback(server, result, 2)
            assert request.result() == 400
    finally:
        server.server_close()


def test_callback_server_ignores_wrong_path_then_accepts_callback() -> None:
    from http.server import HTTPServer

    result = _CallbackResult()
    server = HTTPServer(
        ("127.0.0.1", 0),
        _callback_handler("/callback", "expected-state", result),
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            wrong_path = executor.submit(
                _request,
                f"http://127.0.0.1:{server.server_port}/wrong-path",
            )
            server.timeout = 2
            server.handle_request()
            assert wrong_path.result() == 404
            assert result == _CallbackResult()

            valid_callback = executor.submit(
                _request,
                _callback_url(server, state="expected-state", code="authorization-code"),
            )
            assert _wait_for_callback(server, result, 2) == "authorization-code"
            assert valid_callback.result() == 200
    finally:
        server.server_close()


def test_callback_server_times_out() -> None:
    from http.server import HTTPServer

    result = _CallbackResult()
    server = HTTPServer(
        ("127.0.0.1", 0),
        _callback_handler("/callback", "expected-state", result),
    )
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            _wait_for_callback(server, result, 1)
    finally:
        server.server_close()


@pytest.mark.parametrize("open_browser", [False, True])
def test_authorize_spotify_uses_state_memory_cache_and_browser_option(
    monkeypatch, tmp_path, open_browser: bool
) -> None:
    import spotipy.cache_handler
    import spotipy.oauth2

    callback_threads = []
    oauth_arguments = {}
    browser_urls = []

    class FakeMemoryCacheHandler:
        pass

    class FakeSpotifyOAuth:
        def __init__(self, **kwargs) -> None:
            oauth_arguments.update(kwargs)
            self.redirect_uri = kwargs["redirect_uri"]

        def get_authorize_url(self, state: str) -> str:
            def send_callback() -> None:
                _request(
                    f"{self.redirect_uri}?"
                    + urlencode({"state": state, "code": "authorization-code"})
                )

            executor = ThreadPoolExecutor(max_workers=1)
            callback_threads.append((executor, executor.submit(send_callback)))
            return "https://accounts.spotify.test/authorize"

        def get_access_token(self, code: str, check_cache: bool) -> dict[str, str]:
            assert code == "authorization-code"
            assert check_cache is False
            return {"refresh_token": "refresh-token"}

    monkeypatch.setattr(spotipy.cache_handler, "MemoryCacheHandler", FakeMemoryCacheHandler)
    monkeypatch.setattr(spotipy.oauth2, "SpotifyOAuth", FakeSpotifyOAuth)
    monkeypatch.setattr(
        "music_event_bot.taste.spotify.webbrowser.open",
        lambda url: browser_urls.append(url),
    )
    monkeypatch.chdir(tmp_path)
    port = _available_port()
    settings = Settings(
        _env_file=None,
        spotify_client_id="client-id",
        spotify_client_secret="client-secret",
        spotify_redirect_uri=f"http://127.0.0.1:{port}/callback",
    )

    try:
        assert (
            authorize_spotify(settings, timeout_seconds=2, open_browser=open_browser)
            == "refresh-token"
        )
    finally:
        for executor, future in callback_threads:
            future.result(timeout=3)
            executor.shutdown()

    assert oauth_arguments["scope"] == "user-top-read user-library-read"
    assert isinstance(oauth_arguments["cache_handler"], FakeMemoryCacheHandler)
    assert oauth_arguments["state"]
    assert browser_urls == (
        ["https://accounts.spotify.test/authorize"] if open_browser else []
    )
    assert not (tmp_path / ".cache").exists()

    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", port))
    finally:
        listener.close()


def test_authorize_spotify_reports_occupied_callback_port() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    settings = Settings(
        _env_file=None,
        spotify_client_id="client-id",
        spotify_client_secret="client-secret",
        spotify_redirect_uri=f"http://127.0.0.1:{port}/callback",
    )
    try:
        with pytest.raises(RuntimeError, match="Could not listen for Spotify"):
            authorize_spotify(settings, timeout_seconds=1, open_browser=False)
    finally:
        listener.close()


def test_authorize_spotify_closes_server_after_timeout(monkeypatch) -> None:
    import spotipy.cache_handler
    import spotipy.oauth2

    class FakeMemoryCacheHandler:
        pass

    class FakeSpotifyOAuth:
        def __init__(self, **kwargs) -> None:
            pass

        def get_authorize_url(self, state: str) -> str:
            return "https://accounts.spotify.test/authorize"

    monkeypatch.setattr(spotipy.cache_handler, "MemoryCacheHandler", FakeMemoryCacheHandler)
    monkeypatch.setattr(spotipy.oauth2, "SpotifyOAuth", FakeSpotifyOAuth)
    port = _available_port()
    settings = Settings(
        _env_file=None,
        spotify_client_id="client-id",
        spotify_client_secret="client-secret",
        spotify_redirect_uri=f"http://127.0.0.1:{port}/callback",
    )

    with pytest.raises(RuntimeError, match="timed out"):
        authorize_spotify(settings, timeout_seconds=1, open_browser=False)

    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", port))
    finally:
        listener.close()


def test_spotify_taste_imports_and_deduplicates_artists_and_genres(monkeypatch) -> None:
    import spotipy
    import spotipy.cache_handler
    import spotipy.oauth2

    oauth_arguments = {}
    pages = iter(
        [
            {"items": [{"name": "Artist A", "genres": ["ambient", "electronic"]}]},
            {"items": [{"name": "Artist B", "genres": []}]},
            {"items": [{"name": "Artist A", "genres": ["ambient", "post-rock"]}]},
        ]
    )

    class FakeMemoryCacheHandler:
        pass

    class FakeSpotifyOAuth:
        def __init__(self, **kwargs) -> None:
            oauth_arguments.update(kwargs)

        def refresh_access_token(self, refresh_token: str) -> dict[str, str]:
            assert refresh_token == "refresh-token"
            return {"access_token": "access-token"}

    class FakeSpotify:
        def __init__(self, auth: str, **kwargs) -> None:
            assert auth == "access-token"
            assert kwargs.get("retries") == 0

        def current_user_top_artists(self, *, limit: int, time_range: str):
            assert limit == 50
            assert time_range in {"short_term", "medium_term", "long_term"}
            return next(pages)

        def current_user_top_tracks(self, *, limit: int, time_range: str):
            assert limit == 50
            assert time_range in {"short_term", "medium_term", "long_term"}
            return {"items": []}

        def current_user_saved_tracks(self, *, limit: int, offset: int):
            assert limit == 50
            return {"items": [], "next": None}

    monkeypatch.setattr(spotipy.cache_handler, "MemoryCacheHandler", FakeMemoryCacheHandler)
    monkeypatch.setattr(spotipy.oauth2, "SpotifyOAuth", FakeSpotifyOAuth)
    monkeypatch.setattr(spotipy, "Spotify", FakeSpotify)
    settings = Settings(
        _env_file=None,
        spotify_client_id="client-id",
        spotify_client_secret="client-secret",
        spotify_refresh_token="refresh-token",
    )

    profile = SpotifyTasteImporter(settings)._import_sync()

    assert profile.artists == ("Artist A", "Artist B")
    assert profile.genres == ("ambient", "electronic", "post-rock")
    assert oauth_arguments["scope"] == "user-top-read user-library-read"
    assert isinstance(oauth_arguments["cache_handler"], FakeMemoryCacheHandler)


@pytest.mark.asyncio
async def test_spotify_auth_cli_writes_token_without_printing_it(
    monkeypatch, tmp_path, capsys
) -> None:
    captured = {}
    settings = Settings(_env_file=None)

    def fake_authorize(
        received_settings: Settings,
        *,
        timeout_seconds: int,
        open_browser: bool,
    ) -> str:
        captured.update(
            settings=received_settings,
            timeout_seconds=timeout_seconds,
            open_browser=open_browser,
        )
        return "secret-refresh-token"

    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "authorize_spotify", fake_authorize)
    output_file = tmp_path / "spotify-token.txt"
    args = SimpleNamespace(
        command="spotify-auth",
        timeout_seconds=17,
        no_browser=True,
        output_file=output_file,
    )

    await cli._run(args)

    assert output_file.read_text(encoding="utf-8") == "secret-refresh-token\n"
    assert captured == {
        "settings": settings,
        "timeout_seconds": 17,
        "open_browser": False,
    }
    assert "secret-refresh-token" not in capsys.readouterr().out


def test_spotify_auth_parser_accepts_callback_options() -> None:
    args = cli._parser().parse_args(
        [
            "spotify-auth",
            "--timeout-seconds",
            "45",
            "--no-browser",
            "--output-file",
            ".spotify_refresh_token",
        ]
    )

    assert args.timeout_seconds == 45
    assert args.no_browser is True
    assert args.output_file == Path(".spotify_refresh_token")

    with pytest.raises(SystemExit):
        cli._parser().parse_args(["spotify-auth", "--timeout-seconds", "0"])
