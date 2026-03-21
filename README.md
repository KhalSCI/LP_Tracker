# LP Tracker

A Discord bot that tracks League of Legends ranked LP across your friend group. Create leaderboards, add players by Riot ID, and get automatic updates with win/loss notifications.

![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue)
![discord.py](https://img.shields.io/badge/discord.py-2.3+-blueviolet)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

## Features

- **Leaderboards** — Create multiple named leaderboards per server, each with its own channel for auto-updating rank embeds
- **Live rank tracking** — Pulls tier, division, LP, wins, losses, and winrate from the Riot API (Solo/Duo queue)
- **Win/loss notifications** — Per-leaderboard match notifications with champion, KDA, CS/min, and game duration
- **Slash commands** — Full Discord slash command interface with autocomplete
- **Auto-updates** — Background tasks refresh leaderboards hourly and check for new matches every 5 minutes (configurable)
- **SQLite storage** — Lightweight, no external database needed

## Requirements

- Python 3.10+
- A [Discord bot token](https://discord.com/developers/applications)
- A [Riot Games API key](https://developer.riotgames.com/)

## Setup

```bash
git clone https://github.com/KhalSCI/LP_Tracker.git
cd LP_Tracker
pip install -r requirements.txt
```

Create a `.env` file:

```env
DISCORD_TOKEN=your_discord_bot_token
RIOT_API_KEY=your_riot_api_key
UPDATE_INTERVAL=3600
NOTIFICATION_INTERVAL=300
TIMEZONE=Europe/Warsaw
```

`UPDATE_INTERVAL` is how often leaderboards refresh (seconds, default 3600). `NOTIFICATION_INTERVAL` is how often it checks for new matches (seconds, default 300).

Run:

```bash
python bot.py
```

### Running as a systemd service

A sample service file is included (`lp-tracker.service`). Edit the paths to match your setup, then:

```bash
sudo cp lp-tracker.service /etc/systemd/system/
sudo systemctl enable lp-tracker
sudo systemctl start lp-tracker
```

## Commands

### Leaderboard management

| Command | Description |
|---|---|
| `/leaderboard create <name> [channel]` | Create a leaderboard, optionally set an auto-update channel |
| `/leaderboard delete <name>` | Delete a leaderboard and all its players |
| `/leaderboard setchannel <name> <channel>` | Set or change the auto-update channel |
| `/leaderboard show <name>` | Display the current leaderboard |
| `/leaderboard list` | List all leaderboards in the server |

### Player management

| Command | Description |
|---|---|
| `/player add <leaderboard> <riot_id>` | Add a player (e.g. `PlayerName#EUW`) |
| `/player remove <leaderboard> <riot_id>` | Remove a player |
| `/refresh <leaderboard>` | Manually refresh all player stats |

### Notifications

| Command | Description |
|---|---|
| `/notifications setchannel <leaderboard> <channel>` | Enable win/loss notifications for a leaderboard |
| `/notifications disable <leaderboard>` | Disable notifications |
| `/notifications status` | Show notification settings for all leaderboards |

## How it works

The bot runs two background loops:

1. **Leaderboard updater** (hourly) — Fetches current ranked stats for every tracked player via the Riot API, updates the database, and edits the pinned leaderboard embed in the configured channel. Players are sorted by rank (tier + division + LP).

2. **Match checker** (every 5 min) — Polls recent ranked matches for all tracked players. When a new match is found, it sends a notification embed with the result, champion played, KDA, CS/min, and game duration. Matches are deduplicated per leaderboard to avoid spam.

## Architecture

```
bot.py              — Bot setup, background task loops
config.py           — Environment config, rank constants
riot_api.py         — Async Riot API client (account lookup, ranked stats, match history)
database.py         — SQLite schema, CRUD operations
cogs/
  tracker.py        — Leaderboard/player slash commands, embed formatting
  notifications.py  — Match notification commands and embeds
```

## API note

The free Riot API key (Development key) expires every 24 hours. For a permanent key, apply for a [Production API key](https://developer.riotgames.com/docs/portal#product-registration) or use a [Riot Games Third-Party Application](https://developer.riotgames.com/docs/portal#product-registration_personal-api-keys) personal key.

The bot is currently configured for the EUW region. To change the region, edit the API endpoints in `config.py`.

## License

MIT
