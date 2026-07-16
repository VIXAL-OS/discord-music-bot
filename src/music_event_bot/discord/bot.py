from __future__ import annotations

import asyncio
import hashlib
import json
import logging

import discord
from dateutil.parser import parse as parse_datetime
from discord import app_commands
from discord.ext import commands

from music_event_bot.app import Application
from music_event_bot.discord.permissions import require_reviewer
from music_event_bot.discord.publishing import DiscordPublicationGateway
from music_event_bot.discord.review import EditEventModal, EventReviewView, review_embed
from music_event_bot.discovery.manual import manual_event
from music_event_bot.domain.models import EventStatus
from music_event_bot.domain.scoring import score_event
from music_event_bot.services.publishing import PublicationService
from music_event_bot.services.scheduler import BotScheduler

logger = logging.getLogger(__name__)


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
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        self.music_app = app
        self.settings = app.settings
        self.repository = app.repository
        self.gateway = DiscordPublicationGateway(self, self.settings)
        fallback_roles = frozenset(
            role_id
            for genre, role_id in self.settings.role_map.items()
            if genre.startswith("other")
        )
        self.publication_service = PublicationService(
            self.repository, self.gateway, fallback_role_ids=fallback_roles
        )
        self.scheduler = BotScheduler(self.settings)
        self._ready_once = False
        self._sync_once = False
        self._sync_complete = asyncio.Event()
        self._sync_error: Exception | None = None
        self._initial_cycle_task: asyncio.Task[None] | None = None
        self._register_commands()

    async def setup_hook(self) -> None:
        for event_id, _channel_id, message_id in await self.repository.list_review_registrations():
            self.add_view(EventReviewView(self, event_id), message_id=message_id)
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
            try:
                await self.sync_reviews()
            except Exception as exc:
                self._sync_error = exc
            finally:
                self._sync_complete.set()
            return
        self.scheduler.configure(
            self.music_app.discovery.run,
            self.sync_reviews,
            self.repository.expire_past_events,
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
        token = self.settings.discord_token
        if token is None:
            raise RuntimeError("Discord token is missing")
        self._sync_once = True
        start_task = asyncio.create_task(self.start(token.get_secret_value()))
        sync_task = asyncio.create_task(self._sync_complete.wait())
        done, _pending = await asyncio.wait(
            {start_task, sync_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if start_task in done and not self._sync_complete.is_set():
            sync_task.cancel()
            await start_task
            raise RuntimeError("Discord disconnected before review synchronization completed")
        await self.close()
        await start_task
        if self._sync_error:
            raise self._sync_error

    async def sync_reviews(self) -> int:
        channel = await self._review_channel()
        synced = 0
        new_posts = 0
        limit = self.settings.review_post_batch_size
        for event in await self.repository.list_review_queue():
            message_id = await self.repository.get_review_message_id(event.id)
            if message_id is None and limit > 0 and new_posts >= limit:
                # The queue is score-ordered, so the cap always posts the
                # highest-scored unposted events first; the rest drain on
                # later sync cycles.
                continue
            await self.sync_event_review(event.id, channel=channel)
            if message_id is None:
                new_posts += 1
            synced += 1
        return synced

    async def sync_event_review(
        self, event_id: str, *, channel: discord.TextChannel | None = None
    ) -> None:
        event = await self.repository.get_event(event_id)
        if event is None:
            return
        channel = channel or await self._review_channel()
        view = (
            EventReviewView(self, event_id)
            if event.status
            in {EventStatus.PENDING_REVIEW, EventStatus.INCOMPLETE, EventStatus.PUBLISH_FAILED}
            else None
        )
        embed = review_embed(event)
        card_hash = review_card_hash(embed, view is not None)
        message_id, stored_hash = await self.repository.get_review_sync_state(event_id)
        if message_id and stored_hash == card_hash:
            # Nothing on the card changed; skip the fetch and edit entirely.
            return
        if message_id:
            try:
                message = await channel.fetch_message(message_id)
                await message.edit(embed=embed, view=view)
                await self.repository.set_review_message(
                    event_id, channel.id, message.id, card_hash
                )
                return
            except discord.NotFound:
                logger.warning("Stored review message %s no longer exists", message_id)
        message = await channel.send(embed=embed, view=view)
        await self.repository.set_review_message(event_id, channel.id, message.id, card_hash)

    async def refresh_review_message(self, event_id: str) -> None:
        await self.sync_event_review(event_id)

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
                await self.publication_service.publish(event_id)
            except Exception as exc:
                await interaction.followup.send(f"Publication failed: {exc}", ephemeral=True)
                await self.refresh_review_message(event_id)
                return
            await interaction.followup.send("Event approved and published.", ephemeral=True)
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
