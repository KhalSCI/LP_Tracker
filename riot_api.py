import aiohttp
import asyncio
import time
from collections import deque
from urllib.parse import quote
from config import RIOT_API_KEY, RIOT_ACCOUNT_API, RIOT_EUW_API, RIOT_RATE_LIMITS


class RiotAPIError(Exception):
    """Custom exception for Riot API errors."""
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"Riot API Error {status}: {message}")


class RateLimiter:
    """
    Spaces out requests to stay under rate limits given as (requests, seconds) sliding
    windows, shared by all background tasks so a long job (e.g. scouting) can't cause 429s.
    """

    def __init__(self, limits: list[tuple[int, float]]):
        self.limits = limits
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                longest = max(seconds for _, seconds in self.limits)
                while self._calls and now - self._calls[0] >= longest:
                    self._calls.popleft()
                wait = 0.0
                for count, seconds in self.limits:
                    recent = [t for t in self._calls if now - t < seconds]
                    if len(recent) >= count:
                        # Until the oldest call that keeps the window full drops out of it
                        wait = max(wait, recent[len(recent) - count] + seconds - now)
                if wait <= 0:
                    self._calls.append(now)
                    return
                await asyncio.sleep(wait)


class RiotAPI:
    """Async client for Riot Games API."""

    def __init__(self, api_key: str = None):
        self.api_key = api_key or RIOT_API_KEY
        self._session: aiohttp.ClientSession | None = None
        self._limiter = RateLimiter(RIOT_RATE_LIMITS)

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
        await self._limiter.acquire()

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

    async def get_account_region(self, puuid: str) -> str:
        """
        Get the LoL platform an account plays on (e.g., 'euw1', 'eun1').
        """
        url = f"{RIOT_ACCOUNT_API}/riot/account/v1/region/by-game/lol/by-puuid/{puuid}"
        account_region = await self._request(url)
        return account_region.get("region", "").lower()

    async def get_league_entries(self, puuid: str, platform: str = "euw1") -> list[dict]:
        """All ranked entries (Solo/Duo, Flex) of an account on a platform, e.g. "euw1" or "eun1"."""
        url = f"https://{platform}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
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

        # Get the platform the account plays on (callers only accept EUW)
        region = await self.get_account_region(puuid)

        # Get ranked stats directly by PUUID
        ranked = await self.get_ranked_stats_by_puuid(puuid)

        return {
            "puuid": puuid,
            "game_name": account.get("gameName", game_name),
            "tag_line": account.get("tagLine", tag_line),
            "region": region,
            "ranked": ranked,
        }

    async def get_match_ids_by_puuid(
        self,
        puuid: str,
        queue: int = 420,
        count: int = 5,
        start_time: int = None,
        start: int = 0
    ) -> list[str]:
        """
        Get recent match IDs for a player.

        Args:
            puuid: Player's PUUID
            queue: Queue ID (420 = Ranked Solo/Duo, 440 = Flex)
            count: Number of matches to retrieve (max 100)
            start_time: Unix timestamp in seconds - only get matches after this time
            start: Index of the first match to return, for paging past `count`

        Returns:
            List of match IDs, newest first (e.g., ["EUW1_1234567890", ...])
        """
        url = f"{RIOT_ACCOUNT_API}/lol/match/v5/matches/by-puuid/{puuid}/ids?queue={queue}&count={count}"
        if start_time:
            url += f"&startTime={start_time}"
        if start:
            url += f"&start={start}"
        return await self._request(url)

    async def get_tournament_match_ids(self, puuid: str, start_time: int, count: int = 100) -> list[str]:
        """
        Get a player's tournament-code games (e.g. official matches) since start_time
        (Unix seconds). Regular custom games are not available in the Riot API.
        """
        url = (
            f"{RIOT_ACCOUNT_API}/lol/match/v5/matches/by-puuid/{puuid}/ids"
            f"?type=tourney&count={count}&startTime={start_time}"
        )
        return await self._request(url)

    async def get_match_ids(
        self,
        puuid: str,
        match_type: str = None,
        start_time: int = None,
        end_time: int = None,
        start: int = 0,
        count: int = 100
    ) -> list[str]:
        """
        Get a page of a player's match IDs, newest first, optionally only one type
        ("ranked", "tourney" = tournament codes, ...) and only between start_time and
        end_time (Unix seconds).
        """
        url = f"{RIOT_ACCOUNT_API}/lol/match/v5/matches/by-puuid/{puuid}/ids?count={count}&start={start}"
        if match_type:
            url += f"&type={match_type}"
        if start_time:
            url += f"&startTime={start_time}"
        if end_time:
            url += f"&endTime={end_time}"
        return await self._request(url)

    async def get_match_timeline(self, match_id: str) -> dict:
        """Get a match's timeline: per-minute frames (gold, XP, CS, positions) and events."""
        url = f"{RIOT_ACCOUNT_API}/lol/match/v5/matches/{match_id}/timeline"
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

                # Calculate CS per minute
                total_cs = participant.get("totalMinionsKilled", 0) + participant.get("neutralMinionsKilled", 0)
                game_duration_seconds = info.get("gameDuration", 0)
                game_duration_minutes = game_duration_seconds / 60
                cs_per_min = round(total_cs / max(game_duration_minutes, 1), 2)

                return {
                    "match_id": match_data.get("metadata", {}).get("matchId"),
                    "win": participant.get("win", False),
                    "champion_name": participant.get("championName", "Unknown"),
                    "champion_id": participant.get("championId", 0),
                    "kills": kills,
                    "deaths": deaths,
                    "assists": assists,
                    "kda_ratio": round(kda_ratio, 2),
                    "cs": total_cs,
                    "cs_per_min": cs_per_min,
                    "game_duration_minutes": int(game_duration_minutes),
                    "game_start_timestamp": info.get("gameStartTimestamp", 0),
                    "game_end_timestamp": info.get("gameEndTimestamp", 0),
                    "queue_id": info.get("queueId", 0),
                    "team_id": participant.get("teamId", 0),
                    "remake": participant.get("gameEndedInEarlySurrender", False),
                }

        return None


# Global instance
riot_api = RiotAPI()
