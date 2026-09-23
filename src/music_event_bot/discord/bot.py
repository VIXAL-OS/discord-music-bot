from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime, timedelta
from typing import Any

import discord
from dateutil.parser import parse as parse_datetime
from discord import app_commands
from discord.ext import commands

from music_event_bot.app import Application
from music_event_bot.discord.permissions import require_reviewer
from music_event_bot.discord.publishing import DiscordPublicationGateway
from music_event_bot.discord.review import EditEventModal, EventReviewView, review_embed
from music_event_bot.discord.rsvp import RsvpView
from music_event_bot.discovery.manual import manual_event
from music_event_bot.domain.metros import (
    DEFAULT_TRAVEL_BAND,
    METROS,
    TRAVEL_BANDS,
    metro,
    nearest_metro,
    travel_band,
)
from music_event_bot.domain.models import EventRecord, EventStatus
from music_event_bot.domain.normalization import normalize_text
from music_event_bot.domain.scoring import score_event
from music_event_bot.services.profiles import GuildMember, bucket_genre_roles
from music_event_bot.services.publishing import PublicationService
from music_event_bot.services.request_parser import RequestEventParser
from music_event_bot.services.scheduler import BotScheduler

logger = logging.getLogger(__name__)


_CARD_MARKER_RE = re.compile(r"\[music-event-id:[0-9a-fA-F-]{36}\]")
_URL_RE = re.compile(r"https?://\S+")

# Statuses whose events belong in the review channel. Everything else has
# been decided, so its card gets deleted (deletes are exempt from Discord's
# hourly cap on editing old messages, unlike the final-edit approach).
_REVIEWABLE_STATUSES = frozenset(
    {EventStatus.PENDING_REVIEW, EventStatus.INCOMPLETE, EventStatus.PUBLISH_FAILED}
)


def message_is_orphan_card(
    message: discord.Message, bot_user_id: int, registered_message_ids: frozenset[int]
) -> bool:
    """True for bot-authored review cards whose event registration is gone.

    Purging events deletes their reviews rows but cannot delete Discord
    messages, so re-scrapes leave stale duplicate cards behind. Any card
    carrying the event marker whose message ID is not the canonical one in
    the reviews table is an orphan.
    """
    if message.author.id != bot_user_id:
        return False
    if message.id in registered_message_ids:
        return False
    return any(
        embed.footer and embed.footer.text and _CARD_MARKER_RE.search(embed.footer.text)
        for embed in message.embeds
    )


