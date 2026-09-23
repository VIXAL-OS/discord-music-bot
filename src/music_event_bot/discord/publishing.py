from __future__ import annotations

import logging
from datetime import UTC, timedelta
from typing import TYPE_CHECKING

import discord

from music_event_bot.config import Settings
from music_event_bot.domain.models import EventRecord

if TYPE_CHECKING:
    from music_event_bot.discord.bot import MusicEventDiscordBot

logger = logging.getLogger(__name__)

# Discord rejects new guild scheduled events once 100 are pending.
_SCHEDULED_EVENT_CAP_ERROR = 30038


def event_marker(event_id: str) -> str:
    return f"[music-event-id:{event_id}]"


def _role_content(
    role_ids: tuple[int, ...], suffix: str, user_ids: tuple[int, ...] = ()
) -> str:
    mentions = " ".join(
        [f"<@&{role_id}>" for role_id in role_ids] + [f"<@{user_id}>" for user_id in user_ids]
    )
    if not mentions:
        return suffix
    # Truncated rather than split: one message is one notification however
    # many ways a member is named in it, and a second message would cost
    # everyone a second ping.
    return f"{mentions[:1800]} {suffix}"


def _role_mentions(
    role_ids: tuple[int, ...], user_ids: tuple[int, ...] = ()
) -> discord.AllowedMentions:
    return discord.AllowedMentions(
        everyone=False,
        users=[discord.Object(id=user_id) for user_id in user_ids] if user_ids else False,
        roles=[discord.Object(id=role_id) for role_id in role_ids] if role_ids else False,
        replied_user=False,
    )


def event_embed(event: EventRecord, *, pending: bool = False) -> discord.Embed:
    color = discord.Color.orange() if pending else discord.Color.blurple()
    embed = discord.Embed(
        title=event.title,
        # Full source text by reviewer preference: door times, bag policies,
        # and rain-or-shine notes are useful; duplication is removed upstream.
        description=(event.description or "")[:4000],
        color=color,
        url=event.url,
    )
    if len(event.artists) > 1:
        embed.add_field(name="Lineup", value=", ".join(event.artists)[:1024], inline=False)
    elif event.artist:
        embed.add_field(name="Artist", value=event.artist, inline=True)
    if event.starts_at:
        embed.add_field(
            name="When",
            value=f"<t:{int(event.starts_at.timestamp())}:F>\n<t:{int(event.starts_at.timestamp())}:R>",
            inline=False,
        )
    embed.add_field(name="Venue", value=event.venue or "Needs review", inline=True)
    embed.add_field(name="Location", value=event.location or "Needs review", inline=True)
    if event.genres:
        embed.add_field(name="Genres", value=", ".join(event.genres), inline=False)
    if event.url:
        embed.add_field(name="Source / tickets", value=f"[Open listing]({event.url})", inline=False)
    if event.image_url:
        embed.set_image(url=event.image_url)
    # The marker must appear on every embed (it is how announcements are
    # re-found after a crash), but the match score is reviewer-only.
    footer = event_marker(event.id)
    if pending:
        footer += f" • score {event.score}/100"
    embed.set_footer(text=footer)
    return embed


