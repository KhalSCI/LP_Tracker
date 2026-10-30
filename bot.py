import asyncio
import time
from datetime import datetime, timedelta, timezone
import discord
from discord.ext import commands, tasks

import database as db
from riot_api import riot_api, RiotAPIError
from sheets import team_sheet
from config import (
    DISCORD_TOKEN, UPDATE_INTERVAL, NOTIFICATION_INTERVAL, TIMEZONE,
    GOOGLE_SHEET_ID, SHEET_SYNC_INTERVAL, SHEET_GUILD_ID, SHEET_LEADERBOARD,
    REMINDER_CHANNEL_ID, REMINDER_ROLE_ID, REMINDER_CHECK_INTERVAL, AVAILABILITY_HOUR,
    ROASTS_ENABLED, ROASTS_PER_DAY,
)
from cogs.tracker import create_leaderboard_embed
from cogs.notifications import create_match_notification_embed
from cogs.sheet import sync_team_sheet, match_rows, SOLOQ_QUEUE, MATCH_HISTORY_DAYS, DAY_SECONDS
from reminders import parse_planned_matches, due_reminders, create_reminder_message
from team_games import sync_official_games, SCRIM_TYPE
import scouting
from game_stats import backfill_game_data
from drafts import update_drafts_tab
from stats_tab import update_stats_tab
from availability import AVAILABILITY_TAB, find_missing_availability, create_availability_message
from members import find_member_id, mention
from champions import champions
from roasts import is_soloq_game, detect_trigger, multikills, lane_opponent, write_comment

