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

- **riot_api.py** - Async Riot Games API client (`RiotAPI` class). Handles account lookups by Riot ID and ranked stats by PUUID. Includes automatic rate limit handling (429 retry). Uses Europe account API and EUW1 league API. Global singleton instance: `riot_api`.

- **database.py** - SQLite database layer using aiosqlite. Two tables: `leaderboards` (guild_id, name, channel_id, message_id) and `players` (linked to leaderboard, stores riot_id, puuid, rank data). All operations are async.

- **cogs/tracker.py** - Discord slash commands cog. Command groups: `/leaderboard` (create, delete, setchannel, show, list) and `/player` (add, remove). Also `/refresh` for manual updates. Contains `create_leaderboard_embed()` for rendering leaderboards and `sort_players()` for rank ordering.

- **config.py** - Environment config loaded via python-dotenv. Defines rank/division ordering constants for sorting.

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
- `RIOT_API_KEY` - Riot Games API key (dev keys expire every 24 hours)
- `UPDATE_INTERVAL` - Seconds between auto-updates (default: 3600)
- `TIMEZONE` - Timezone for timestamps (default: UTC)
