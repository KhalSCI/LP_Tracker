import os
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
RIOT_API_KEY = os.getenv("RIOT_API_KEY")
UPDATE_INTERVAL = int(os.getenv("UPDATE_INTERVAL", 3600))
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "UTC"))

# Riot API endpoints
RIOT_ACCOUNT_API = "https://europe.api.riotgames.com"
RIOT_EUW_API = "https://euw1.api.riotgames.com"

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
