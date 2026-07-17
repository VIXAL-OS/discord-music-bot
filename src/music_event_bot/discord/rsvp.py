from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import discord

from music_event_bot.discord.publishing import event_embed

if TYPE_CHECKING:
    from music_event_bot.discord.bot import MusicEventDiscordBot
    from music_event_bot.domain.models import EventRecord

logger = logging.getLogger(__name__)

RSVP_STATES: tuple[tuple[str, str, discord.ButtonStyle], ...] = (
    ("going", "Going", discord.ButtonStyle.success),
    ("interested", "Interested", discord.ButtonStyle.primary),
    ("declined", "Can't go", discord.ButtonStyle.secondary),
)
_MAX_NAMES = 10


def rsvp_summary(groups: dict[str, list[str]]) -> str | None:
    lines: list[str] = []
    for state, label, _style in RSVP_STATES:
        names = groups.get(state, [])
        if not names:
            continue
        shown = ", ".join(names[:_MAX_NAMES])
        extra = f" +{len(names) - _MAX_NAMES} more" if len(names) > _MAX_NAMES else ""
        lines.append(f"**{label} ({len(names)})**: {shown}{extra}")
    return "\n".join(lines) or None


def announcement_embed(
    event: EventRecord, groups: dict[str, list[str]]
) -> discord.Embed:
    embed = event_embed(event)
    summary = rsvp_summary(groups)
    if summary:
        embed.add_field(name="Who's in", value=summary[:1024], inline=False)
    return embed


class RsvpView(discord.ui.View):
    """Persistent Going / Interested / Can't go buttons on announcements.

    Anyone in the server may respond — RSVP is community input, not a
    reviewer action, so there is deliberately no permission gate.
    """

    def __init__(self, bot: MusicEventDiscordBot, event_id: str) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.event_id = event_id
        for state, label, style in RSVP_STATES:
            button: discord.ui.Button[RsvpView] = discord.ui.Button(
                label=label,
                style=style,
                custom_id=f"music-event:rsvp:{state}:{event_id}",
            )
            button.callback = self._callback_for(state)
            self.add_item(button)

    def _callback_for(
        self, state: str
    ) -> Callable[[discord.Interaction], Awaitable[None]]:
        async def callback(interaction: discord.Interaction) -> None:
            await self._record(interaction, state)

        return callback

    async def _record(self, interaction: discord.Interaction, state: str) -> None:
        user = interaction.user
        await self.bot.repository.upsert_rsvp(
            self.event_id, user.id, user.display_name, state
        )
        event = await self.bot.repository.get_event(self.event_id)
        if event is None:
            await interaction.response.send_message(
                "This event no longer exists.", ephemeral=True
            )
            return
        groups = await self.bot.repository.get_rsvps(self.event_id)
        # Editing via the interaction updates the card in place; content and
        # mentions are untouched, so nobody gets re-pinged.
        await interaction.response.edit_message(
            embed=announcement_embed(event, groups), view=self
        )
