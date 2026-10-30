# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

LP Tracker is a Discord bot that tracks League of Legends ranked LP (League Points) for players on EUW. It uses Discord slash commands to manage leaderboards and players, with automatic periodic updates.

## Commands

**Run the bot:**
```bash
python bot.py
```

**Run tests:**
```bash
pytest                    # Run all tests
pytest tests/test_riot_api.py  # Run specific test file
pytest -k "test_name"     # Run tests matching pattern
```

**Install dependencies:**
```bash
pip install -r requirements.txt
```

## Architecture

### Core Components

- **bot.py** - Main entry point. `LPTrackerBot` class handles Discord connection, loads cogs, syncs slash commands, and runs a background task (`update_leaderboards`) that refreshes all leaderboard stats every hour (configurable via `UPDATE_INTERVAL`).

- **riot_api.py** - Async Riot Games API client (`RiotAPI` class). Handles account lookups by Riot ID, ranked stats by PUUID, match IDs/details/timelines. All requests go through one `RateLimiter` (sliding windows from `RIOT_RATE_LIMITS`) plus 429 retry. Uses Europe account API and EUW1 league API (other platforms via `get_league_entries`). Global singleton instance: `riot_api`.

- **database.py** - SQLite database layer using aiosqlite. Two tables: `leaderboards` (guild_id, name, channel_id, message_id) and `players` (linked to leaderboard, stores riot_id, puuid, rank data). All operations are async.

- **cogs/tracker.py** - Discord slash commands cog. Command groups: `/leaderboard` (create, delete, setchannel, show, list) and `/player` (add, remove). Also `/refresh` for manual updates. Contains `create_leaderboard_embed()` for rendering leaderboards and `sort_players()` for rank ordering.

- **config.py** - Environment config loaded via python-dotenv. Defines rank/division ordering constants for sorting.

- **sheets.py** / **cogs/sheet.py** - Optional team Google Sheet sync (`update_team_sheet` loop in bot.py, `/sheet sync`). Reads the roster from the `Skład` tab into one leaderboard, stores SoloQ/Flex games in the `matches` table, and writes stats to the `SoloQ` tab. `database.update_player_rank` also records `rank_history` snapshots used for the 7-day LP change. gspread is blocking, so sheet calls run in `asyncio.to_thread`.

- **team_games.py** / **cogs/scrims.py** / **drafts.py** - Team games: scrims arrive as `lp-tracker-scrims.json` from webhook messages (Windows script in `uploader/` reading the LoL client's local API; regular custom games aren't in the Riot API), officials (tournament codes) are fetched from the Riot API during the sheet sync. Games are normalized to one dict format, deduplicated by match ID (`team_games` table + `Mapy` "Game ID" column), linked to a `Mecze` row, and appended to `Mapy`. `drafts.py` rebuilds the `Drafty` tab from `Mapy`. Sheet columns are always found by row-4 header names.

- **availability.py** - Daily ping (`check_availability` loop in bot.py, from `AVAILABILITY_HOUR`) for `Skład` players with status Main whose `Dostępność` cells (nick header in row 5) for the next `AVAILABILITY_DAYS` days are empty (any text, also "nie" and "?", is an answer). Sent once a day on the reminders channel, deduplicated via `match_reminders`.

- **roasts.py** / **llm.py** / **cogs/roasts.py** - Comments on SoloQ games: `bot.comment_on_game` runs after each SoloQ notification, stores the game in `matches`, and `detect_trigger` picks one trigger (streak milestones, highlight, int, off-pool loss, streak end, night tilt). Text from OpenRouter (`llm.complete`, model in `OPENROUTER_MODEL`) or a template. Only roster players, max `ROASTS_PER_DAY`, `/roast off` opt-out (`roast_optout`).

- **members.py** - Roster nick → Discord user ID via `guild.query_members` (no privileged intent), cached in `discord_members`.

- **game_stats.py** - Full data of team games only (never SoloQ): raw API/client JSON + timeline gzipped in `team_game_raw`, per-player stats of both teams in `team_game_stats` (GD/CSD/XPD@10/15 vs lane opponent, KP, shares, vision, early deaths). Roles missing in custom games are inferred from the timeline. Uploader format v2 sends client timelines; `backfill_game_data` fills older games from `uploads/` during the sheet sync. **stats_tab.py** writes them to the bot-only `Statystyki` tab (player/role averages, then lane matchups: our champion vs the enemy one in the same role), every sheet sync and after uploads that add maps or timelines; `replace_tab(keep_last=True)` keeps it the sheet's last tab and `format_tab` rebuilds its formatting (number formats, colors, frozen rows) each time.

- **scouting.py** / **cogs/scouting.py** - `/scout <team> [players] [okres]`: opponents' Riot IDs from the user-edited `Przeciwnicy` tab (row-4 headers Drużyna / Multi EUW / Multi EUNE; the bot only creates it if missing), else `players` saved in `opponents`, else our games vs that `Mecze` opponent → per account: SoloQ games of the last 3/6/12 months (`SCOUT_MONTHS`, no game cap, shown as the whole period and its last 3 months; no Flex; accounts off EUW only when Master+, see `ranked_policy`) and, kept separate in the report, tournament-code games (own period: `PERIODS["quick"]` is SoloQ 1 month + tournaments 12 months) (`type=tourney`, queue 3130; Clash is left out on purpose), cached in `scout_games`; ranks from the account's own platform (EUW/EUNE); pro presence (picks+bans per game) and WR from gol.gg's champion list over the last `PRO_META_PATCHES` patches (allowed by its robots.txt, patch filter is a POST stored in its session, cached a day); `group_accounts` merges the only EUW and the only EUNE account of a main role into one player; ban suggestions; optional LLM summary. Runs in the background behind one lock, posts an embed and rewrites and formats (`sheet_format.py`, shared with Statystyki) the bot-only `Scouting` tab. Also started after the 24h/day reminder of a non-scrim match with known opponents.

- **reminders.py** - Match reminders from the sheet's `Mecze` tab (`check_match_reminders` loop in bot.py): planned matches with the `Przypomnienie` checkbox ticked (columns located by row-4 header names) get pings 24h and 1h before (untimed ones at 18:00 the day before), deduplicated in the `match_reminders` table.

### Data Flow

1. User adds player via `/player add` -> Riot API lookup -> store puuid + initial rank in SQLite
2. Background task runs hourly -> fetches fresh rank data for all players via puuid -> updates DB -> edits/sends embed message in configured channel
3. Leaderboard embeds show players sorted by rank (tier -> division -> LP) with LP change indicators (▲/▼)

### Key Patterns

- All Riot API calls go through the global `riot_api` instance
- Database module provides standalone async functions (no class wrapper)
- Discord cog uses `app_commands` for slash commands with `interaction.response.defer()` for operations that may take time
- Leaderboard names are case-insensitive (stored lowercase)
- Player lookups are case-insensitive

## Environment Variables

Copy `.env.example` to `.env` and configure:
- `DISCORD_TOKEN` - Discord bot token
- `RIOT_API_KEY` - Riot Games API key (the bot runs on a personal key, which doesn't expire; dev keys expire every 24 hours)
- `UPDATE_INTERVAL` - Seconds between auto-updates (default: 3600)
- `TIMEZONE` - Timezone for timestamps (default: UTC)
- `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` - LLM for game comments (templates without a key)
- `AVAILABILITY_HOUR`, `AVAILABILITY_DAYS`, `ROASTS_ENABLED`, `ROASTS_PER_DAY`, `SCOUT_MONTHS` - see `.env.example`
