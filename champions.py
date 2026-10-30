import time

import aiohttp

DDRAGON = "https://ddragon.leagueoflegends.com"
REFRESH_SECONDS = 24 * 60 * 60


class ChampionData:
    """Champion names from Riot's Data Dragon, refreshed once a day."""

    def __init__(self):
        self.by_key: dict[int, str] = {}  # 62 -> "Wukong"
        self.by_id: dict[str, str] = {}  # "MonkeyKing" -> "Wukong"
        self._loaded_at = 0.0

    async def refresh(self) -> None:
        """Load the latest champion list, unless it was loaded in the last day. Keeps the old list on errors."""
        if self.by_key and time.time() - self._loaded_at < REFRESH_SECONDS:
            return
        try:
            # A separate session: the Riot API one sends our API key
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{DDRAGON}/api/versions.json") as response:
                    version = (await response.json())[0]
                async with session.get(f"{DDRAGON}/cdn/{version}/data/en_US/champion.json") as response:
                    data = (await response.json())["data"]
        except (aiohttp.ClientError, KeyError, IndexError) as e:
            print(f"Could not load champion names from Data Dragon: {e}")
            return
        self.by_key = {int(champion["key"]): champion["name"] for champion in data.values()}
        self.by_id = {champion["id"]: champion["name"] for champion in data.values()}
        self._loaded_at = time.time()

    def name(self, champion_id: int = None, champion_key: str = None) -> str:
        """Display name by numeric ID (client and API) or by key like "MonkeyKing" (API)."""
        if champion_id in self.by_key:
            return self.by_key[champion_id]
        if champion_key:
            return self.by_id.get(champion_key, champion_key)
        return f"Champion {champion_id}"

    @property
    def names(self) -> list[str]:
        return sorted(self.by_key.values())


# Global instance
champions = ChampionData()
