import aiohttp
import asyncio
from urllib.parse import quote
from config import RIOT_API_KEY, RIOT_ACCOUNT_API, RIOT_EUW_API


class RiotAPIError(Exception):
    """Custom exception for Riot API errors."""
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"Riot API Error {status}: {message}")


class RiotAPI:
    """Async client for Riot Games API."""

    def __init__(self, api_key: str = None):
        self.api_key = api_key or RIOT_API_KEY
        self._session: aiohttp.ClientSession | None = None

    @property
    def headers(self):
        return {"X-Riot-Token": self.api_key}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self.headers)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(self, url: str) -> dict | list:
        """Make an API request with basic rate limit handling."""
        session = await self._get_session()

        async with session.get(url) as response:
            if response.status == 200:
                return await response.json()
            elif response.status == 429:
                # Rate limited - wait and retry
                retry_after = int(response.headers.get("Retry-After", 5))
                await asyncio.sleep(retry_after)
                return await self._request(url)
            elif response.status == 404:
                raise RiotAPIError(404, "Player not found")
            elif response.status == 403:
                raise RiotAPIError(403, "Invalid or expired API key")
            else:
                text = await response.text()
                raise RiotAPIError(response.status, text)

    async def get_account_by_riot_id(self, game_name: str, tag_line: str) -> dict:
        """
        Get account info by Riot ID (name#tag).
        Returns dict with 'puuid', 'gameName', 'tagLine'.
        """
        # URL encode the game name and tag in case of special characters
        encoded_name = quote(game_name, safe='')
        encoded_tag = quote(tag_line, safe='')
        url = f"{RIOT_ACCOUNT_API}/riot/account/v1/accounts/by-riot-id/{encoded_name}/{encoded_tag}"
        return await self._request(url)

    async def get_ranked_stats_by_puuid(self, puuid: str) -> dict | None:
        """
        Get ranked stats by PUUID.
        Returns Solo Queue stats or None if unranked.
        """
        url = f"{RIOT_EUW_API}/lol/league/v4/entries/by-puuid/{puuid}"
        entries = await self._request(url)

        # Find Solo Queue entry
        for entry in entries:
            if entry.get("queueType") == "RANKED_SOLO_5x5":
                return {
                    "tier": entry.get("tier"),
                    "rank": entry.get("rank"),
                    "lp": entry.get("leaguePoints", 0),
                    "wins": entry.get("wins", 0),
                    "losses": entry.get("losses", 0),
                }

        # Player is unranked in Solo Queue
        return None

    async def get_player_full_info(self, game_name: str, tag_line: str) -> dict:
        """
        Get full player info including rank.
        Uses PUUID-based endpoints (no summoner ID needed).
        """
        # Get account (PUUID)
        account = await self.get_account_by_riot_id(game_name, tag_line)
        puuid = account["puuid"]

        # Get ranked stats directly by PUUID
        ranked = await self.get_ranked_stats_by_puuid(puuid)

        return {
            "puuid": puuid,
            "game_name": account.get("gameName", game_name),
            "tag_line": account.get("tagLine", tag_line),
            "ranked": ranked,
        }

    async def get_match_ids_by_puuid(
        self,
        puuid: str,
        queue: int = 420,
        count: int = 5,
        start_time: int = None
    ) -> list[str]:
        """
        Get recent match IDs for a player.

        Args:
            puuid: Player's PUUID
            queue: Queue ID (420 = Ranked Solo/Duo, 440 = Flex)
            count: Number of matches to retrieve (max 100)
            start_time: Unix timestamp in seconds - only get matches after this time

        Returns:
            List of match IDs (e.g., ["EUW1_1234567890", ...])
        """
        url = f"{RIOT_ACCOUNT_API}/lol/match/v5/matches/by-puuid/{puuid}/ids?queue={queue}&count={count}"
        if start_time:
            url += f"&startTime={start_time}"
        return await self._request(url)

    async def get_match_details(self, match_id: str) -> dict:
        """
        Get detailed information about a specific match.

        Args:
            match_id: Match ID (e.g., "EUW1_1234567890")

        Returns:
            Match data including participants, KDA, champions, win/loss, etc.
        """
        url = f"{RIOT_ACCOUNT_API}/lol/match/v5/matches/{match_id}"
        return await self._request(url)

    def extract_player_match_stats(self, match_data: dict, puuid: str) -> dict | None:
        """
        Extract relevant stats for a specific player from match data.

        Returns:
            Dict with match stats or None if player not found in match.
        """
        info = match_data.get("info", {})
        participants = info.get("participants", [])

        for participant in participants:
            if participant.get("puuid") == puuid:
                kills = participant.get("kills", 0)
                deaths = participant.get("deaths", 0)
                assists = participant.get("assists", 0)

                # Calculate KDA ratio (avoid division by zero)
                kda_ratio = (kills + assists) / max(deaths, 1)

                return {
                    "match_id": match_data.get("metadata", {}).get("matchId"),
                    "win": participant.get("win", False),
                    "champion_name": participant.get("championName", "Unknown"),
                    "champion_id": participant.get("championId", 0),
                    "kills": kills,
                    "deaths": deaths,
                    "assists": assists,
                    "kda_ratio": round(kda_ratio, 2),
                    "cs": participant.get("totalMinionsKilled", 0) + participant.get("neutralMinionsKilled", 0),
                    "game_duration_minutes": info.get("gameDuration", 0) // 60,
                    "game_end_timestamp": info.get("gameEndTimestamp", 0),
                    "queue_id": info.get("queueId", 0),
                }

        return None


# Global instance
riot_api = RiotAPI()
