import os
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
RIOT_API_KEY = os.getenv("RIOT_API_KEY")
UPDATE_INTERVAL = int(os.getenv("UPDATE_INTERVAL", 3600))
NOTIFICATION_INTERVAL = int(os.getenv("NOTIFICATION_INTERVAL", 300))  # 5 minutes
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "UTC"))

# Google Sheets sync (disabled unless GOOGLE_SHEET_ID is set)
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "~/.config/lp-tracker/service_account.json"
)
SHEET_GUILD_ID = int(os.getenv("SHEET_GUILD_ID", 0))
SHEET_LEADERBOARD = os.getenv("SHEET_LEADERBOARD", "")
SHEET_SYNC_INTERVAL = int(os.getenv("SHEET_SYNC_INTERVAL", 3600))  # 1 hour

# Reminders about planned matches from the sheet's "Mecze" tab (need the sheet settings above)
REMINDER_CHANNEL_ID = int(os.getenv("REMINDER_CHANNEL_ID", 0))  # 0 = team leaderboard's notification channel
REMINDER_ROLE_ID = int(os.getenv("REMINDER_ROLE_ID", 0))  # 0 = don't ping a role
REMINDER_CHECK_INTERVAL = int(os.getenv("REMINDER_CHECK_INTERVAL", 300))  # 5 minutes

# Pings about the "Dostępność" tab: Main players without an answer for the next days
AVAILABILITY_HOUR = int(os.getenv("AVAILABILITY_HOUR", 12))  # local time of the daily check
AVAILABILITY_DAYS = int(os.getenv("AVAILABILITY_DAYS", 3))  # days ahead, from tomorrow

# Comments on SoloQ games written by an LLM through OpenRouter (templates without a key)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-6-luna")
ROASTS_ENABLED = os.getenv("ROASTS_ENABLED", "1") not in ("0", "false", "False", "")
ROASTS_PER_DAY = int(os.getenv("ROASTS_PER_DAY", 3))  # per player
# Replies of the bot per person per day when people reply to its messages under games
CHAT_REPLIES_PER_DAY = int(os.getenv("CHAT_REPLIES_PER_DAY", 30))

# Opponent scouting: default period of ranked and tournament-code games, in months (/scout offers 3, 6, 12)
SCOUT_MONTHS = int(os.getenv("SCOUT_MONTHS", 6))

# Scouting reports as HTML pages: folder a web server serves and its public URL (both empty = off)
SCOUTING_WEB_DIR = os.getenv("SCOUTING_WEB_DIR", "")
SCOUTING_WEB_URL = os.getenv("SCOUTING_WEB_URL", "").rstrip("/")

# Riot API endpoints
RIOT_ACCOUNT_API = "https://europe.api.riotgames.com"
RIOT_EUW_API = "https://euw1.api.riotgames.com"
RIOT_EUW_PLATFORM = "euw1"  # Only accounts on this platform are tracked
# App rate limits of the API key as (requests, seconds), kept a bit under Riot's 20/1 s and 100/2 min
RIOT_RATE_LIMITS = [(19, 1), (95, 120)]

# Rank order for sorting (higher index = better rank)
RANK_ORDER = {
    "IRON": 0,
    "BRONZE": 1,
    "SILVER": 2,
    "GOLD": 3,
    "PLATINUM": 4,
    "EMERALD": 5,
    "DIAMOND": 6,
    "MASTER": 7,
    "GRANDMASTER": 8,
    "CHALLENGER": 9,
}

DIVISION_ORDER = {
    "IV": 0,
    "III": 1,
    "II": 2,
    "I": 3,
}
