from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from music_event_bot.app import Application
from music_event_bot.config import Settings
from music_event_bot.discovery.images import is_publishable_image
from music_event_bot.domain.models import EventStatus
from music_event_bot.services.dedupe import DEFAULT_WINDOW_MINUTES
from music_event_bot.storage.database import Database
from music_event_bot.taste.spotify import authorize_spotify


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


async def _verify_image_renders(url: str) -> None:
    """Reject artwork Discord would render as a blank embed.

    Mirrors ArtworkResolver._renders: some hosts serve flyers as
    application/octet-stream, which returns 200 but embeds as nothing.
    """
    import httpx

    from music_event_bot.discovery.artwork import _BROWSER_UA

    async with httpx.AsyncClient(
        follow_redirects=True, timeout=20.0, headers={"User-Agent": _BROWSER_UA}
    ) as client:
        try:
            response = await client.get(url, headers={"Range": "bytes=0-1023"})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ValueError(f"Could not fetch {url}: {exc}") from None
        content_type = response.headers.get("content-type", "")
        if not content_type.lower().startswith("image/"):
            raise ValueError(
                f"Refusing {url}: content-type is {content_type!r}, not an image type"
            )


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
    events_parser.add_argument(
        "--venue",
        help="Only events at this venue, matched through config/venue-aliases.json",
    )
    events_parser.add_argument(
        "--on",
        dest="on_date",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="Only events on this local calendar night at the venue's own timezone",
    )
    subparsers.add_parser("scrape-once", help="Run all enabled discovery sources once")
    backfill_parser = subparsers.add_parser(
        "backfill-artwork",
        help="Find artwork for stored events that have none (reports without --apply)",
    )
    backfill_parser.add_argument(
        "--status",
        action="append",
        choices=[status.value for status in EventStatus],
        help="Limit to these statuses; repeat to include several "
        "(default: published, approved, pending_review, incomplete, publish_failed)",
    )
    backfill_parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the images found; without it nothing is modified",
    )
    image_parser = subparsers.add_parser(
        "set-image",
        help="Replace one event's artwork; edits the announcement in place when published",
    )
    image_parser.add_argument("event_id", help="Event id from `events`")
    image_parser.add_argument("url", help="Image URL to store")
    image_parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip the content-type fetch; URL-level placeholder rules still apply",
    )
    dedupe_parser = subparsers.add_parser(
        "dedupe",
        help="Repair stale fingerprints and fold duplicate events together "
        "(reports without --apply)",
    )
    dedupe_parser.add_argument(
        "--window-minutes",
        type=_positive_int,
        default=DEFAULT_WINDOW_MINUTES,
        help="How far two starts may differ and still be one show "
        f"(default: {DEFAULT_WINDOW_MINUTES}; an early and a late set are ~150 apart)",
    )
    dedupe_parser.add_argument(
        "--apply",
        action="store_true",
        help="Repair fingerprints and merge duplicates; without it nothing is modified",
    )
    dedupe_parser.add_argument(
        "--include-published",
        action="store_true",
        help="Also merge duplicates that already announced to Discord. The "
        "announcement and scheduled event are NOT removed -- delete those by hand",
    )
    seed_parser = subparsers.add_parser(
        "seed-profiles",
        help="Seed per-user taste profiles from the genre roles members hold",
    )
    seed_parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the profiles. Without it this is a dry run that changes nothing.",
    )
    seed_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicitly request the default dry run",
    )
    seed_parser.add_argument(
        "--metro",
        help="Home metro for newly seeded profiles (default: the metro nearest home)",
    )
    report_parser = subparsers.add_parser(
        "delivery-report",
        help="Replay recent announcements through per-user delivery and report the difference",
    )
    report_parser.add_argument(
        "--days", type=_positive_int, default=14, help="How far back to replay (default 14)"
    )
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
                "personal_delivery": {
                    "mode": settings.personal_delivery,
                    "profiles": len(await app.repository.list_user_profiles()),
                    "default_daily_ping_cap": settings.default_daily_ping_cap,
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
        events = await app.repository.list_events(
            *statuses, venue=args.venue, on_date=args.on_date
        )
        print(json.dumps([asdict(event) for event in events], indent=2, default=str))
        return
    if args.command == "scrape-once":
        summary = await app.discovery.run()
        print(json.dumps(asdict(summary), indent=2, default=str))
        return
    if args.command == "backfill-artwork":
        from music_event_bot.services.artwork_backfill import ArtworkBackfill

        backfill = ArtworkBackfill(app.repository)
        statuses = tuple(EventStatus(value) for value in (args.status or []))
        summary = await backfill.run(
            **({"statuses": statuses} if statuses else {}), apply=args.apply
        )
        print(json.dumps(asdict(summary), indent=2, default=str))
        return

    if args.command == "dedupe":
        from music_event_bot.services.dedupe import DedupeService

        service = DedupeService(app.repository)
        report = await service.scan(window_minutes=args.window_minutes)
        if args.apply:
            await service.repair(report)
            for group in report.groups:
                report.merged += await service.merge(
                    group, include_published=args.include_published
                )
            if report.merged:
                # A repair skipped earlier because its recomputed key collided
                # may be free now: merging deleted the row that owned that key.
                settled = await service.scan(window_minutes=args.window_minutes)
                report.repaired += await service.repair(settled)
        report.skipped_published = sum(
            len(group.blocked_by_publication) for group in report.groups
        ) if not args.include_published else 0
        print(
            json.dumps(
                {
                    "applied": args.apply,
                    "window_minutes": args.window_minutes,
                    "stale_fingerprints": len(report.stale),
                    "stale_colliding": sum(
                        1 for stale in report.stale if stale.collides_with
                    ),
                    "fingerprints_repaired": report.repaired,
                    "stale_venue_keys": len(report.stale_venue_keys),
                    "venue_keys_refreshed": report.venue_keys_refreshed,
                    "duplicate_groups": len(report.groups),
                    "events_merged": report.merged,
                    "skipped_because_published": report.skipped_published,
                    "groups": [
                        {
                            "reason": group.reason,
                            "keep": {
                                "id": group.keeper.event_id,
                                "title": group.keeper.title,
                                "venue": group.keeper.venue,
                                "starts_at": group.keeper.starts_at,
                                "status": group.keeper.status,
                                "sources": list(group.keeper.sources),
                            },
                            "merge": [
                                {
                                    "id": loser.event_id,
                                    "title": loser.title,
                                    "venue": loser.venue,
                                    "starts_at": loser.starts_at,
                                    "status": loser.status,
                                    "sources": list(loser.sources),
                                    "announcement_message_id": loser.announcement_message_id,
                                    "scheduled_event_id": loser.scheduled_event_id,
                                    "needs_manual_discord_cleanup": loser.is_public,
                                }
                                for loser in group.losers
                            ],
                        }
                        for group in report.groups
                    ],
                },
                indent=2,
                default=str,
            )
        )
        return

    if args.command == "set-image":
        event = await app.repository.get_event(args.event_id)
        if event is None:
            raise ValueError(f"No such event: {args.event_id}")
        if not is_publishable_image(args.url):
            raise ValueError(
                f"Refusing {args.url}: matches a placeholder or unrenderable-suffix rule"
            )
        if not args.skip_verify:
            await _verify_image_renders(args.url)
        previous = event.image_url
        await app.repository.update_event(event.id, image_url=args.url)
        result = {
            "event_id": event.id,
            "title": event.title,
            "status": event.status.value,
            "previous_image_url": previous,
            "image_url": args.url,
            "announcement_updated": False,
        }
        if event.status is EventStatus.PUBLISHED:
            settings.require_discord()
            from music_event_bot.discord.bot import MusicEventDiscordBot

            bot = MusicEventDiscordBot(app)
            await bot.run_once(
                lambda: bot.publication_service.update_existing(event.id),
                "artwork update",
            )
            result["announcement_updated"] = True
        print(json.dumps(result, indent=2, default=str))
        return

    if args.command == "delivery-report":
        from collections import Counter
        from datetime import timedelta

        from music_event_bot.services.delivery import (
            STRONG_SCORE,
            apply_daily_caps,
            build_profiles,
            match_users,
            roles_for_event,
        )

        profiles = build_profiles(
            await app.repository.list_user_profiles(),
            await app.repository.list_user_taste("genre"),
            settings.bucket_role_map,
            await app.repository.list_user_taste_weighted("artist"),
            await app.repository.list_user_taste_weighted("venue"),
        )
        if not profiles:
            raise ValueError("No profiles to report on; run seed-profiles --apply first")
        fallback = frozenset(
            role_id
            for genre, role_id in settings.role_map.items()
            if genre.startswith("other")
        )
        since = datetime.now(UTC) - timedelta(days=args.days)
        announced = await app.repository.list_announced_events(since)
        sequence = [
            (
                announced_at,
                match_users(
                    event,
                    await roles_for_event(
                        app.repository, event, fallback_role_ids=fallback
                    ),
                    profiles,
                ),
            )
            for event, announced_at in announced
        ]
        capped = apply_daily_caps(
            sequence, profiles, settings.timezone, settings.reserved_ping_slots
        )

        # Three numbers per member, so the reduction is attributable: taste
        # alone is what their roles already gave them, then the metro, then
        # the cap.
        taste = Counter[int]()
        in_band = Counter[int]()
        pinged = Counter[int]()
        strong = Counter[int]()
        busiest: dict[int, Counter[str]] = {}
        for announced_at, matches in capped:
            day = announced_at.astimezone(settings.timezone).date().isoformat()
            for match in matches:
                taste[match.user_id] += 1
                if match.within_band:
                    in_band[match.user_id] += 1
                    if match.score >= STRONG_SCORE:
                        strong[match.user_id] += 1
                    # Counted before the cap on purpose: capped days all look
                    # identical, so only the uncapped load says whether the
                    # cap binds and how much lands in the catch-up post.
                    busiest.setdefault(match.user_id, Counter())[day] += 1
                if match.pinged:
                    pinged[match.user_id] += 1
        days = max(1, args.days)
        rows = []
        for user_id, profile in profiles.items():
            peak = busiest.get(user_id, Counter()).most_common(1)
            rows.append(
                {
                    "member": profile.display_name,
                    "metro": profile.metro,
                    "travel_band": profile.travel_band,
                    "daily_cap": profile.daily_ping_cap,
                    "role_pings_today": round(taste[user_id] / days, 1),
                    "after_metro_today": round(in_band[user_id] / days, 1),
                    "after_cap_today": round(pinged[user_id] / days, 1),
                    "queued_total": in_band[user_id] - pinged[user_id],
                    "followed_acts": len(profile.artists),
                    "strong_matches": strong[user_id],
                    "busiest_day": peak[0][1] if peak else 0,
                }
            )
        rows.sort(key=lambda row: -float(row["role_pings_today"]))
        print(
            json.dumps(
                {
                    "window_days": args.days,
                    "announcements_replayed": len(announced),
                    "announcements_per_day": round(len(announced) / days, 1),
                    "busiest_announcement_day": max(
                        (
                            Counter(
                                at.astimezone(settings.timezone).date().isoformat()
                                for at, _matches in capped
                            ).values()
                        ),
                        default=0,
                    ),
                    "events_matching_nobody": sum(
                        1 for _at, matches in capped if not any(m.within_band for m in matches)
                    ),
                    "members": rows,
                },
                indent=2,
                default=str,
            )
        )
        return

    settings.require_discord()
    from music_event_bot.discord.bot import MusicEventDiscordBot

    bot = MusicEventDiscordBot(app)
    if args.command == "seed-profiles":
        if args.apply and args.dry_run:
            raise ValueError("--apply and --dry-run are mutually exclusive")
        if not settings.members_intent:
            raise ValueError(
                "seed-profiles needs the Server Members intent. Enable it under "
                "Bot > Privileged Gateway Intents in the Discord Developer Portal, "
                "then set MUSICBOT_MEMBERS_INTENT=true."
            )
        from music_event_bot.domain.metros import metro as lookup_metro
        from music_event_bot.domain.metros import nearest_metro
        from music_event_bot.domain.normalization import normalize_text
        from music_event_bot.services.profiles import (
            apply_role_seed,
            bucket_genre_roles,
            plan_role_seed,
            summarize,
        )

        home_metro = (
            lookup_metro(args.metro) if args.metro else nearest_metro(settings.home_point)
        )
        members: list[Any] = []

        async def _collect() -> None:
            members.extend(await bot.collect_guild_members())

        await bot.run_once(_collect, "member collection")

        # Only the configured buckets, not the hundreds of tag aliases
        # seed_genre_role_aliases derives into genre_roles on every startup.
        # Those aliases are how an event's genres are *recognised*, and they
        # improve over time; freezing a snapshot of them into a member's
        # profile would both be unreadable and cut that member off from every
        # later improvement. Matching resolves event genres through the live
        # alias table instead.
        role_genres = bucket_genre_roles(
            await app.repository.list_genre_roles(),
            {normalize_text(genre) for genre in settings.role_map},
        )
        actions = plan_role_seed(
            members,
            role_genres,
            await app.repository.list_user_profiles(),
            await app.repository.list_user_taste("genre"),
            default_metro=home_metro.key,
        )
        seed_report: dict[str, Any] = {
            "applied": bool(args.apply),
            "members_scanned": len(members),
            "genre_roles": len(role_genres),
            "default_metro": home_metro.key,
            "counts": summarize(actions),
            # Named members, not just counts: the point of the dry run is to
            # see whether anyone would end up with no genres at all before
            # per-user delivery makes that mean silence.
            "members": [
                {
                    "user_id": str(action.user_id),
                    "display_name": action.display_name,
                    "action": action.action,
                    "genres": list(action.genres),
                    "adds": list(action.added_genres),
                }
                for action in sorted(actions, key=lambda item: item.display_name.casefold())
            ],
        }
        if args.apply:
            seed_report["written"] = await apply_role_seed(
                app.repository, actions, daily_ping_cap=settings.default_daily_ping_cap
            )
        print(json.dumps(seed_report, indent=2, default=str))
        return
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
