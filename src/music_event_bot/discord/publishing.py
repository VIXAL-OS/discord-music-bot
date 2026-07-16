from __future__ import annotations

from datetime import UTC, timedelta

import discord

from music_event_bot.config import Settings
from music_event_bot.domain.models import EventRecord


def event_marker(event_id: str) -> str:
    return f"[music-event-id:{event_id}]"


def event_embed(event: EventRecord, *, pending: bool = False) -> discord.Embed:
    color = discord.Color.orange() if pending else discord.Color.blurple()
    embed = discord.Embed(
        title=event.title,
        description=(event.description or "")[:4000],
        color=color,
        url=event.url,
    )
    if event.artist:
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
    embed.set_footer(text=f"{event_marker(event.id)} • score {event.score}/100")
    return embed


class DiscordPublicationGateway:
    def __init__(self, bot: discord.Client, settings: Settings) -> None:
        self.bot = bot
        self.settings = settings

    async def create_or_find_scheduled_event(self, event: EventRecord) -> int:
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
        return scheduled.id

    async def create_or_find_announcement(
        self, event: EventRecord, role_id: int | None
    ) -> int:
        channel = await self._announcement_channel()
        marker = event_marker(event.id)
        async for message in channel.history(limit=100):
            if message.author.id == self.bot.user.id and any(
                embed.footer and marker in (embed.footer.text or "") for embed in message.embeds
            ):
                return message.id

        content = f"<@&{role_id}> New show alert!" if role_id else "New show alert!"
        allowed_mentions = discord.AllowedMentions(
            everyone=False,
            users=False,
            roles=[discord.Object(id=role_id)] if role_id else False,
            replied_user=False,
        )
        message = await channel.send(
            content=content,
            embed=event_embed(event),
            allowed_mentions=allowed_mentions,
        )
        return message.id

    async def update_published_event(
        self,
        event: EventRecord,
        scheduled_event_id: int,
        announcement_message_id: int,
        role_id: int | None,
    ) -> None:
        guild = await self._guild()
        scheduled = await guild.fetch_scheduled_event(scheduled_event_id)
        if event.starts_at is None or not event.venue or not event.location:
            raise ValueError("Published events require a start time, venue, and location")
        starts_at = event.starts_at.astimezone(UTC)
        ends_at = (
            event.ends_at.astimezone(UTC)
            if event.ends_at
            else starts_at + timedelta(minutes=self.settings.default_event_duration_minutes)
        )
        await scheduled.edit(
            name=event.title[:100],
            description=self._scheduled_description(event)[:1000],
            start_time=starts_at,
            end_time=ends_at,
            location=f"{event.venue} — {event.location}"[:100],
            reason=f"Updated music event {event.id}",
        )
        channel = await self._announcement_channel()
        message = await channel.fetch_message(announcement_message_id)
        content = f"<@&{role_id}> Updated show listing" if role_id else "Updated show listing"
        await message.edit(
            content=content,
            embed=event_embed(event),
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                users=False,
                roles=[discord.Object(id=role_id)] if role_id else False,
                replied_user=False,
            ),
        )

    async def _guild(self) -> discord.Guild:
        if self.settings.discord_guild_id is None:
            raise RuntimeError("Discord guild ID is missing")
        guild = self.bot.get_guild(self.settings.discord_guild_id)
        if guild is None:
            guild = await self.bot.fetch_guild(self.settings.discord_guild_id)
        return guild

    async def _announcement_channel(self) -> discord.TextChannel:
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
