import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime

import database as db
from riot_api import riot_api, RiotAPIError
from config import RANK_ORDER, DIVISION_ORDER, TIMEZONE

AUTO_DELETE_SECONDS = 60


def sort_players(players: list[dict]) -> list[dict]:
    """Sort players by rank (highest first)."""
    def rank_value(player):
        tier = player.get("tier") or ""
        rank = player.get("rank") or ""
        lp = player.get("lp") or 0

        tier_val = RANK_ORDER.get(tier.upper(), -1)
        div_val = DIVISION_ORDER.get(rank.upper(), 0)

        # Combined score: tier * 1000 + division * 100 + lp
        return tier_val * 1000 + div_val * 100 + lp

    return sorted(players, key=rank_value, reverse=True)


def format_rank(tier: str, rank: str, lp: int) -> str:
    """Format rank for display."""
    if not tier:
        return "Unranked"

    # Master+ don't have divisions
    if tier.upper() in ("MASTER", "GRANDMASTER", "CHALLENGER"):
        return f"{tier.title()} ({lp} LP)"

    return f"{tier.title()} {rank} ({lp} LP)"


def get_lp_change_indicator(current_lp: int, prev_lp: int, tier: str, prev_tier: str = None) -> str:
    """Get LP change indicator arrow."""
    if prev_lp is None or prev_lp == 0:
        return ""

    # Simple LP comparison (doesn't account for tier changes perfectly, but good enough)
    if current_lp > prev_lp:
        return " ▲"
    elif current_lp < prev_lp:
        return " ▼"
    return ""


