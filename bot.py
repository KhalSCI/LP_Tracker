import asyncio
import discord
from discord.ext import commands, tasks

import database as db
from riot_api import riot_api, RiotAPIError
from config import DISCORD_TOKEN, UPDATE_INTERVAL
from cogs.tracker import create_leaderboard_embed


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

        # Sync slash commands
        await self.tree.sync()
        print("Slash commands synced!")

        # Start background task
        self.update_leaderboards.start()

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print(f"Connected to {len(self.guilds)} guild(s)")
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


def main():
    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN not found in environment variables.")
        print("Please copy .env.example to .env and fill in your tokens.")
        return

    bot = LPTrackerBot()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