# notified_matches records older than this are deleted after each match check
NOTIFIED_MATCH_RETENTION_DAYS = 7
# Only matches started within this window are checked. Must stay shorter than the
# retention above, or a match whose record was cleaned up would be posted again.
MATCH_LOOKBACK_SECONDS = 24 * 60 * 60
# Reminders after which the opponent of an official is scouted (the day before the match)
SCOUTING_REMINDERS = ("|24h", "|day")
# The availability check runs once a day, from AVAILABILITY_HOUR; this is how often it looks at the clock
AVAILABILITY_CHECK_INTERVAL = 30 * 60


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
        await self.load_extension("cogs.sheet")
        await self.load_extension("cogs.scrims")
        await self.load_extension("cogs.roasts")
        await self.load_extension("cogs.scouting")

        # Sync slash commands
        await self.tree.sync()
        print("Slash commands synced!")

        # Start background tasks
        self.update_leaderboards.start()
        self.check_match_notifications.start()
        if GOOGLE_SHEET_ID:
            self.update_team_sheet.start()
            self.check_match_reminders.start()
            self.check_availability.start()
        else:
            print("Team sheet sync, match reminders and availability pings disabled (GOOGLE_SHEET_ID not set)")

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

        # Only fetch recent matches, so old ones are never seen again once cleaned up
        start_time = int(time.time()) - MATCH_LOOKBACK_SECONDS

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
            tracked_puuids = {p["puuid"] for p in players}

            for player in players:
                puuid = player["puuid"]

                try:
                    # Check cache for match IDs, otherwise fetch
                    if puuid not in player_matches_cache:
                        match_ids = await riot_api.get_match_ids_by_puuid(
                            puuid,
                            queue=420,  # Ranked Solo/Duo
                            count=5,
                            start_time=start_time
                        )
                        player_matches_cache[puuid] = match_ids
                        await asyncio.sleep(0.5)  # Rate limit
                    else:
                        match_ids = player_matches_cache[puuid]

                    # Oldest first, so streaks are counted with the earlier games already stored
                    for match_id in reversed(match_ids):
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
                        message = await channel.send(embed=embed)

                        # Mark as notified for this leaderboard
                        await db.mark_match_notified(leaderboard_id, match_id, puuid)
                        if not match_stats["remake"]:
                            try:
                                # Replies to the notification start a conversation about the game (cogs/roasts.py)
                                await db.create_chat(message.id, match_id, puuid)
                            except Exception as e:
                                print(f"Error starting the conversation about {match_id}: {e}")
                        await self.comment_on_game(message, player, match_data, match_stats, tracked_puuids)

                        # Small delay to avoid Discord rate limits
                        await asyncio.sleep(1)

                except RiotAPIError as e:
                    print(f"Error checking matches for {player['riot_id']}: {e}")

        # Periodic cleanup of old match records
        await db.cleanup_old_notified_matches(days=NOTIFIED_MATCH_RETENTION_DAYS)

    async def comment_on_game(self, message: discord.Message, player: dict, match_data: dict,
                              match_stats: dict, tracked_puuids: set[str]) -> None:
        """Reply to a SoloQ game notification with a comment (and a ping) when the game is worth one."""
        if not ROASTS_ENABLED or match_stats["queue_id"] != SOLOQ_QUEUE or match_stats["remake"]:
            return
        puuid = player["puuid"]
        try:
            # Stored right away (the sheet sync would add it within the hour) so streaks include it
            await db.save_matches(match_rows(match_stats["match_id"], match_data, tracked_puuids))
            if await db.is_roasted(match_stats["match_id"], puuid):
                return
            nick = await self.roster_nick(player)
            if nick is None:
                return
            midnight = datetime.now(TIMEZONE).replace(hour=0, minute=0, second=0, microsecond=0)
            if await db.count_roasts_since(puuid, midnight.astimezone(timezone.utc)) >= ROASTS_PER_DAY:
                return

            start = match_stats["game_start_timestamp"]
            history = [
                game for game in await db.get_matches_since([puuid], start - MATCH_HISTORY_DAYS * DAY_SECONDS * 1000)
                if is_soloq_game(game) and game["game_start"] <= start
            ]
            stats = dict(match_stats, **multikills(match_data, puuid))
            trigger = detect_trigger(stats, history)
            if trigger is None:
                return
            user_id = await find_member_id(message.guild, nick)
            if user_id and await db.is_roast_optout(user_id):
                return

            await champions.refresh()
            text = await write_comment(nick, player, stats, trigger, history, lane_opponent(match_data, puuid))
            reply = await message.reply(
                f"{mention(user_id, nick)} {text}",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=[discord.Object(user_id)] if user_id else False
                ),
            )
            await db.mark_roasted(match_stats["match_id"], puuid, trigger["kind"])
            print(f"Commented on {player['riot_id']}'s game {match_stats['match_id']}: {trigger['kind']}")
            chat = await db.get_chat_by_bot_message(message.id)
            if chat:
                await db.add_chat_message(chat["id"], reply.id, "comment", text)
        except Exception as e:
            print(f"Error commenting on {player['riot_id']}'s game: {e}")

    async def roster_nick(self, player: dict) -> str | None:
        """The player's nick from the roster sheet; None if not in it (e.g. an ex-member)."""
        entry = await self.roster_entry(player)
        return entry["nick"] if entry else None

    async def roster_entry(self, player: dict) -> dict | None:
        """The player's roster row ("nick", "role", ...); None if not in it (e.g. an ex-member)."""
        if not GOOGLE_SHEET_ID:
            return {"nick": player.get("discord_name") or player["riot_id"], "role": ""}
        for row in await self.roster():
            if row["riot_id"].lower() == player["riot_id"].lower():
                return dict(row, nick=row["nick"] or row["riot_id"])
        return None

    async def roster(self) -> list[dict]:
        """The roster sheet's rows as last read (empty without the sheet)."""
        return await team_sheet.read_roster_cached() if GOOGLE_SHEET_ID else []

    @check_match_notifications.before_loop
    async def before_match_check(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=SHEET_SYNC_INTERVAL)
    async def update_team_sheet(self):
        """Background task to sync the team Google Sheet (roster in, SoloQ stats out)."""
        print("Syncing team sheet...")
        try:
            summary = await sync_team_sheet()
            print(
                f"Synced team sheet: {summary['players']} players, "
                f"{summary['new_games']} new games, {summary['errors']} roster errors"
            )
        except Exception as e:
            print(f"Error syncing team sheet: {e}")

        try:
            summary = await sync_official_games()
            if summary["added"] or summary["unlinked"]:
                print(f"Official games: {len(summary['added'])} maps added, {len(summary['unlinked'])} without a match")
        except Exception as e:
            print(f"Error adding official games: {e}")

        try:
            filled = await backfill_game_data()
            if filled:
                print(f"Stored full data of {filled} earlier team games")
        except Exception as e:
            print(f"Error storing full data of earlier team games: {e}")

        try:
            maps = await update_drafts_tab()
            print(f"Updated drafts from {maps} maps")
        except Exception as e:
            print(f"Error updating drafts: {e}")

        try:
            await update_stats_tab()
        except Exception as e:
            print(f"Error updating stats: {e}")

    @update_team_sheet.before_loop
    async def before_sheet_sync(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=REMINDER_CHECK_INTERVAL)
    async def check_match_reminders(self):
        """Background task to remind about planned matches from the team sheet's "Mecze" tab."""
        try:
            channel = await self.get_reminder_channel()
            if not channel:
                return

            matches = parse_planned_matches(await team_sheet.read_matches())
            now = datetime.now(TIMEZONE)
            allowed_mentions = discord.AllowedMentions(
                everyone=False,
                users=False,
                roles=[discord.Object(REMINDER_ROLE_ID)] if REMINDER_ROLE_ID else False
            )

            for key, match in due_reminders(matches, now):
                if await db.is_reminder_sent(key):
                    continue
                content, embed = create_reminder_message(match, now, REMINDER_ROLE_ID)
                await channel.send(content, embed=embed, allowed_mentions=allowed_mentions)
                await db.mark_reminder_sent(key)
                print(f"Sent match reminder: {key}")
                if key.endswith(SCOUTING_REMINDERS) and match["opponent"] and match["type"] != SCRIM_TYPE:
                    await self.scout_opponent(channel, match["opponent"])
        except Exception as e:
            print(f"Error checking match reminders: {e}")

    async def scout_opponent(self, channel, opponent: str) -> None:
        """Start scouting an official's opponent when its players are known (Przeciwnicy tab, saved, or our games)."""
        players, _, roles = await scouting.known_players(opponent)
        if players:
            scouting.start_scouting(channel, opponent, players, roles=roles)
        else:
            print(f"Scouting: no players known for {opponent}, add them to the Przeciwnicy tab")

    @check_match_reminders.before_loop
    async def before_reminder_check(self):
        await self.wait_until_ready()

        channel = await self.get_reminder_channel()
        if not channel:
            print("Match reminders: no channel found (set REMINDER_CHANNEL_ID or the team notification channel)")
            return
        print(f"Match reminders go to #{channel.name}")
        role = channel.guild.get_role(REMINDER_ROLE_ID) if REMINDER_ROLE_ID else None
        if REMINDER_ROLE_ID and not role:
            print(f"Match reminders: role {REMINDER_ROLE_ID} not found in {channel.guild.name}")
        elif role and not role.mentionable and not channel.permissions_for(channel.guild.me).mention_everyone:
            print(f"Match reminders: role @{role.name} is not mentionable and the bot can't mention all roles")

    @tasks.loop(seconds=AVAILABILITY_CHECK_INTERVAL)
    async def check_availability(self):
        """Once a day from AVAILABILITY_HOUR: ping Main players without availability for the next days."""
        now = datetime.now(TIMEZONE)
        key = f"availability|{now:%Y-%m-%d}"
        if now.hour < AVAILABILITY_HOUR or await db.is_reminder_sent(key):
            return
        try:
            channel = await self.get_reminder_channel()
            if not channel:
                return
            pinged = await self.send_availability_reminder(channel, now.date())
            await db.mark_reminder_sent(key)
            print(f"Availability check: {', '.join(pinged) if pinged else 'everyone filled in'}")
        except Exception as e:
            print(f"Error checking availability: {e}")

    @check_availability.before_loop
    async def before_availability_check(self):
        await self.wait_until_ready()

    async def send_availability_reminder(self, channel, today) -> list[str]:
        """Post the availability reminder if someone is missing. Returns the pinged nicks."""
        missing = await find_missing_availability(today)
        if not missing:
            return []
        user_ids = {nick: await find_member_id(channel.guild, nick) for nick in missing}
        content = create_availability_message(
            [(mention(user_ids[nick], nick), days) for nick, days in missing.items()],
            await team_sheet.tab_url(AVAILABILITY_TAB),
        )
        await channel.send(content, allowed_mentions=discord.AllowedMentions(
            everyone=False, roles=False, users=[discord.Object(i) for i in user_ids.values() if i]
        ))
        return list(missing)

    async def get_reminder_channel(self):
        """The channel for match reminders: REMINDER_CHANNEL_ID, else the team leaderboard's notification channel."""
        channel_id = REMINDER_CHANNEL_ID
        if not channel_id:
            lb = await db.get_leaderboard(SHEET_GUILD_ID, SHEET_LEADERBOARD)
            channel_id = lb.get("notification_channel_id") if lb else None
        return self.get_channel(channel_id) if channel_id else None


def main():
    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN not found in environment variables.")
        print("Please copy .env.example to .env and fill in your tokens.")
        return

    bot = LPTrackerBot()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