async def create_leaderboard_embed(leaderboard: dict, players: list[dict]) -> discord.Embed:
    """Create a Discord embed for a leaderboard."""
    sorted_players = sort_players(players)

    embed = discord.Embed(
        title=f"🏆 {leaderboard['name'].title()} Leaderboard",
        color=discord.Color.gold(),
        timestamp=datetime.now(TIMEZONE)
    )

    if not sorted_players:
        embed.description = "No players added yet. Use `/player add` to add players."
    else:
        lines = []
        for i, player in enumerate(sorted_players, 1):
            riot_id = player["riot_id"]
            rank_str = format_rank(player.get("tier"), player.get("rank"), player.get("lp", 0))
            indicator = get_lp_change_indicator(
                player.get("lp", 0),
                player.get("prev_lp", 0),
                player.get("tier")
            )

            # Add win/loss if available
            wins = player.get("wins", 0)
            losses = player.get("losses", 0)
            if wins or losses:
                winrate = round(wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
                lines.append(f"**{i}.** {riot_id}\n   {rank_str}{indicator} ({wins}W/{losses}L - {winrate}%)")
            else:
                lines.append(f"**{i}.** {riot_id}\n   {rank_str}{indicator}")

        embed.description = "\n\n".join(lines)

    embed.set_footer(text="Last updated")
    return embed


class TrackerCog(commands.Cog):
    """Cog for LP tracking commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # Leaderboard command group
    leaderboard_group = app_commands.Group(name="leaderboard", description="Manage leaderboards")

    @leaderboard_group.command(name="create", description="Create a new leaderboard")
    @app_commands.describe(
        name="Name for the leaderboard",
        channel="Channel for automatic updates (optional)"
    )
    async def leaderboard_create(
        self,
        interaction: discord.Interaction,
        name: str,
        channel: discord.TextChannel = None
    ):
        await interaction.response.defer()

        # Check if leaderboard already exists
        existing = await db.get_leaderboard(interaction.guild_id, name)
        if existing:
            msg = await interaction.followup.send(f"❌ A leaderboard named **{name}** already exists.", wait=True)
            await msg.delete(delay=AUTO_DELETE_SECONDS)
            return

        channel_id = channel.id if channel else None
        await db.create_leaderboard(interaction.guild_id, name, channel_id)

        text = f"✅ Created leaderboard **{name}**"
        if channel:
            text += f" with auto-updates in {channel.mention}"
        msg = await interaction.followup.send(text, wait=True)
        await msg.delete(delay=AUTO_DELETE_SECONDS)

    @leaderboard_group.command(name="delete", description="Delete a leaderboard")
    @app_commands.describe(name="Name of the leaderboard to delete")
    async def leaderboard_delete(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer()

        deleted = await db.delete_leaderboard(interaction.guild_id, name)
        if deleted:
            msg = await interaction.followup.send(f"✅ Deleted leaderboard **{name}** and all its players.", wait=True)
        else:
            msg = await interaction.followup.send(f"❌ Leaderboard **{name}** not found.", wait=True)
        await msg.delete(delay=AUTO_DELETE_SECONDS)

    @leaderboard_group.command(name="setchannel", description="Set auto-update channel for a leaderboard")
    @app_commands.describe(
        name="Name of the leaderboard",
        channel="Channel for automatic updates"
    )
    async def leaderboard_setchannel(
        self,
        interaction: discord.Interaction,
        name: str,
        channel: discord.TextChannel
    ):
        await interaction.response.defer()

        updated = await db.set_leaderboard_channel(interaction.guild_id, name, channel.id)
        if updated:
            msg = await interaction.followup.send(
                f"✅ Leaderboard **{name}** will now post updates to {channel.mention}",
                wait=True
            )
        else:
            msg = await interaction.followup.send(f"❌ Leaderboard **{name}** not found.", wait=True)
        await msg.delete(delay=AUTO_DELETE_SECONDS)

    @leaderboard_group.command(name="show", description="Display a leaderboard")
    @app_commands.describe(name="Name of the leaderboard to display")
    async def leaderboard_show(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer()

        leaderboard = await db.get_leaderboard(interaction.guild_id, name)
        if not leaderboard:
            msg = await interaction.followup.send(f"❌ Leaderboard **{name}** not found.", wait=True)
            await msg.delete(delay=AUTO_DELETE_SECONDS)
            return

        players = await db.get_leaderboard_players(leaderboard["id"])
        embed = await create_leaderboard_embed(leaderboard, players)
        msg = await interaction.followup.send(embed=embed, wait=True)
        await msg.delete(delay=AUTO_DELETE_SECONDS)

    @leaderboard_group.command(name="list", description="List all leaderboards in this server")
    async def leaderboard_list(self, interaction: discord.Interaction):
        await interaction.response.defer()

        leaderboards = await db.get_guild_leaderboards(interaction.guild_id)

        if not leaderboards:
            msg = await interaction.followup.send("No leaderboards found. Use `/leaderboard create` to create one.", wait=True)
            await msg.delete(delay=AUTO_DELETE_SECONDS)
            return

        embed = discord.Embed(
            title="📋 Server Leaderboards",
            color=discord.Color.blue()
        )

        for lb in leaderboards:
            players = await db.get_leaderboard_players(lb["id"])
            channel_info = f"<#{lb['channel_id']}>" if lb["channel_id"] else "Not set"
            embed.add_field(
                name=lb["name"].title(),
                value=f"Players: {len(players)}\nAuto-update channel: {channel_info}",
                inline=True
            )

        msg = await interaction.followup.send(embed=embed, wait=True)
        await msg.delete(delay=AUTO_DELETE_SECONDS)

    # Player command group
    player_group = app_commands.Group(name="player", description="Manage players")

    @player_group.command(name="add", description="Add a player to a leaderboard")
    @app_commands.describe(
        leaderboard="Name of the leaderboard",
        riot_id="Player's Riot ID (e.g., PlayerName#EUW)"
    )
    async def player_add(self, interaction: discord.Interaction, leaderboard: str, riot_id: str):
        await interaction.response.defer()

        # Validate leaderboard exists
        lb = await db.get_leaderboard(interaction.guild_id, leaderboard)
        if not lb:
            msg = await interaction.followup.send(f"❌ Leaderboard **{leaderboard}** not found.", wait=True)
            await msg.delete(delay=AUTO_DELETE_SECONDS)
            return

        # Check if player already exists
        existing = await db.get_player(lb["id"], riot_id)
        if existing:
            msg = await interaction.followup.send(f"❌ **{riot_id}** is already in **{leaderboard}**.", wait=True)
            await msg.delete(delay=AUTO_DELETE_SECONDS)
            return

        # Parse Riot ID
        if "#" not in riot_id:
            msg = await interaction.followup.send("❌ Invalid Riot ID format. Use `Name#Tag` (e.g., `PlayerName#EUW`)", wait=True)
            await msg.delete(delay=AUTO_DELETE_SECONDS)
            return

        game_name, tag_line = riot_id.rsplit("#", 1)

        # Fetch player info from Riot API
        try:
            player_info = await riot_api.get_player_full_info(game_name, tag_line)
        except RiotAPIError as e:
            if e.status == 404:
                msg = await interaction.followup.send(f"❌ Player **{riot_id}** not found. Check the name and tag.", wait=True)
            elif e.status == 403:
                msg = await interaction.followup.send("❌ Riot API key is invalid or expired. Please update it.", wait=True)
            else:
                msg = await interaction.followup.send(f"❌ Error fetching player: {e.message}", wait=True)
            await msg.delete(delay=AUTO_DELETE_SECONDS)
            return

        # Add player to database
        player_id = await db.add_player(
            lb["id"],
            f"{player_info['game_name']}#{player_info['tag_line']}",
            player_info["puuid"]
        )

        # Update rank if available
        ranked = player_info.get("ranked")
        if ranked:
            await db.update_player_rank(
                player_id,
                ranked["tier"],
                ranked["rank"],
                ranked["lp"],
                ranked["wins"],
                ranked["losses"]
            )
            rank_str = format_rank(ranked["tier"], ranked["rank"], ranked["lp"])
            msg = await interaction.followup.send(
                f"✅ Added **{player_info['game_name']}#{player_info['tag_line']}** to **{leaderboard}**\n"
                f"   Current rank: {rank_str}",
                wait=True
            )
        else:
            msg = await interaction.followup.send(
                f"✅ Added **{player_info['game_name']}#{player_info['tag_line']}** to **{leaderboard}**\n"
                f"   Current rank: Unranked",
                wait=True
            )
        await msg.delete(delay=AUTO_DELETE_SECONDS)

    @player_group.command(name="remove", description="Remove a player from a leaderboard")
    @app_commands.describe(
        leaderboard="Name of the leaderboard",
        riot_id="Player's Riot ID"
    )
    async def player_remove(self, interaction: discord.Interaction, leaderboard: str, riot_id: str):
        await interaction.response.defer()

        lb = await db.get_leaderboard(interaction.guild_id, leaderboard)
        if not lb:
            msg = await interaction.followup.send(f"❌ Leaderboard **{leaderboard}** not found.", wait=True)
            await msg.delete(delay=AUTO_DELETE_SECONDS)
            return

        removed = await db.remove_player(lb["id"], riot_id)
        if removed:
            msg = await interaction.followup.send(f"✅ Removed **{riot_id}** from **{leaderboard}**.", wait=True)
        else:
            msg = await interaction.followup.send(f"❌ **{riot_id}** not found in **{leaderboard}**.", wait=True)
        await msg.delete(delay=AUTO_DELETE_SECONDS)

    @app_commands.command(name="refresh", description="Manually refresh all player stats for a leaderboard")
    @app_commands.describe(leaderboard="Name of the leaderboard to refresh")
    async def refresh(self, interaction: discord.Interaction, leaderboard: str):
        await interaction.response.defer()

        lb = await db.get_leaderboard(interaction.guild_id, leaderboard)
        if not lb:
            msg = await interaction.followup.send(f"❌ Leaderboard **{leaderboard}** not found.", wait=True)
            await msg.delete(delay=AUTO_DELETE_SECONDS)
            return

        players = await db.get_leaderboard_players(lb["id"])
        if not players:
            msg = await interaction.followup.send(f"❌ No players in **{leaderboard}** to refresh.", wait=True)
            await msg.delete(delay=AUTO_DELETE_SECONDS)
            return

        updated = 0
        errors = 0

        for player in players:
            try:
                ranked = await riot_api.get_ranked_stats_by_puuid(player["puuid"])
                if ranked:
                    await db.update_player_rank(
                        player["id"],
                        ranked["tier"],
                        ranked["rank"],
                        ranked["lp"],
                        ranked["wins"],
                        ranked["losses"],
                        prev_lp=player.get("lp", 0)
                    )
                updated += 1
            except RiotAPIError:
                errors += 1

        # Show updated leaderboard
        players = await db.get_leaderboard_players(lb["id"])
        embed = await create_leaderboard_embed(lb, players)

        text = f"✅ Refreshed **{updated}** players"
        if errors:
            text += f" ({errors} errors)"

        msg = await interaction.followup.send(text, embed=embed, wait=True)
        await msg.delete(delay=AUTO_DELETE_SECONDS)


async def setup(bot: commands.Bot):
    await bot.add_cog(TrackerCog(bot))
