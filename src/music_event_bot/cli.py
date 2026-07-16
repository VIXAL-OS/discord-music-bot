from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from dataclasses import asdict
from pathlib import Path

from music_event_bot.app import Application
from music_event_bot.config import Settings
from music_event_bot.domain.models import EventStatus
from music_event_bot.storage.database import Database
from music_event_bot.taste.spotify import authorize_spotify


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _write_refresh_token(path: Path, refresh_token: str) -> None:
    if not path.parent.exists():
        raise ValueError(f"Output directory does not exist: {path.parent}")
    try:
        with path.open("x", encoding="utf-8") as output:
            output.write(f"{refresh_token}\n")
    except FileExistsError as exc:
        raise ValueError(f"Refusing to overwrite existing file: {path}") from exc
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="music-event-bot")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--log-file",
        type=Path,
        help="Append logs to this UTF-8 file in addition to the console",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("migrate", help="Apply SQLite migrations")
    subparsers.add_parser("health", help="Show configuration and database health")
    events_parser = subparsers.add_parser("events", help="List stored events as JSON")
    events_parser.add_argument(
        "--status",
        action="append",
        choices=[status.value for status in EventStatus],
        help="Filter by lifecycle status; repeat to include multiple statuses",
    )
    subparsers.add_parser("scrape-once", help="Run all enabled discovery sources once")
    subparsers.add_parser("review-sync", help="Connect to Discord and reconcile review cards")
    subparsers.add_parser("bot", help="Run the Discord bot and scheduler")
    spotify_parser = subparsers.add_parser(
        "spotify-auth", help="Obtain a Spotify refresh token"
    )
    spotify_parser.add_argument(
        "--timeout-seconds",
        type=_positive_int,
        default=300,
        help="Seconds to wait for the browser callback (default: 300)",
    )
    spotify_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the authorization URL without opening a browser",
    )
    spotify_parser.add_argument(
        "--output-file",
        type=Path,
        help="Write the refresh token to a new file instead of stdout",
    )
    return parser


async def _run(args: argparse.Namespace) -> None:
    settings = Settings()
    if args.command == "migrate":
        version = await Database(settings.database_path).initialize()
        print(json.dumps({"schema_version": version, "database": str(settings.database_path)}))
        return
    if args.command == "spotify-auth":
        refresh_token = await asyncio.to_thread(
            authorize_spotify,
            settings,
            timeout_seconds=args.timeout_seconds,
            open_browser=not args.no_browser,
        )
        output_file: Path | None = args.output_file
        if output_file is not None:
            await asyncio.to_thread(_write_refresh_token, output_file, refresh_token)
            print(
                "\nSpotify refresh token written to "
                f"{output_file}. Store it as MUSICBOT_SPOTIFY_REFRESH_TOKEN, then delete "
                "the token file."
            )
        else:
            print("\nStore this value as MUSICBOT_SPOTIFY_REFRESH_TOKEN:\n")
            print(refresh_token)
        return

    app = await Application.create(settings)
    if args.command == "health":
        health = await app.repository.health()
        health.update(
            {
                "discord_configured": settings.discord_configured,
                "ticketmaster_configured": settings.ticketmaster_configured,
                "spotify_configured": settings.spotify_configured,
                "lastfm_configured": settings.lastfm_configured,
                "musicbrainz_enabled": settings.musicbrainz_enabled,
                "anthropic_configured": settings.anthropic_configured,
                "sources": [source.name for source in app.sources],
                "travel_region": {
                    "home": {
                        "latitude": settings.home_point.latitude,
                        "longitude": settings.home_point.longitude,
                    },
                    "max_radius_miles": settings.max_travel_radius_miles,
                    "ticketmaster_coverage_cells": len(settings.ticketmaster_cells),
                    "window_days": [
                        settings.discovery_start_offset_days,
                        settings.discovery_horizon_days,
                    ],
                },
                "taste": {
                    "artists": len(app.profile.artists),
                    "genres": len(app.profile.genres),
                    "weak_genres": len(app.profile.weak_genres),
                    "venues": len(app.profile.venues),
                },
            }
        )
        print(json.dumps(health, indent=2, default=str))
        return
    if args.command == "events":
        statuses = tuple(EventStatus(value) for value in (args.status or []))
        events = await app.repository.list_events(*statuses)
        print(json.dumps([asdict(event) for event in events], indent=2, default=str))
        return
    if args.command == "scrape-once":
        summary = await app.discovery.run()
        print(json.dumps(asdict(summary), indent=2, default=str))
        return

    settings.require_discord()
    from music_event_bot.discord.bot import MusicEventDiscordBot

    bot = MusicEventDiscordBot(app)
    if args.command == "review-sync":
        await bot.run_sync_once()
    else:
        token = settings.discord_token
        if token is None:
            raise RuntimeError("Discord token is missing")
        await bot.start(token.get_secret_value())


def main() -> None:
    args = _parser().parse_args()
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file is not None:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(_run(args))
    except (ValueError, RuntimeError) as exc:
        logging.getLogger(__name__).error("%s", exc)
        raise SystemExit(2) from None
