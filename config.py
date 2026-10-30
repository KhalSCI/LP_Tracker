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

# Riot API endpoints
RIOT_ACCOUNT_API = "https://europe.api.riotgames.com"
RIOT_EUW_API = "https://euw1.api.riotgames.com"
RIOT_EUW_PLATFORM = "euw1"  # Only accounts on this platform are tracked

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
