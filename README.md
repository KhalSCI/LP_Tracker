# LP Tracker

A Discord bot that tracks League of Legends ranked LP across your friend group. Create leaderboards, add players by Riot ID, and get automatic updates with win/loss notifications.

![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue)
![discord.py](https://img.shields.io/badge/discord.py-2.3+-blueviolet)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

## Features

- **Leaderboards** — Create multiple named leaderboards per server, each with its own channel for auto-updating rank embeds
- **Live rank tracking** — Pulls tier, division, LP, wins, losses, and winrate from the Riot API (Solo/Duo queue)
- **Win/loss notifications** — Per-leaderboard match notifications with champion, KDA, CS/min, and game duration
- **Comments on SoloQ games** — Streaks, int games, pentakills and off-pool picks get a short ironic comment (written by a cheap LLM through OpenRouter, or from templates) that pings the player
- **Slash commands** — Full Discord slash command interface with autocomplete
- **Auto-updates** — Background tasks refresh leaderboards hourly and check for new matches every 5 minutes (configurable)
- **Team Google Sheet sync** (optional) — Takes the roster from a Google Sheet and writes ranks, weekly activity, LP change, recent champions and games played together back to it; reminds about matches and missing availability, and keeps full data of scrims and officials
- **Opponent scouting** — `/scout` (and automatically the day before an official) reports each opponent's champions this and last season with games and winrate, tournament games, suggested bans and pro-play priority from gol.gg
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

### Team Google Sheet (optional)

The bot can keep one leaderboard in sync with a team spreadsheet:

1. In Google Cloud, enable the Google Sheets API, create a service account and download its JSON key. Keep the key outside the repo, by default at `~/.config/lp-tracker/service_account.json` (or point `GOOGLE_APPLICATION_CREDENTIALS` at it).
2. Share the spreadsheet with the service account's e-mail as Editor.
3. Add to `.env`:

```env
GOOGLE_SHEET_ID=the_id_from_the_sheet_url
SHEET_GUILD_ID=your_discord_server_id
SHEET_LEADERBOARD=name_of_the_team_leaderboard
SHEET_SYNC_INTERVAL=3600
```

Every `SHEET_SYNC_INTERVAL` seconds the bot:

- reads the roster from the `Skład` tab (rows 8–19: A role, B Discord nick, E EUW Riot ID) and adds new Riot IDs to the leaderboard and updates nicks. Players removed from the sheet stay on the leaderboard (e.g. ex-teammates still followed there) until removed with `/player remove`. Rows with a wrong Riot ID or a non-EUW account show an error in the sheet instead.
- fetches the players' SoloQ and Flex games (the last 30 days on the first run, then only new ones).
- writes the `SoloQ` tab: rank, LP, season W/L, games and W-L in the last 7 days, LP change over 7 days, top 3 SoloQ champions of the last 30 days with winrate, and a table of games where team members played on the same team (SoloQ duo and Flex).

Ranks come from the leaderboard updates, so the team leaderboard needs an auto-update channel. Riot doesn't provide LP history, so the bot records rank snapshots itself; the 7-day LP change is complete one week after the first run.

#### Match reminders

With the sheet configured, the bot also reads the `Mecze` tab every `REMINDER_CHECK_INTERVAL` seconds (default 300) and posts a reminder for each match whose result says "Zaplanowany" and whose `Przypomnienie` checkbox is ticked (columns are found by their row-4 headers: `Data`, `Godz.`, `Typ`, `Przeciwnik`, `Turniej / nazwa`, `Rezultat`, `Przypomnienie`, `Notatki`): 24 hours and 1 hour before the start, or at 18:00 the day before if the match has no time yet. Unticked matches stay in the sheet without reminders, e.g. qualifier dates you may not play. Each reminder is sent once; changing a match's date, time or name schedules new ones.

```env
REMINDER_ROLE_ID=role_to_ping       # empty = no ping
REMINDER_CHANNEL_ID=                # empty = the team leaderboard's notification channel
```

The role is pinged only if it is mentionable or the bot has the "Mention @everyone, @here and All Roles" permission.

#### Availability pings

Once a day from `AVAILABILITY_HOUR` (default 12:00) the bot checks the `Dostępność` tab for the next `AVAILABILITY_DAYS` days (default 3, from tomorrow). Players with status `Main` in `Skład` whose cell for one of those days is empty or `?` get one message on the reminders channel that pings them and lists the days; `nie` counts as an answer. Nicks are matched to Discord members by name (cached in the database), so no privileged intent is needed. `/sheet availability` shows who would be pinged right now, without pinging.

#### Scrims, officials and drafts

The Riot API doesn't return regular custom games, so scrims come from the LoL client:

- `uploader/` holds a Windows script (PowerShell, nothing to install). After scrims, a player runs `Wyslij scrimy.cmd` (or leaves `Wysylaj automatycznie.cmd` running): it reads recent custom games from the LoL client's local API and posts them as `lp-tracker-scrims.json` through a Discord webhook. See `uploader/INSTRUKCJA.txt`.
- The bot takes webhook messages with that file in the team server, recognizes our team (3+ of our players on one side, by Riot ID), and appends one row per map to the `Mapy` tab: result, side, players and champions per role, enemy picks and game ID. Each map is linked to that day's scrim in `Mecze` (or a new scrim row is added). Games are deduplicated by game ID in the database and the sheet, so sending the same games again changes nothing.
- Officials played with tournament codes are in the Riot API: every sheet sync the bot looks up our players' tournament games from the last 14 days and adds those that match an official in `Mecze` that day.
- The `Drafty` tab is rebuilt every sync from `Mapy`: our champions per player (scrim/official record and SoloQ winrate), enemy champions we faced, bot lane and jungle-mid pairs, side winrate, and bans against us.
- Full data of every team game is kept for later analysis (SoloQ games are not): the raw Riot API or LoL client JSON with its timeline goes to `team_game_raw` (gzipped), and `team_game_stats` gets one row per player of both teams: role, champion, KDA, kill participation, CS/min, gold and damage share, vision, wards, first blood, deaths before 14 minutes, and gold/CS/XP difference to the lane opponent at 10 and 15 minutes. Officials' timelines come from the Riot API; scrim timelines are sent by the uploader (format version 2). Roles missing in custom games are worked out from the timeline (jungle camps, lane positions). Games added before this are filled in from the saved uploads in `uploads/` during the sheet sync, and re-sending scrims (`Wyslij ponownie wszystko.cmd`) adds timelines to maps the bot already has.

#### Opponent scouting

`/scout <team> [players]` scouts an opponent for draft prep. Players are given as a multi op.gg link or Riot IDs separated by commas (EUW and EUNE accounts both work) and are remembered for the team; without them the bot uses the saved ones or the most frequent opponents from our games against that team (by the `Przeciwnik` column in `Mecze`). For each player the report shows:

- rank (SoloQ and Flex) and main role,
- champions in ranked this season and last season (up to `SCOUT_MAX_GAMES` newest games per season, default 100) with games, W/L, winrate and KDA,
- champions in tournament-code and Clash games,
- one-trick warnings,

plus suggested bans (comfort picks by games and smoothed winrate, tournament games count triple), what the team picked and banned against us, pro-play priority and winrate of each champion from [gol.gg](https://gol.gg) (current season, all tournaments, cached for a day), and with an OpenRouter key a short summary. The embed goes to the channel and the full tables to the bot-only `Scouting` tab (overwritten by each scouting). Games are cached in `scout_games`, so the first scouting of a team can take 15–20 minutes because of the Riot API rate limit, and later ones only fetch new games. When the 24-hour (or day-before) reminder of an official (any type but Scrim) is posted and the opponent's players are known, the bot scouts it automatically and posts the report under the reminder.

```env
SCOUT_MAX_GAMES=100                 # ranked games per player and season
```

#### Comments on SoloQ games

When a SoloQ game of a roster player is posted to the notification channel, the bot looks at it with the player's last 30 days of SoloQ and may reply with a short comment that pings the player: 3/5/7/10+ game win or lose streaks, the end of a 4+ streak, pentakills and great games, int games (10+ deaths, KDA under 1), losses on a champion outside the player's pool while a comfort pick has 60%+ winrate, and the third loss of the night. At most `ROASTS_PER_DAY` comments per player a day (default 3); players can turn them off with `/roast off`. Only players listed in `Skład` get comments.

The text comes from a cheap model through [OpenRouter](https://openrouter.ai), fed only the game's facts (recent games, champion pool, lane opponent, rank). Without a key, or when the request fails, a template is used instead.

```env
OPENROUTER_API_KEY=                 # empty = template comments
OPENROUTER_MODEL=openai/gpt-6-luna
ROASTS_ENABLED=1
ROASTS_PER_DAY=3
```

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

### Team sheet

| Command | Description |
|---|---|
| `/sheet sync` | Sync the team Google Sheet now |
| `/sheet availability` | Show who would be pinged about missing availability (no pings) |

### Scouting

| Command | Description |
|---|---|
| `/scout <team> [players]` | Scout an opponent; `players` is a multi op.gg link or Riot IDs, saved for the team |

### Comments on games

| Command | Description |
|---|---|
| `/roast off` | Turn off comments on your SoloQ games |
| `/roast on` | Turn them back on |

## How it works

The bot runs two background loops:

1. **Leaderboard updater** (hourly) — Fetches current ranked stats for every tracked player via the Riot API, updates the database, and edits the pinned leaderboard embed in the configured channel. Players are sorted by rank (tier + division + LP).

2. **Match checker** (every 5 min) — Polls recent ranked matches for all tracked players. When a new match is found, it sends a notification embed with the result, champion played, KDA, CS/min, and game duration, and may reply with a comment (see above). Matches are deduplicated per leaderboard to avoid spam.

3. **Team sheet sync** (hourly, only when `GOOGLE_SHEET_ID` is set) — Syncs the roster from the sheet, stores new SoloQ/Flex games, and writes the stats to the sheet (see above).

4. **Match reminders** (every 5 min, only when `GOOGLE_SHEET_ID` is set) — Posts reminders about planned matches from the sheet's `Mecze` tab, pinging the team role.

5. **Availability check** (daily from `AVAILABILITY_HOUR`, only when `GOOGLE_SHEET_ID` is set) — Pings Main players without availability for the next days.

All Riot API requests share one rate limiter (kept just under 20 requests per second and 100 per 2 minutes), so long jobs don't run into 429s.

## Architecture

```
bot.py              — Bot setup, background task loops
config.py           — Environment config, rank constants
riot_api.py         — Async Riot API client (account lookup, ranked stats, match history)
database.py         — SQLite schema, CRUD operations
sheets.py           — Google Sheets client for the team spreadsheet
reminders.py        — Match reminder schedule and messages
availability.py     — Pings about missing availability in the Dostępność tab
roasts.py           — Comments on SoloQ games (triggers, prompt, templates)
llm.py              — OpenRouter client for short texts
members.py          — Discord members found by roster nick
team_games.py       — Scrims (client uploads) and officials (Riot API) into Mecze/Mapy
game_stats.py       — Full data of team games: raw JSON, timelines, per-player stats
scouting.py         — Opponent scouting (Riot API pools, gol.gg pro meta, Scouting tab)
drafts.py           — Drafty tab analysis
champions.py        — Champion names from Data Dragon
uploader/           — Windows script that sends scrims from the LoL client
cogs/
  tracker.py        — Leaderboard/player slash commands, embed formatting
  notifications.py  — Match notification commands and embeds
  sheet.py          — Team sheet sync (roster, match history, stats), /sheet sync and /sheet availability
  scrims.py         — Scrim uploads from the webhook
  roasts.py         — /roast on|off
  scouting.py       — /scout
```

## API note

The free Riot API key (Development key) expires every 24 hours. For a permanent key, register a [personal API key](https://developer.riotgames.com/docs/portal#product-registration_personal-api-keys) (doesn't expire, same rate limits: 20 requests per second, 100 per 2 minutes) or apply for a [Production API key](https://developer.riotgames.com/docs/portal#product-registration).

The bot is currently configured for the EUW region. To change the region, edit the API endpoints in `config.py`.

## License

MIT
