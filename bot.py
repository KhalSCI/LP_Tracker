import asyncio
import discord
from discord.ext import commands, tasks

import database as db
from riot_api import riot_api, RiotAPIError
from config import DISCORD_TOKEN, UPDATE_INTERVAL, NOTIFICATION_INTERVAL
from cogs.tracker import create_leaderboard_embed
from cogs.notifications import create_match_notification_embed


class LPTrackerBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(
            command_prefix="!",  # Not used, but required
            intents=intents
        )

    async def setup_hook(self):
        # Initialize database
        await db.init_db()

        # Load cogs
        await self.load_extension("cogs.tracker")
        await self.load_extension("cogs.notifications")

        # Sync slash commands
        await self.tree.sync()
        print("Slash commands synced!")

        # Start background tasks
        self.update_leaderboards.start()
        self.check_match_notifications.start()

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print(f"Connected to {len(self.guilds)} guild(s)")
        # Sync commands to all guilds for immediate availability
        for guild in self.guilds:
            try:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                print(f"Synced commands to {guild.name}")
            except Exception as e:
                print(f"Failed to sync to {guild.name}: {e}")
        print("------")

    async def close(self):
        await riot_api.close()
        await super().close()

    @tasks.loop(seconds=UPDATE_INTERVAL)
    async def update_leaderboards(self):
        """Background task to update all leaderboards hourly."""
        print("Running scheduled leaderboard update...")

        leaderboards = await db.get_all_leaderboards_with_channels()

        for lb in leaderboards:
            try:
                channel = self.get_channel(lb["channel_id"])
                if not channel:
                    continue

                players = await db.get_leaderboard_players(lb["id"])
                if not players:
                    continue

                # Update each player's stats
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
                    except RiotAPIError as e:
                        print(f"Error updating {player['riot_id']}: {e}")

                    # Small delay to respect rate limits
                    await asyncio.sleep(0.5)

                # Fetch updated players and post/edit embed
                players = await db.get_leaderboard_players(lb["id"])
                embed = await create_leaderboard_embed(lb, players)

                # Try to edit existing message, or send new one
                message_id = lb.get("message_id")
                if message_id:
                    try:
                        message = await channel.fetch_message(message_id)
                        await message.edit(embed=embed)
                        print(f"Edited leaderboard '{lb['name']}' in guild {lb['guild_id']}")
                    except discord.NotFound:
                        # Message was deleted, send a new one
                        message = await channel.send(embed=embed)
                        await db.set_leaderboard_message(lb["id"], message.id)
                        print(f"Re-sent leaderboard '{lb['name']}' (old message deleted)")
                else:
                    # First time posting, send new message
                    message = await channel.send(embed=embed)
                    await db.set_leaderboard_message(lb["id"], message.id)
                    print(f"Sent new leaderboard '{lb['name']}' in guild {lb['guild_id']}")

            except Exception as e:
                print(f"Error updating leaderboard {lb['name']}: {e}")

    @update_leaderboards.before_loop
    async def before_update(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=NOTIFICATION_INTERVAL)
    async def check_match_notifications(self):
        """Background task to check for new matches and send notifications (per-leaderboard)."""
        print("Checking for new matches...")

        # Get all leaderboards with notification channels enabled
        leaderboards = await db.get_leaderboards_with_notifications()

        # Cache match data to avoid redundant API calls for same player
        match_cache = {}  # {match_id: match_data}
        player_matches_cache = {}  # {puuid: [match_ids]}

        for lb in leaderboards:
            leaderboard_id = lb["id"]
            channel_id = lb["notification_channel_id"]
            enabled_at = lb.get("notifications_enabled_at")

            # Convert enabled_at to milliseconds timestamp for comparison with Riot API
            if enabled_at:
                from datetime import datetime
                if isinstance(enabled_at, str):
                    enabled_at_dt = datetime.fromisoformat(enabled_at)
                else:
                    enabled_at_dt = enabled_at
                enabled_at_ms = int(enabled_at_dt.timestamp() * 1000)
            else:
                enabled_at_ms = 0

            channel = self.get_channel(channel_id)
            if not channel:
                continue

            # Get players for this leaderboard
            players = await db.get_players_for_leaderboard_notifications(leaderboard_id)

            for player in players:
                puuid = player["puuid"]

                try:
                    # Check cache for match IDs, otherwise fetch
                    if puuid not in player_matches_cache:
                        match_ids = await riot_api.get_match_ids_by_puuid(
                            puuid,
                            queue=420,  # Ranked Solo/Duo
                            count=5
                        )
                        player_matches_cache[puuid] = match_ids
                        await asyncio.sleep(0.5)  # Rate limit
                    else:
                        match_ids = player_matches_cache[puuid]

                    for match_id in match_ids:
                        # Check if already notified for THIS leaderboard
                        if await db.is_match_notified(leaderboard_id, match_id, puuid):
                            continue

                        # Check cache for match details, otherwise fetch
                        if match_id not in match_cache:
                            match_data = await riot_api.get_match_details(match_id)
                            match_cache[match_id] = match_data
                            await asyncio.sleep(0.5)  # Rate limit
                        else:
                            match_data = match_cache[match_id]

                        match_stats = riot_api.extract_player_match_stats(
                            match_data,
                            puuid
                        )

                        if not match_stats:
                            continue

                        # Skip matches that ended before notifications were enabled
                        if match_stats["game_end_timestamp"] < enabled_at_ms:
                            # Mark as notified to avoid checking again
                            await db.mark_match_notified(leaderboard_id, match_id, puuid)
                            continue

                        # Create and send notification
                        embed = create_match_notification_embed(
                            player["riot_id"],
                            match_stats
                        )
                        await channel.send(embed=embed)

                        # Mark as notified for this leaderboard
                        await db.mark_match_notified(leaderboard_id, match_id, puuid)

                        # Small delay to avoid Discord rate limits
                        await asyncio.sleep(1)

                except RiotAPIError as e:
                    print(f"Error checking matches for {player['riot_id']}: {e}")

        # Periodic cleanup of old match records
        await db.cleanup_old_notified_matches(days=7)

    @check_match_notifications.before_loop
    async def before_match_check(self):
        await self.wait_until_ready()


def main():
    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN not found in environment variables.")
        print("Please copy .env.example to .env and fill in your tokens.")
        return

    bot = LPTrackerBot()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
