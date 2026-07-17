from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import discord
from dateutil.parser import parse as parse_datetime

from music_event_bot.discord.permissions import require_reviewer
from music_event_bot.discord.publishing import event_embed
from music_event_bot.domain.models import EventRecord, EventStatus

if TYPE_CHECKING:
    from music_event_bot.discord.bot import MusicEventDiscordBot


def review_embed(event: EventRecord) -> discord.Embed:
    embed = event_embed(event, pending=True)
    embed.title = f"Review: {event.title}"
    embed.add_field(name="Status", value=event.status.value, inline=True)
    if event.match_reasons:
        embed.add_field(
            name="Why it matched", value="\n".join(event.match_reasons), inline=False
        )
    if not event.is_complete:
        embed.add_field(
            name="Missing before approval",
            value="Title, venue, location, and a start time are required.",
            inline=False,
        )
    return embed


class EventReviewView(discord.ui.View):
    def __init__(self, bot: MusicEventDiscordBot, event_id: str) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.event_id = event_id

        approve = discord.ui.Button(
            label="Approve & publish",
            style=discord.ButtonStyle.success,
            custom_id=f"music-event:approve:{event_id}",
        )
        approve.callback = self._approve
        self.add_item(approve)

        edit = discord.ui.Button(
            label="Edit",
            style=discord.ButtonStyle.primary,
            custom_id=f"music-event:edit:{event_id}",
        )
        edit.callback = self._edit
        self.add_item(edit)

        reject = discord.ui.Button(
            label="Reject",
            style=discord.ButtonStyle.danger,
            custom_id=f"music-event:reject:{event_id}",
        )
        reject.callback = self._reject
        self.add_item(reject)

        retry = discord.ui.Button(
            label="Retry publish",
            style=discord.ButtonStyle.secondary,
            custom_id=f"music-event:retry:{event_id}",
        )
        retry.callback = self._retry
        self.add_item(retry)

    async def _approve(self, interaction: discord.Interaction) -> None:
        if not await require_reviewer(interaction, self.bot.settings):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.bot.repository.approve(self.event_id, interaction.user.id)
            event = await self.bot.publish_or_queue(self.event_id)
        except Exception as exc:
            await interaction.followup.send(f"Approval failed: {exc}", ephemeral=True)
            await self.bot.refresh_review_message(self.event_id)
            return
        await interaction.followup.send(self.bot.queue_note(event), ephemeral=True)
        await interaction.message.edit(embed=review_embed(event), view=None)

    async def _retry(self, interaction: discord.Interaction) -> None:
        if not await require_reviewer(interaction, self.bot.settings):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.bot.repository.approve(self.event_id, interaction.user.id)
            event = await self.bot.publication_service.publish(self.event_id)
        except Exception as exc:
            await interaction.followup.send(f"Retry failed: {exc}", ephemeral=True)
            return
        await interaction.followup.send("Publication retry succeeded.", ephemeral=True)
        await interaction.message.edit(embed=review_embed(event), view=None)

    async def _edit(self, interaction: discord.Interaction) -> None:
        if not await require_reviewer(interaction, self.bot.settings):
            return
        event = await self.bot.repository.get_event(self.event_id)
        if event is None:
            await interaction.response.send_message("Event no longer exists.", ephemeral=True)
            return
        await interaction.response.send_modal(EditEventModal(self.bot, event))

    async def _reject(self, interaction: discord.Interaction) -> None:
        if not await require_reviewer(interaction, self.bot.settings):
            return
        await interaction.response.send_modal(RejectEventModal(self.bot, self.event_id))


class EditEventModal(discord.ui.Modal):
    def __init__(self, bot: MusicEventDiscordBot, event: EventRecord) -> None:
        super().__init__(title="Edit music event", timeout=600)
        self.bot = bot
        self.event = event
        self.title_input = discord.ui.TextInput(
            label="Title",
            default=event.title[:4000],
            required=True,
            max_length=100,
        )
        self.venue_input = discord.ui.TextInput(
            label="Venue",
            default=(event.venue or "")[:4000],
            required=True,
            max_length=100,
        )
        self.location_input = discord.ui.TextInput(
            label="Full location/address",
            default=(event.location or "")[:4000],
            required=True,
            max_length=200,
        )
        start_default = (
            event.starts_at.astimezone(bot.settings.timezone).strftime("%Y-%m-%d %H:%M")
            if event.starts_at
            else ""
        )
        self.start_input = discord.ui.TextInput(
            label=f"Start time ({bot.settings.default_timezone})",
            default=start_default,
            placeholder="2026-09-12 20:00",
            required=True,
            max_length=40,
        )
        self.genres_input = discord.ui.TextInput(
            label="Genres (comma-separated)",
            default=", ".join(event.genres)[:4000],
            required=False,
            max_length=200,
        )
        for item in (
            self.title_input,
            self.venue_input,
            self.location_input,
            self.start_input,
            self.genres_input,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await require_reviewer(interaction, self.bot.settings):
            return
        try:
            start = parse_datetime(str(self.start_input.value))
            if start.tzinfo is None:
                start = start.replace(tzinfo=self.bot.settings.timezone)
        except (ValueError, OverflowError) as exc:
            await interaction.response.send_message(
                f"Could not parse the start time: {exc}", ephemeral=True
            )
            return
        duration = timedelta(minutes=self.bot.settings.default_event_duration_minutes)
        if self.event.starts_at and self.event.ends_at:
            duration = self.event.ends_at - self.event.starts_at
        updated = await self.bot.repository.update_event(
            self.event.id,
            title=str(self.title_input.value),
            venue=str(self.venue_input.value),
            location=str(self.location_input.value),
            starts_at=start,
            ends_at=start + duration,
            timezone=self.bot.settings.default_timezone,
            genres=tuple(
                value.strip()
                for value in str(self.genres_input.value).split(",")
                if value.strip()
            ),
        )
        if updated.status == EventStatus.PUBLISHED:
            await self.bot.publication_service.update_existing(updated.id)
        await interaction.response.send_message("Event updated.", ephemeral=True)
        await self.bot.refresh_review_message(updated.id)


class RejectEventModal(discord.ui.Modal):
    def __init__(self, bot: MusicEventDiscordBot, event_id: str) -> None:
        super().__init__(title="Reject music event", timeout=600)
        self.bot = bot
        self.event_id = event_id
        self.reason = discord.ui.TextInput(
            label="Reason (optional)",
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=500,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await require_reviewer(interaction, self.bot.settings):
            return
        await self.bot.repository.reject(
            self.event_id,
            interaction.user.id,
            str(self.reason.value).strip() or None,
        )
        event = await self.bot.repository.get_event(self.event_id)
        await interaction.response.send_message("Event rejected.", ephemeral=True)
        if event and interaction.message:
            await interaction.message.edit(embed=review_embed(event), view=None)
