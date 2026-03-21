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

    # CS/min field
    embed.add_field(
        name="CS/min",
        value=str(match_stats["cs_per_min"]),
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
    """Cog for win/loss notification commands (per-leaderboard)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    notifications_group = app_commands.Group(
        name="notifications",
        description="Manage win/loss notifications"
    )

    async def leaderboard_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for leaderboard names."""
        leaderboards = await db.get_guild_leaderboards(interaction.guild_id)
        choices = [
            app_commands.Choice(name=lb["name"], value=lb["name"])
            for lb in leaderboards
            if current.lower() in lb["name"].lower()
        ]
        return choices[:25]  # Discord limit

    @notifications_group.command(name="setchannel", description="Set the notification channel for a leaderboard")
    @app_commands.describe(
        leaderboard="Name of the leaderboard",
        channel="Channel for win/loss notifications"
    )
    @app_commands.autocomplete(leaderboard=leaderboard_autocomplete)
    async def set_channel(
        self,
        interaction: discord.Interaction,
        leaderboard: str,
        channel: discord.TextChannel
    ):
        await interaction.response.defer()

        # Check if leaderboard exists
        lb = await db.get_leaderboard(interaction.guild_id, leaderboard)
        if not lb:
            msg = await interaction.followup.send(
                f"Leaderboard '{leaderboard}' not found.",
                wait=True
            )
            await msg.delete(delay=AUTO_DELETE_SECONDS)
            return

        success = await db.set_leaderboard_notification_channel(
            interaction.guild_id,
            leaderboard,
            channel.id
        )

        if success:
            msg = await interaction.followup.send(
                f"Win/loss notifications for **{leaderboard}** will be sent to {channel.mention}",
                wait=True
            )
        else:
            msg = await interaction.followup.send(
                f"Failed to set notification channel for '{leaderboard}'.",
                wait=True
            )
        await msg.delete(delay=AUTO_DELETE_SECONDS)

    @notifications_group.command(name="disable", description="Disable notifications for a leaderboard")
    @app_commands.describe(leaderboard="Name of the leaderboard")
    @app_commands.autocomplete(leaderboard=leaderboard_autocomplete)
    async def disable(self, interaction: discord.Interaction, leaderboard: str):
        await interaction.response.defer()

        disabled = await db.disable_leaderboard_notifications(interaction.guild_id, leaderboard)
        if disabled:
            msg = await interaction.followup.send(
                f"Notifications for **{leaderboard}** have been disabled.",
                wait=True
            )
        else:
            msg = await interaction.followup.send(
                f"Leaderboard '{leaderboard}' not found or notifications were not enabled.",
                wait=True
            )
        await msg.delete(delay=AUTO_DELETE_SECONDS)

    @notifications_group.command(name="status", description="Show notification settings for all leaderboards")
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer()

        leaderboards = await db.get_guild_leaderboards(interaction.guild_id)

        if not leaderboards:
            msg = await interaction.followup.send(
                "**Notification Status**\n"
                "No leaderboards found. Create one with `/leaderboard create`.",
                wait=True
            )
            await msg.delete(delay=AUTO_DELETE_SECONDS)
            return

        status_lines = ["**Notification Status**\n"]

        for lb in leaderboards:
            name = lb["name"]
            channel_id = lb.get("notification_channel_id")
            enabled = lb.get("notifications_enabled", False)

            if channel_id and enabled:
                channel_mention = f"<#{channel_id}>"
                players = await db.get_leaderboard_players(lb["id"])
                status_lines.append(f"**{name}**: {channel_mention} ({len(players)} players)")
            else:
                status_lines.append(f"**{name}**: Disabled")

        msg = await interaction.followup.send("\n".join(status_lines), wait=True)
        await msg.delete(delay=AUTO_DELETE_SECONDS)


async def setup(bot: commands.Bot):
    await bot.add_cog(NotificationsCog(bot))
