from __future__ import annotations

import discord

from music_event_bot.config import Settings


def is_reviewer(interaction: discord.Interaction, settings: Settings) -> bool:
    if interaction.guild_id != settings.discord_guild_id:
        return False
    if interaction.user.id in settings.admins:
        return True
    member = interaction.user
    if isinstance(member, discord.Member):
        return any(role.id in settings.reviewer_roles for role in member.roles)
    return False


async def require_reviewer(interaction: discord.Interaction, settings: Settings) -> bool:
    if is_reviewer(interaction, settings):
        return True
    message = "You are not authorized to review or publish music events."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
    return False