class DiscordPublicationGateway:
    def __init__(self, bot: MusicEventDiscordBot, settings: Settings) -> None:
        self.bot = bot
        self.settings = settings

    async def create_or_find_scheduled_event(self, event: EventRecord) -> int | None:
        guild = await self._guild()
        marker = event_marker(event.id)
        for scheduled in await guild.fetch_scheduled_events():
            if marker in (scheduled.description or ""):
                return scheduled.id

        if event.starts_at is None or not event.venue or not event.location:
            raise ValueError("A Scheduled Event requires a start time, venue, and location")
        starts_at = event.starts_at.astimezone(UTC)
        if starts_at <= discord.utils.utcnow():
            raise ValueError("Discord Scheduled Events cannot start in the past")
        ends_at = (
            event.ends_at.astimezone(UTC)
            if event.ends_at
            else starts_at + timedelta(minutes=self.settings.default_event_duration_minutes)
        )
        description = self._scheduled_description(event)
        try:
            scheduled = await guild.create_scheduled_event(
                name=event.title[:100],
                description=description[:1000],
                start_time=starts_at,
                end_time=ends_at,
                entity_type=discord.EntityType.external,
                privacy_level=discord.PrivacyLevel.guild_only,
                location=f"{event.venue} — {event.location}"[:100],
                reason=f"Approved music event {event.id}",
            )
        except discord.HTTPException as exc:
            if exc.code != _SCHEDULED_EVENT_CAP_ERROR:
                raise
            # The guild is at Discord's 100 pending scheduled events cap.
            # The announcement is the product; the native event is a bonus,
            # so publish without one rather than jamming the queue.
            logger.warning(
                "Guild scheduled-event cap reached; publishing %r without a "
                "native Scheduled Event",
                event.title,
            )
            return None
        return scheduled.id

    async def create_or_find_announcement(
        self,
        event: EventRecord,
        role_ids: tuple[int, ...],
        scheduled_event_id: int | None,
        channel_id: int | None = None,
        user_ids: tuple[int, ...] = (),
    ) -> int:
        channel = await self._announcement_channel(channel_id)
        marker = event_marker(event.id)
        async for message in channel.history(limit=100):
            if message.author.id == self.bot.user.id and any(
                embed.footer and marker in (embed.footer.text or "") for embed in message.embeds
            ):
                return message.id

        # Local import: the RSVP module renders cards with event_embed, so
        # importing it at module level would be circular.
        from music_event_bot.discord.rsvp import RsvpView, announcement_embed

        # The scheduled-event link makes Discord render its native event card
        # with an Interested button alongside the bot's own RSVP buttons.
        suffix = "New show alert!"
        if scheduled_event_id is not None:
            suffix += f"\n{self._event_link(scheduled_event_id)}"
        content = _role_content(role_ids, suffix, user_ids)
        allowed_mentions = _role_mentions(role_ids, user_ids)
        groups = await self.bot.repository.get_rsvps(event.id)
        message = await channel.send(
            content=content,
            embed=announcement_embed(event, groups),
            allowed_mentions=allowed_mentions,
            view=RsvpView(self.bot, event.id),
        )
        return message.id

    async def update_published_event(
        self,
        event: EventRecord,
        scheduled_event_id: int | None,
        announcement_message_id: int,
        role_ids: tuple[int, ...],
        channel_id: int | None = None,
        user_ids: tuple[int, ...] = (),
    ) -> None:
        if event.starts_at is None or not event.venue or not event.location:
            raise ValueError("Published events require a start time, venue, and location")
        starts_at = event.starts_at.astimezone(UTC)
        ends_at = (
            event.ends_at.astimezone(UTC)
            if event.ends_at
            else starts_at + timedelta(minutes=self.settings.default_event_duration_minutes)
        )
        if scheduled_event_id is not None:
            guild = await self._guild()
            scheduled = await guild.fetch_scheduled_event(scheduled_event_id)
            await scheduled.edit(
                name=event.title[:100],
                description=self._scheduled_description(event)[:1000],
                start_time=starts_at,
                end_time=ends_at,
                location=f"{event.venue} — {event.location}"[:100],
                reason=f"Updated music event {event.id}",
            )
        from music_event_bot.discord.rsvp import RsvpView, announcement_embed

        channel = await self._announcement_channel(channel_id)
        message = await channel.fetch_message(announcement_message_id)
        suffix = "Updated show listing"
        if scheduled_event_id is not None:
            suffix += f"\n{self._event_link(scheduled_event_id)}"
        groups = await self.bot.repository.get_rsvps(event.id)
        await message.edit(
            content=_role_content(role_ids, suffix, user_ids),
            embed=announcement_embed(event, groups),
            allowed_mentions=_role_mentions(role_ids, user_ids),
            # Also backfills RSVP buttons onto announcements posted before
            # the feature existed.
            view=RsvpView(self.bot, event.id),
        )

    def _event_link(self, scheduled_event_id: int) -> str:
        return f"https://discord.com/events/{self.settings.discord_guild_id}/{scheduled_event_id}"

    async def _guild(self) -> discord.Guild:
        if self.settings.discord_guild_id is None:
            raise RuntimeError("Discord guild ID is missing")
        guild = self.bot.get_guild(self.settings.discord_guild_id)
        if guild is None:
            guild = await self.bot.fetch_guild(self.settings.discord_guild_id)
        return guild

    async def _announcement_channel(self, channel_id: int | None = None) -> discord.TextChannel:
        # The caller passes the channel its routing chose (or the one an
        # existing card was posted in); the setting is only the fallback for
        # announcements recorded before the local/regional split existed.
        if channel_id is None:
            channel_id = self.settings.announcement_channel_id
        if channel_id is None:
            raise RuntimeError("Announcement channel ID is missing")
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise TypeError("Announcement channel must be a text channel")
        return channel

    @staticmethod
    def _scheduled_description(event: EventRecord) -> str:
        parts = [event_marker(event.id)]
        if event.description:
            parts.append(event.description)
        if event.url:
            parts.append(f"Tickets/source: {event.url}")
        return "\n\n".join(parts)
