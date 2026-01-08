import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime

import database as db
from config import TIMEZONE

AUTO_DELETE_SECONDS = 60


def create_match_notification_embed(riot_id: str, match_stats: dict) -> discord.Embed:
    """Create a Discord embed for a match notification."""
    win = match_stats["win"]

    embed = discord.Embed(
        title="VICTORY!" if win else "DEFEAT",
        color=discord.Color.green() if win else discord.Color.red(),
        timestamp=datetime.now(TIMEZONE)
    )

    outcome_text = "won" if win else "lost"
    embed.description = f"**{riot_id}** just {outcome_text} a ranked game!"

    # Champion field
    embed.add_field(
        name="Champion",
        value=match_stats["champion_name"],
        inline=True
    )

    # KDA field
    kills = match_stats["kills"]
    deaths = match_stats["deaths"]
    assists = match_stats["assists"]
    kda_ratio = match_stats["kda_ratio"]
    embed.add_field(
        name="KDA",
        value=f"{kills}/{deaths}/{assists} ({kda_ratio})",
        inline=True
    )

    # CS field
    embed.add_field(
        name="CS",
        value=str(match_stats["cs"]),
        inline=True
    )

    # Duration field
    duration = match_stats["game_duration_minutes"]
    embed.add_field(
        name="Duration",
        value=f"{duration} min",
        inline=True
    )

    # Add champion icon from Data Dragon CDN
    champion_name = match_stats["champion_name"]
    icon_url = f"https://ddragon.leagueoflegends.com/cdn/14.24.1/img/champion/{champion_name}.png"
    embed.set_thumbnail(url=icon_url)

    return embed


class NotificationsCog(commands.Cog):
    """Cog for win/loss notification commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    notifications_group = app_commands.Group(
        name="notifications",
        description="Manage win/loss notifications"
    )

    @notifications_group.command(name="setchannel", description="Set the channel for win/loss notifications")
    @app_commands.describe(channel="Channel for win/loss notifications")
    async def set_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel
    ):
        await interaction.response.defer()

        await db.set_notification_channel(interaction.guild_id, channel.id)
        msg = await interaction.followup.send(
            f"✅ Win/loss notifications will be sent to {channel.mention}",
            wait=True
        )
        await msg.delete(delay=AUTO_DELETE_SECONDS)

    @notifications_group.command(name="disable", description="Disable win/loss notifications")
    async def disable(self, interaction: discord.Interaction):
        await interaction.response.defer()

        disabled = await db.disable_notifications(interaction.guild_id)
        if disabled:
            msg = await interaction.followup.send("✅ Win/loss notifications have been disabled.", wait=True)
        else:
            msg = await interaction.followup.send("❌ Notifications were not enabled for this server.", wait=True)
        await msg.delete(delay=AUTO_DELETE_SECONDS)

    @notifications_group.command(name="status", description="Show current notification settings")
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer()

        settings = await db.get_guild_settings(interaction.guild_id)

        if not settings or not settings.get("notification_channel_id"):
            msg = await interaction.followup.send(
                "**Notification Status**\n"
                "Channel: Not configured\n"
                "Use `/notifications setchannel` to enable notifications.",
                wait=True
            )
            await msg.delete(delay=AUTO_DELETE_SECONDS)
            return

        channel_id = settings["notification_channel_id"]
        enabled = settings.get("notifications_enabled", False)

        status_text = "Enabled" if enabled else "Disabled"
        channel_mention = f"<#{channel_id}>"

        # Count tracked players
        players = await db.get_unique_players_for_guild(interaction.guild_id)

        msg = await interaction.followup.send(
            f"**Notification Status**\n"
            f"Channel: {channel_mention}\n"
            f"Status: {status_text}\n"
            f"Tracking: {len(players)} unique player(s)",
            wait=True
        )
        await msg.delete(delay=AUTO_DELETE_SECONDS)


async def setup(bot: commands.Bot):
    await bot.add_cog(NotificationsCog(bot))