def chunk_message_lines(lines: list[str], limit: int = 1900) -> list[str]:
    """Pack lines into as few messages as fit under Discord's 2000-char cap."""
    chunks: list[str] = []
    current = ""
    for line in lines:
        line = line[:limit]
        if current and len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def review_card_hash(embed: discord.Embed, has_view: bool) -> str:
    """Fingerprint of the rendered review card.

    Cards are only edited when this changes; unconditional re-edits of every
    posted card each sync cycle drown in Discord's per-channel PATCH limits.
    """
    payload = json.dumps(
        {"embed": embed.to_dict(), "view": has_view}, sort_keys=True, default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class MusicEventDiscordBot(commands.Bot):
    def __init__(self, app: Application) -> None:
        intents = discord.Intents.default()
        # Privileged, enabled in the Developer Portal: lets @mention requests
        # read the surrounding conversation ("event card for this pls?").
        intents.message_content = True
        # Also privileged, and required to read who holds which genre role --
        # without it the roster is empty and seed-profiles has nothing to
        # seed from. The REST member list is gated on the same portal toggle,
        # so there is no way around enabling it there too. Off unless asked
        # for: requesting an intent the portal has not granted makes the
        # gateway refuse the connection outright.
        intents.members = app.settings.members_intent
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.music_app = app
        self.settings = app.settings
        self.repository = app.repository
        self.gateway = DiscordPublicationGateway(self, self.settings)
        fallback_roles = frozenset(
            role_id
            for genre, role_id in self.settings.role_map.items()
            if genre.startswith("other")
        )
        # When nothing matches at all, ping only the music catch-all; the
        # "Other Events" community is for non-music listings that arrive
        # with their own genre labels.
        catchall_roles = (
            frozenset(
                role_id
                for genre, role_id in self.settings.role_map.items()
                if genre == "other music"
            )
            or fallback_roles
        )
        self.publication_service = PublicationService(
            self.repository,
            self.gateway,
            fallback_role_ids=fallback_roles,
            catchall_role_ids=catchall_roles,
            # Last gate before the community sees anything: an event approved
            # before its act joined the blocklist must not go out.
            blocklist=app.blocklist,
            announcement_channel_id=self.settings.announcement_channel_id,
            regional_announcement_channel_id=self.settings.regional_announcement_channel_id,
            home=self.settings.home_point,
            local_radius_miles=self.settings.local_radius_miles,
            personal_delivery=self.settings.personal_delivery,
            bucket_roles=self.settings.bucket_role_map,
            timezone=self.settings.timezone,
        )
        # Discovery writes late-arriving artwork straight to SQLite. Hand it the
        # publication service so an event that was announced before its flyer
        # existed gets that embed refreshed instead of staying imageless.
        self.music_app.discovery.published_sync = self.publication_service
        self.scheduler = BotScheduler(self.settings)
        self.request_parser = RequestEventParser(self.settings)
        self._ready_once = False
        self._sync_once = False
        self._once_action: Callable[[], Awaitable[None]] | None = None
        self._sync_complete = asyncio.Event()
        self._sync_error: Exception | None = None
        self._initial_cycle_task: asyncio.Task[None] | None = None
        self._register_commands()

    async def setup_hook(self) -> None:
        for event_id, _channel_id, message_id in await self.repository.list_review_registrations():
            self.add_view(EventReviewView(self, event_id), message_id=message_id)
        for event_id, message_id in await self.repository.list_announcement_registrations():
            self.add_view(RsvpView(self, event_id), message_id=message_id)
        guild_id = self.settings.discord_guild_id
        if guild_id is None:
            raise RuntimeError("Discord guild ID is missing")
        guild = discord.Object(id=guild_id)
        await self.tree.sync(guild=guild)

    async def on_ready(self) -> None:
        if self._ready_once:
            return
        self._ready_once = True
        logger.info("Connected to Discord as %s", self.user)
        if self._sync_once:
            action = self._once_action or self.sync_reviews
            try:
                await action()
            except Exception as exc:
                self._sync_error = exc
            finally:
                self._sync_complete.set()
            return
        self.scheduler.configure(
            self.music_app.discovery.run,
            self.sync_reviews,
            self.repository.expire_past_events,
            self.drain_publication_queue,
            self.remind_rsvps,
            self.post_catchup,
        )
        self.scheduler.start()
        self._initial_cycle_task = asyncio.create_task(self._initial_cycle())

    async def _initial_cycle(self) -> None:
        try:
            await self.music_app.discovery.run()
            await self.sync_reviews()
        except Exception:
            logger.exception("Initial discovery/review cycle failed")

    async def close(self) -> None:
        self.scheduler.shutdown()
        await super().close()

    async def run_sync_once(self) -> None:
        await self.run_once(self.sync_reviews, "review synchronization")

    async def run_once(
        self,
        action: Callable[[], Awaitable[None]],
        description: str = "the requested action",
    ) -> None:
        """Connect, run one action against the live gateway, then disconnect."""
        token = self.settings.discord_token
        if token is None:
            raise RuntimeError("Discord token is missing")
        self._sync_once = True
        self._once_action = action
        start_task = asyncio.create_task(self.start(token.get_secret_value()))
        sync_task = asyncio.create_task(self._sync_complete.wait())
        done, _pending = await asyncio.wait(
            {start_task, sync_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if start_task in done and not self._sync_complete.is_set():
            sync_task.cancel()
            await start_task
            raise RuntimeError(f"Discord disconnected before {description} completed")
        await self.close()
        await start_task
        if self._sync_error:
            raise self._sync_error

    async def collect_guild_members(self) -> list[GuildMember]:
        """Every human member with the roles they hold.

        Uses the REST member list rather than the gateway cache: the cache is
        only populated once chunking finishes, which a one-shot CLI run does
        not wait for.
        """
        guild_id = self.settings.discord_guild_id
        if guild_id is None:
            raise RuntimeError("Discord guild ID is missing")
        guild = self.get_guild(guild_id) or await self.fetch_guild(guild_id)
        members: list[GuildMember] = []
        async for member in guild.fetch_members(limit=None):
            if member.bot:
                continue
            members.append(
                GuildMember(
                    user_id=member.id,
                    display_name=member.display_name,
                    role_ids=tuple(role.id for role in member.roles),
                )
            )
        return members

    async def drain_publication_queue(self) -> int:
        published = await self.publication_service.drain_approved(
            limit_per_hour=self.settings.publish_batch_per_hour,
            start_hour=self.settings.publish_start_hour,
            end_hour=self.settings.publish_end_hour,
            timezone=self.settings.timezone,
        )
        if published:
            logger.info("Published %d queued approved events", published)
        return published

    async def publish_or_queue(self, event_id: str) -> EventRecord:
        """Publish immediately when pacing is off; otherwise queue and drain.

        With pacing on, approval leaves the event in the approved queue and
        an immediate drain attempt publishes it right away only if the hourly
        ping budget and quiet-hours window allow.
        """
        if self.settings.publish_batch_per_hour <= 0:
            return await self.publication_service.publish(event_id)
        await self.drain_publication_queue()
        event = await self.repository.get_event(event_id)
        if event is None:
            raise KeyError(f"Unknown event ID: {event_id}")
        return event

    def queue_note(self, event: EventRecord) -> str:
        if event.status is EventStatus.PUBLISHED:
            return "Event approved and published."
        if event.status is EventStatus.PUBLISH_FAILED:
            return "Event approved but publication failed; use Retry publish."
        return (
            "Event approved and queued: announcements go out up to "
            f"{self.settings.publish_batch_per_hour}/hour between "
            f"{self.settings.publish_start_hour:02d}:00 and "
            f"{self.settings.publish_end_hour % 24:02d}:00 "
            f"({self.settings.default_timezone})."
        )

    async def _sweep_orphan_review_cards(self, channel: discord.TextChannel) -> int:
        """Delete orphaned cards and reconcile cards deleted out from under us.

        One history scan serves both directions: bot cards whose registration
        is gone come down, and registrations whose message is gone (reviewer
        cleared the channel by hand) are unregistered so those events repost —
        otherwise their unchanged card hash makes the sync skip them forever.
        """
        if self.user is None:
            return 0
        registered = await self.repository.list_registered_review_message_ids()
        bot_user_id = self.user.id
        deleted = 0
        scanned = 0
        seen: set[int] = set()
        async for message in channel.history(limit=1000):
            scanned += 1
            if message.id in registered:
                seen.add(message.id)
            elif message_is_orphan_card(message, bot_user_id, registered):
                try:
                    await message.delete()
                    deleted += 1
                except discord.NotFound:
                    pass
        missing = set(registered) - seen
        # Only trust "missing" when the scan covered the whole channel.
        if missing and scanned < 1000:
            cleared = await self.repository.clear_review_messages(missing)
            if cleared:
                logger.info(
                    "Reconciled %d review cards deleted outside the bot; "
                    "their events will repost",
                    cleared,
                )
        return deleted

    async def sync_reviews(self) -> int:
        channel = await self._review_channel()
        try:
            removed = await self._sweep_orphan_review_cards(channel)
            if removed:
                logger.info("Orphan card sweep removed %d messages", removed)
        except Exception:
            logger.exception("Orphaned review card sweep failed")
        synced = 0
        new_posts = 0
        edits = 0
        limit = self.settings.review_post_batch_size
        # Decided events lose their cards first — deletes are uncapped, and
        # clearing them shortens the channel before new cards land.
        for event in await self.repository.list_departed_events_with_cards():
            try:
                if await self.sync_event_review(event.id, channel=channel) == "removed":
                    synced += 1
            except discord.HTTPException:
                logger.exception("Failed removing decided card for %s", event.id)
        queue = await self.repository.list_review_queue()
        for event in queue:
            message_id = await self.repository.get_review_message_id(event.id)
            if message_id is None:
                if limit > 0 and new_posts >= limit:
                    # The queue is score-ordered, so the cap always posts the
                    # highest-scored unposted events first; the rest drain on
                    # later sync cycles.
                    continue
            elif limit > 0 and edits >= limit:
                # Discord hard-caps edits to messages older than an hour
                # (error 30046); spread mass card refreshes over cycles.
                continue
            try:
                result = await self.sync_event_review(event.id, channel=channel)
            except discord.HTTPException as exc:
                if exc.code == 30046:
                    logger.warning(
                        "Hourly limit for editing old messages reached; deferring "
                        "remaining card edits to the next sync cycle"
                    )
                    edits = limit if limit > 0 else edits
                    continue
                raise
            if result == "posted":
                new_posts += 1
                synced += 1
            elif result == "edited":
                edits += 1
                synced += 1
        return synced

    async def sync_event_review(
        self, event_id: str, *, channel: discord.TextChannel | None = None
    ) -> str | None:
        event = await self.repository.get_event(event_id)
        if event is None:
            return None
        channel = channel or await self._review_channel()
        if event.status not in _REVIEWABLE_STATUSES:
            # Decided (approved, published, rejected, expired): the card comes
            # down entirely instead of getting a final edit.
            message_id, _stored_hash = await self.repository.get_review_sync_state(event_id)
            if message_id is None:
                return None
            try:
                message = await channel.fetch_message(message_id)
                await message.delete()
            except discord.NotFound:
                pass
            await self.repository.clear_review_message(event_id)
            return "removed"
        view = EventReviewView(self, event_id)
        nearby = await self.repository.find_nearby_venue_events(event)
        duplicates = tuple(
            f"{dup.title[:70]} — {dup.status.value} `{dup.id[:8]}`" for dup in nearby[:3]
        )
        embed = review_embed(event, duplicates)
        card_hash = review_card_hash(embed, view is not None)
        message_id, stored_hash = await self.repository.get_review_sync_state(event_id)
        if message_id and stored_hash == card_hash:
            # Nothing on the card changed; skip the fetch and edit entirely.
            return "unchanged"
        if message_id:
            try:
                message = await channel.fetch_message(message_id)
                await message.edit(embed=embed, view=view)
                await self.repository.set_review_message(
                    event_id, channel.id, message.id, card_hash
                )
                return "edited"
            except discord.NotFound:
                logger.warning("Stored review message %s no longer exists", message_id)
        message = await channel.send(embed=embed, view=view)
        await self.repository.set_review_message(event_id, channel.id, message.id, card_hash)
        return "posted"

    async def refresh_review_message(self, event_id: str) -> None:
        await self.sync_event_review(event_id)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or self.user is None:
            return
        if message.guild is None or message.guild.id != self.settings.discord_guild_id:
            return
        # Require the mention to be typed in the message text. Replying to
        # one of the bot's messages also puts it in message.mentions, and
        # commentary on an announcement is not an event request.
        if not re.search(rf"<@!?{self.user.id}>", message.content):
            return
        try:
            await self._handle_event_request(message)
        except Exception:
            logger.exception("Failed handling an @mention event request")

    async def _handle_event_request(self, message: discord.Message) -> None:
        """Anyone can @mention the bot with a show and it becomes a review card.

        This is the community's side door past the taste gate: the event skips
        discovery scoring thresholds entirely and lands in the queue marked
        with who asked for it.
        """
        assert self.user is not None
        content = re.sub(rf"<@!?{self.user.id}>", " ", message.content)
        url_match = _URL_RE.search(content)
        url = url_match.group(0).rstrip(">),.") if url_match else None
        title = " ".join(_URL_RE.sub(" ", content).split()).strip(" -–—:,")
        context: list[tuple[str, str]] = []
        try:
            async for prior in message.channel.history(limit=10, before=message):
                if prior.content:
                    context.append((prior.author.display_name, prior.content[:500]))
        except discord.Forbidden:
            pass
        context.reverse()
        page_text = await self._fetch_page_text(url) if url else None
        extracted = await self.request_parser.extract(
            content.strip(),
            context,
            datetime.now(self.settings.timezone),
            page_text=page_text,
        )
        starts_at = None
        date_note = None
        venue = location = artist = None
        genres: tuple[str, ...] = ()
        if extracted:
            title = str(extracted["title"]).strip()
            artist = str(extracted.get("artist", "")).strip() or None
            venue = str(extracted.get("venue", "")).strip() or None
            location = str(extracted.get("location", "")).strip() or None
            url = url or (str(extracted.get("url", "")).strip() or None)
            genres = tuple(
                str(genre).strip() for genre in extracted.get("genres", []) if str(genre).strip()
            )
            raw_start = str(extracted.get("starts_at", "")).strip()
            if raw_start:
                if len(raw_start) <= 10:
                    # Date only: leave the start time for the reviewer to fill
                    # in rather than fabricating one.
                    date_note = raw_start
                else:
                    try:
                        starts_at = parse_datetime(raw_start)
                        if starts_at.tzinfo is None:
                            starts_at = starts_at.replace(tzinfo=self.settings.timezone)
                    except (ValueError, OverflowError):
                        date_note = raw_start
        if not title and not url:
            await message.reply(
                "Tell me what to add — `@" + self.user.name + " <artist or event,"
                " plus a link if you have one>` and I'll queue a review card.",
                mention_author=False,
            )
            return
        candidate = manual_event(
            title=title or url or "Untitled request",
            starts_at=starts_at,
            venue=venue,
            location=location,
            source_url=url,
            artist=artist,
            description=f"Date mentioned: {date_note}" if date_note else None,
            duration_minutes=self.settings.default_event_duration_minutes,
            submitted_by=message.author.id,
            source_name="request",
        )
        if genres:
            candidate = dataclass_replace(candidate, genres=genres)
        score = score_event(
            candidate,
            self.music_app.profile,
            home=self.settings.home_point,
            max_travel_radius_miles=self.settings.max_travel_radius_miles,
        )
        score = dataclass_replace(
            score, reasons=(*score.reasons, f"requested by {message.author.display_name}")
        )
        result = await self.repository.upsert_discovered(candidate, score)
        await self.sync_event_review(result.event.id)
        details = [part for part in (venue, date_note) if part]
        detail_text = f" ({', '.join(details)})" if details else ""
        note = (
            ""
            if result.event.is_complete
            else " Some details are missing, so it may need an edit before it"
            " can be approved."
        )
        await message.reply(
            f"Got it — **{result.event.title}**{detail_text} is in the review queue.{note}",
            mention_author=False,
        )

    async def _fetch_page_text(self, url: str) -> str | None:
        """Fetch a requested link and return its visible text for extraction."""
        import httpx

        from music_event_bot.discovery.feeds import _clean_feed_html

        try:
            async with httpx.AsyncClient(
                timeout=10,
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"
                    )
                },
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Could not fetch requested link %s: %s", url, exc)
            return None
        text = _clean_feed_html(response.text)
        return text[:6000] if text else None

    async def remind_rsvps(self) -> int:
        """Ping Going/Interested RSVPs roughly a day before their event."""
        now = datetime.now(UTC)
        due = await self.repository.list_events_needing_reminder(
            now, now + timedelta(hours=25)
        )
        sent = 0
        for event, user_ids in due:
            if not user_ids or event.starts_at is None:
                # Nothing to ping; mark it so it never re-qualifies.
                await self.repository.mark_reminder_sent(event.id)
                continue
            publication = await self.repository.get_publication(event.id)
            channel_id = self.settings.announcement_channel_id
            if publication is None or channel_id is None:
                await self.repository.mark_reminder_sent(event.id)
                continue
            # Reminders always go to the main channel, even for a show whose card
            # lives in the regional one: muting the regional flood should cost you
            # the browsing, never a reminder for a show you said you were going to.
            # Only the jump link follows the card.
            channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue
            when = f"<t:{int(event.starts_at.timestamp())}:t>"
            venue = f" at {event.venue}" if event.venue else ""
            header = f"⏰ Tomorrow: **{event.title}**{venue}, {when}"
            announcement_id = publication.get("announcement_message_id")
            if announcement_id:
                card_channel_id = publication.get("announcement_channel_id") or channel_id
                header += (
                    f"\nhttps://discord.com/channels/{self.settings.discord_guild_id}"
                    f"/{card_channel_id}/{announcement_id}"
                )
            mentions = [f"<@{user_id}>" for user_id in user_ids]
            await channel.send(
                header + "\n" + " ".join(mentions)[:1800],
                allowed_mentions=discord.AllowedMentions(users=True),
            )
            await self.repository.mark_reminder_sent(event.id)
            sent += 1
        if sent:
            logger.info("Sent %d event-tomorrow RSVP reminders", sent)
        return sent

    async def post_catchup(self) -> int:
        """One public post a day for everything that overflowed a daily cap.

        Deliberately one message: a member who overflowed by six shows should
        pay one notification for them, not six. It stays in the channel
        rather than becoming a DM so the whole model stays public -- the
        mentions here are the same mentions the announcements carry.
        """
        if self.settings.personal_delivery != "on":
            return 0
        expired = await self.repository.expire_stale_queued(datetime.now(UTC))
        if expired:
            logger.info("Dropped %d queued item(s) whose show had already started", expired)
        queued = await self.repository.list_queued_notifications()
        if not queued:
            return 0
        channel_id = self.settings.announcement_channel_id
        if channel_id is None:
            return 0
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return 0

        lines: list[str] = []
        mentioned: set[int] = set()
        delivered: list[str] = []
        for event, user_ids in queued[: self.settings.catchup_max_events]:
            publication = await self.repository.get_publication(event.id)
            link = ""
            if publication and publication.get("announcement_message_id"):
                card_channel = publication.get("announcement_channel_id") or channel_id
                link = (
                    f"https://discord.com/channels/{self.settings.discord_guild_id}"
                    f"/{card_channel}/{publication['announcement_message_id']}"
                )
            when = f" <t:{int(event.starts_at.timestamp())}:D>" if event.starts_at else ""
            names = " ".join(f"<@{user_id}>" for user_id in user_ids)
            title = f"[{event.title}]({link})" if link else f"**{event.title}**"
            lines.append(f"- {title}{when} — {names}")
            mentioned.update(user_ids)
            delivered.append(event.id)
        overflow = len(queued) - len(delivered)
        header = (
            f"**Catch-up — {len(delivered)} show(s) you matched but did not get pinged for**"
        )
        body = "\n".join(lines)
        if overflow:
            body += f"\n- …and {overflow} more waiting in the channel."
        await channel.send(
            f"{header}\n{body}"[:2000],
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                roles=False,
                users=[discord.Object(id=user_id) for user_id in mentioned],
                replied_user=False,
            ),
        )
        await self.repository.mark_notifications_delivered(tuple(delivered))
        logger.info(
            "Catch-up post covered %d event(s) for %d member(s)", len(delivered), len(mentioned)
        )
        return len(delivered)

    async def _review_channel(self) -> discord.TextChannel:
        channel_id = self.settings.review_channel_id
        if channel_id is None:
            raise RuntimeError("Review channel ID is missing")
        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise TypeError("Review channel must be a text channel")
        return channel

    async def ensure_profile(
        self, member: discord.Member, *, create_if_empty: bool = False
    ) -> dict[str, Any] | None:
        """The member's profile, seeded from their roles if they have none.

        Shared by /me and the role listener so a member reaches per-user
        delivery the same way whichever they touch first: the roles they
        already hold become the profile, rather than a blank slate that
        would quietly stop pinging them.
        """
        existing = await self.repository.get_user_profile(member.id)
        if existing is not None:
            return existing
        role_genres = bucket_genre_roles(
            await self.repository.list_genre_roles(),
            {normalize_text(genre) for genre in self.settings.role_map},
        )
        granted = sorted(
            {genre for role in member.roles for genre in role_genres.get(role.id, ())}
        )
        if not granted and not create_if_empty:
            return None
        await self.repository.upsert_user_profile(
            member.id,
            display_name=member.display_name,
            metro=nearest_metro(self.settings.home_point).key,
            travel_band=DEFAULT_TRAVEL_BAND,
            daily_ping_cap=self.settings.default_daily_ping_cap,
            source="role-seed",
        )
        if granted:
            await self.repository.add_user_taste(
                member.id, "genre", tuple(granted), source="role-seed"
            )
        logger.info("Seeded a profile for %s from %d role genre(s)", member.id, len(granted))
        return await self.repository.get_user_profile(member.id)

    async def profile_summary(self, user_id: int) -> str:
        profile = await self.repository.get_user_profile(user_id)
        if profile is None:
            return "You have no alert profile yet."
        band = travel_band(str(profile["travel_band"]))
        taste = await self.repository.get_user_taste(user_id, "genre")
        held = [value for value, weight in taste.items() if weight > 0]
        dropped = [value for value, weight in taste.items() if weight <= 0]
        cap = int(profile["daily_ping_cap"])
        home_label = metro(str(profile["metro"])).label
        cap_label = str(cap) if cap > 0 else "no limit"
        overflow = " - anything over waits for the daily catch-up post" if cap > 0 else ""
        held_label = ", ".join(held) if held else "none yet"
        lines = [
            "**Your show alerts**",
            f"- Home: **{home_label}**",
            f"- Travel: **{band.label}** (within {band.radius_miles} miles)",
            f"- Daily cap: **{cap_label}**{overflow}",
            f"- Delivery: **{profile['delivery']}**",
            f"- Genres: {held_label}",
        ]
        if dropped:
            lines.append("- Dropped: " + ", ".join(dropped))
        return "\n".join(lines)

    def _register_profile_commands(self, guild: discord.Object) -> None:
        """/me -- a member's own alert settings.

        Deliberately no permission gate, like the RSVP buttons: these are
        every member's own settings, not a reviewer action.
        """
        group = app_commands.Group(name="me", description="Tune which shows ping you")
        buckets = sorted({normalize_text(genre) for genre in self.settings.role_map})

        async def _member(interaction: discord.Interaction) -> discord.Member | None:
            if not isinstance(interaction.user, discord.Member):
                await interaction.response.send_message(
                    "Use this in the server, not in a DM.", ephemeral=True
                )
                return None
            return interaction.user

        async def _respond(interaction: discord.Interaction, note: str) -> None:
            summary = await self.profile_summary(interaction.user.id)
            await interaction.response.send_message(
                note + "\n\n" + summary,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        @group.command(name="show", description="Show your alert settings")
        async def show(interaction: discord.Interaction) -> None:
            member = await _member(interaction)
            if member is None:
                return
            await self.ensure_profile(member, create_if_empty=True)
            await _respond(interaction, "Here is what the bot has for you.")

        @group.command(name="home", description="Set the metro you go to shows in")
        @app_commands.choices(
            place=[
                app_commands.Choice(name=candidate.label, value=candidate.key)
                for candidate in METROS
            ]
        )
        async def home(
            interaction: discord.Interaction, place: app_commands.Choice[str]
        ) -> None:
            member = await _member(interaction)
            if member is None:
                return
            await self.ensure_profile(member, create_if_empty=True)
            await self.repository.update_user_profile(member.id, metro=place.value)
            await _respond(interaction, f"Home set to **{place.name}**.")

        @group.command(name="travel", description="How far you will go for a show")
        @app_commands.choices(
            distance=[
                app_commands.Choice(
                    name=f"{band.label} (within {band.radius_miles} miles)", value=band.key
                )
                for band in TRAVEL_BANDS
            ]
        )
        async def travel(
            interaction: discord.Interaction, distance: app_commands.Choice[str]
        ) -> None:
            member = await _member(interaction)
            if member is None:
                return
            await self.ensure_profile(member, create_if_empty=True)
            await self.repository.update_user_profile(member.id, travel_band=distance.value)
            await _respond(interaction, f"Travel range set to **{distance.name}**.")

        @group.command(name="cap", description="Most pings you want in one day")
        @app_commands.describe(per_day="0 means no limit. Anything over waits for the catch-up.")
        async def cap(
            interaction: discord.Interaction, per_day: app_commands.Range[int, 0, 50]
        ) -> None:
            member = await _member(interaction)
            if member is None:
                return
            await self.ensure_profile(member, create_if_empty=True)
            await self.repository.update_user_profile(member.id, daily_ping_cap=int(per_day))
            await _respond(interaction, f"Daily cap set to **{per_day or 'no limit'}**.")

        @group.command(name="delivery", description="How you hear about shows")
        @app_commands.choices(
            mode=[
                app_commands.Choice(name="Mention me on matching shows", value="mention"),
                app_commands.Choice(name="Mention me on everything", value="firehose"),
                app_commands.Choice(name="Never mention me", value="off"),
            ]
        )
        async def delivery(
            interaction: discord.Interaction, mode: app_commands.Choice[str]
        ) -> None:
            member = await _member(interaction)
            if member is None:
                return
            await self.ensure_profile(member, create_if_empty=True)
            await self.repository.update_user_profile(member.id, delivery=mode.value)
            await _respond(interaction, f"Delivery set to **{mode.name}**.")

        @group.command(name="genre", description="Add or drop a genre")
        @app_commands.choices(
            action=[
                app_commands.Choice(name="Add", value="add"),
                app_commands.Choice(name="Drop", value="drop"),
            ]
        )
        @app_commands.describe(name="One of the genre buckets this server uses")
        async def genre_command(
            interaction: discord.Interaction, action: app_commands.Choice[str], name: str
        ) -> None:
            member = await _member(interaction)
            if member is None:
                return
            chosen = normalize_text(name)
            if chosen not in buckets:
                await interaction.response.send_message(
                    f"`{name}` is not one of: " + ", ".join(buckets), ephemeral=True
                )
                return
            await self.ensure_profile(member, create_if_empty=True)
            # Dropping writes -1 rather than deleting the row: a negative
            # weight is what stops a later seed run handing the genre back.
            await self.repository.set_user_taste(
                member.id, "genre", chosen, 1 if action.value == "add" else -1
            )
            verb = "Added" if action.value == "add" else "Dropped"
            await _respond(interaction, f"{verb} **{chosen}**.")

        @genre_command.autocomplete("name")
        async def genre_autocomplete(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            matching = [item for item in buckets if current.casefold() in item]
            return [app_commands.Choice(name=item, value=item) for item in matching[:25]]

        self.tree.add_command(group, guild=guild)

    def _register_commands(self) -> None:
        group = app_commands.Group(name="event", description="Manage music events")
        guild_id = self.settings.discord_guild_id
        if guild_id is None:
            raise RuntimeError("Discord guild ID is missing")
        guild = discord.Object(id=guild_id)

        @group.command(name="submit", description="Submit an event for private review")
        @app_commands.describe(
            title="Event title",
            start="Local start time, for example 2026-09-12 20:00",
            venue="Venue name",
            location="Full address or location",
            url="Source or ticket URL",
            artist="Primary artist",
            genre="Genre used for role matching",
        )
        async def submit(
            interaction: discord.Interaction,
            title: str,
            start: str | None = None,
            venue: str | None = None,
            location: str | None = None,
            url: str | None = None,
            artist: str | None = None,
            genre: str | None = None,
        ) -> None:
            if not await require_reviewer(interaction, self.settings):
                return
            starts_at = None
            if start:
                try:
                    starts_at = parse_datetime(start)
                    if starts_at.tzinfo is None:
                        starts_at = starts_at.replace(tzinfo=self.settings.timezone)
                except (ValueError, OverflowError) as exc:
                    await interaction.response.send_message(
                        f"Could not parse the start time: {exc}", ephemeral=True
                    )
                    return
            candidate = manual_event(
                title=title,
                starts_at=starts_at,
                venue=venue,
                location=location,
                source_url=url,
                artist=artist,
                genre=genre,
                duration_minutes=self.settings.default_event_duration_minutes,
                submitted_by=interaction.user.id,
            )
            result = await self.repository.upsert_discovered(
                candidate,
                score_event(
                    candidate,
                    self.music_app.profile,
                    home=self.settings.home_point,
                    max_travel_radius_miles=self.settings.max_travel_radius_miles,
                ),
            )
            await self.sync_event_review(result.event.id)
            await interaction.response.send_message(
                f"Queued `{result.event.id}` for review.", ephemeral=True
            )

        @group.command(
            name="queue", description="Show everything waiting in the review and publish queues"
        )
        async def queue_view(interaction: discord.Interaction) -> None:
            if not await require_reviewer(interaction, self.settings):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            total, posted, waiting = await self.repository.review_queue_snapshot(1000)
            approved = await self.repository.list_events(EventStatus.APPROVED)

            def line(event: EventRecord, *, with_score: bool) -> str:
                when = (
                    f"<t:{int(event.starts_at.timestamp())}:d>"
                    if event.starts_at
                    else "date TBD"
                )
                prefix = f"`{event.score:>3}` " if with_score else ""
                return f"- {prefix}{event.title[:60]} — {event.venue or '?'}, {when}"

            lines = [
                f"**Review queue:** {total} events, {posted} cards posted, "
                f"{total - posted} still waiting "
                f"(posting {self.settings.review_post_batch_size} per sync cycle)."
            ]
            if waiting:
                lines.append("Waiting, in posting order (requests first, then score):")
                lines.extend(line(event, with_score=True) for event in waiting)
            lines.append("")
            lines.append(
                f"**Publish queue:** {len(approved)} approved, announcing up to "
                f"{self.settings.publish_batch_per_hour}/hour, soonest show first."
            )
            lines.extend(line(event, with_score=False) for event in approved)
            for chunk in chunk_message_lines(lines):
                await interaction.followup.send(chunk, ephemeral=True)

        @group.command(name="show", description="Show an event record")
        async def show(interaction: discord.Interaction, event_id: str) -> None:
            if not await require_reviewer(interaction, self.settings):
                return
            event = await self.repository.get_event(event_id)
            if event is None:
                await interaction.response.send_message("Unknown event ID.", ephemeral=True)
                return
            await interaction.response.send_message(embed=review_embed(event), ephemeral=True)

        @group.command(name="approve", description="Approve and publish an event")
        async def approve(interaction: discord.Interaction, event_id: str) -> None:
            if not await require_reviewer(interaction, self.settings):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await self.repository.approve(event_id, interaction.user.id)
                event = await self.publish_or_queue(event_id)
            except Exception as exc:
                await interaction.followup.send(f"Approval failed: {exc}", ephemeral=True)
                await self.refresh_review_message(event_id)
                return
            await interaction.followup.send(self.queue_note(event), ephemeral=True)
            await self.refresh_review_message(event_id)

        @group.command(name="reject", description="Reject a pending event")
        async def reject(
            interaction: discord.Interaction, event_id: str, reason: str | None = None
        ) -> None:
            if not await require_reviewer(interaction, self.settings):
                return
            try:
                await self.repository.reject(event_id, interaction.user.id, reason)
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            await interaction.response.send_message("Event rejected.", ephemeral=True)
            await self.refresh_review_message(event_id)

        @group.command(name="edit", description="Open the event edit form")
        async def edit(interaction: discord.Interaction, event_id: str) -> None:
            if not await require_reviewer(interaction, self.settings):
                return
            event = await self.repository.get_event(event_id)
            if event is None:
                await interaction.response.send_message("Unknown event ID.", ephemeral=True)
                return
            await interaction.response.send_modal(EditEventModal(self, event))

        @group.command(name="retry", description="Retry a failed publication")
        async def retry(interaction: discord.Interaction, event_id: str) -> None:
            if not await require_reviewer(interaction, self.settings):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await self.repository.approve(event_id, interaction.user.id)
                await self.publication_service.publish(event_id)
            except Exception as exc:
                await interaction.followup.send(f"Retry failed: {exc}", ephemeral=True)
                return
            await interaction.followup.send("Publication retry succeeded.", ephemeral=True)
            await self.refresh_review_message(event_id)

        @group.command(name="set-role", description="Map a genre to a Discord role")
        async def set_role(
            interaction: discord.Interaction, genre: str, role: discord.Role
        ) -> None:
            if not await require_reviewer(interaction, self.settings):
                return
            await self.repository.set_genre_role(genre, role.id)
            await interaction.response.send_message(
                f"Events tagged `{genre}` will mention {role.mention}.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        self.tree.add_command(group, guild=guild)
        self._register_profile_commands(guild)
