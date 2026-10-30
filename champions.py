import time

import aiohttp

DDRAGON = "https://ddragon.leagueoflegends.com"
REFRESH_SECONDS = 24 * 60 * 60


class ChampionData:
    """Champion and item names from Riot's Data Dragon, refreshed once a day."""

    def __init__(self):
        self.by_key: dict[int, str] = {}  # 62 -> "Wukong"
        self.by_id: dict[str, str] = {}  # "MonkeyKing" -> "Wukong"
        self.id_by_name: dict[str, str] = {}  # "Wukong" -> "MonkeyKing"
        self.items: dict[int, str] = {}  # 3031 -> "Infinity Edge"
        self.version = ""
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
                async with session.get(f"{DDRAGON}/cdn/{version}/data/en_US/item.json") as response:
                    items = (await response.json())["data"]
        except (aiohttp.ClientError, KeyError, IndexError) as e:
            print(f"Could not load champion names from Data Dragon: {e}")
            return
        self.by_key = {int(champion["key"]): champion["name"] for champion in data.values()}
        self.by_id = {champion["id"]: champion["name"] for champion in data.values()}
        self.id_by_name = {champion["name"]: champion["id"] for champion in data.values()}
        self.items = {int(item_id): item["name"] for item_id, item in items.items() if item_id.isdigit()}
        self.version = version
        self._loaded_at = time.time()

    def name(self, champion_id: int = None, champion_key: str = None) -> str:
        """Display name by numeric ID (client and API) or by key like "MonkeyKing" (API)."""
        if champion_id in self.by_key:
            return self.by_key[champion_id]
        if champion_key:
            return self.by_id.get(champion_key, champion_key)
        return f"Champion {champion_id}"

    def item_name(self, item_id: int) -> str:
        return self.items.get(item_id, f"item {item_id}")

    def icon_url(self, name: str) -> str | None:
        """Data Dragon square icon of a champion by display name (or key), None if unknown."""
        champion_id = self.id_by_name.get(name) or (name if name in self.by_id else None)
        if not champion_id or not self.version:
            return None
        return f"{DDRAGON}/cdn/{self.version}/img/champion/{champion_id}.png"

    @property
    def names(self) -> list[str]:
        return sorted(self.by_key.values())


# Global instance
champions = ChampionData()
