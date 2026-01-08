"""Tests for Riot API client."""
import pytest
from aioresponses import aioresponses

from riot_api import RiotAPI, RiotAPIError
from config import RIOT_ACCOUNT_API, RIOT_EUW_API


# Sample API responses
SAMPLE_ACCOUNT_RESPONSE = {
    "puuid": "abc123-puuid-xyz",
    "gameName": "Dziad",
    "tagLine": "J0KER"
}

SAMPLE_RANKED_RESPONSE = [
    {
        "queueType": "RANKED_SOLO_5x5",
        "tier": "DIAMOND",
        "rank": "IV",
        "leaguePoints": 45,
        "wins": 100,
        "losses": 80
    },
    {
        "queueType": "RANKED_FLEX_SR",
        "tier": "PLATINUM",
        "rank": "II",
        "leaguePoints": 30,
        "wins": 50,
        "losses": 40
    }
]

SAMPLE_UNRANKED_RESPONSE = []


class TestRiotAPI:
    """Test cases for RiotAPI class."""

    @pytest.fixture
    def api(self):
        """Create a RiotAPI instance with test API key."""
        return RiotAPI(api_key="test-api-key")

    @pytest.mark.asyncio
    async def test_get_account_by_riot_id_success(self, api):
        """Test successful account lookup by Riot ID."""
        with aioresponses() as mocked:
            url = f"{RIOT_ACCOUNT_API}/riot/account/v1/accounts/by-riot-id/Dziad/J0KER"
            mocked.get(url, payload=SAMPLE_ACCOUNT_RESPONSE)

            result = await api.get_account_by_riot_id("Dziad", "J0KER")
            await api.close()

            assert result["puuid"] == "abc123-puuid-xyz"
            assert result["gameName"] == "Dziad"
            assert result["tagLine"] == "J0KER"

    @pytest.mark.asyncio
    async def test_get_account_by_riot_id_not_found(self, api):
        """Test account lookup returns 404 for unknown player."""
        with aioresponses() as mocked:
            url = f"{RIOT_ACCOUNT_API}/riot/account/v1/accounts/by-riot-id/Unknown/Player"
            mocked.get(url, status=404)

            with pytest.raises(RiotAPIError) as exc_info:
                await api.get_account_by_riot_id("Unknown", "Player")
            await api.close()

            assert exc_info.value.status == 404
            assert "not found" in exc_info.value.message.lower()

    @pytest.mark.asyncio
    async def test_get_account_by_riot_id_invalid_api_key(self, api):
        """Test account lookup returns 403 for invalid API key."""
        with aioresponses() as mocked:
            url = f"{RIOT_ACCOUNT_API}/riot/account/v1/accounts/by-riot-id/Dziad/J0KER"
            mocked.get(url, status=403)

            with pytest.raises(RiotAPIError) as exc_info:
                await api.get_account_by_riot_id("Dziad", "J0KER")
            await api.close()

            assert exc_info.value.status == 403

    @pytest.mark.asyncio
    async def test_get_account_special_characters(self, api):
        """Test account lookup with special characters in name."""
        with aioresponses() as mocked:
            # URL-encoded special characters
            url = f"{RIOT_ACCOUNT_API}/riot/account/v1/accounts/by-riot-id/Test%20Player/TAG"
            mocked.get(url, payload={
                "puuid": "special-puuid",
                "gameName": "Test Player",
                "tagLine": "TAG"
            })

            result = await api.get_account_by_riot_id("Test Player", "TAG")
            await api.close()

            assert result["gameName"] == "Test Player"

    @pytest.mark.asyncio
    async def test_get_ranked_stats_by_puuid_success(self, api):
        """Test successful ranked stats lookup by PUUID."""
        puuid = "abc123-puuid-xyz"
        with aioresponses() as mocked:
            url = f"{RIOT_EUW_API}/lol/league/v4/entries/by-puuid/{puuid}"
            mocked.get(url, payload=SAMPLE_RANKED_RESPONSE)

            result = await api.get_ranked_stats_by_puuid(puuid)
            await api.close()

            assert result is not None
            assert result["tier"] == "DIAMOND"
            assert result["rank"] == "IV"
            assert result["lp"] == 45
            assert result["wins"] == 100
            assert result["losses"] == 80

    @pytest.mark.asyncio
    async def test_get_ranked_stats_by_puuid_unranked(self, api):
        """Test ranked stats returns None for unranked player."""
        puuid = "unranked-puuid"
        with aioresponses() as mocked:
            url = f"{RIOT_EUW_API}/lol/league/v4/entries/by-puuid/{puuid}"
            mocked.get(url, payload=SAMPLE_UNRANKED_RESPONSE)

            result = await api.get_ranked_stats_by_puuid(puuid)
            await api.close()

            assert result is None

    @pytest.mark.asyncio
    async def test_get_ranked_stats_flex_only(self, api):
        """Test ranked stats returns None when only flex queue is played."""
        puuid = "flex-only-puuid"
        flex_only_response = [
            {
                "queueType": "RANKED_FLEX_SR",
                "tier": "GOLD",
                "rank": "I",
                "leaguePoints": 75,
                "wins": 30,
                "losses": 25
            }
        ]
        with aioresponses() as mocked:
            url = f"{RIOT_EUW_API}/lol/league/v4/entries/by-puuid/{puuid}"
            mocked.get(url, payload=flex_only_response)

            result = await api.get_ranked_stats_by_puuid(puuid)
            await api.close()

            # Should return None since there's no RANKED_SOLO_5x5
            assert result is None

    @pytest.mark.asyncio
    async def test_get_player_full_info_ranked(self, api):
        """Test get_player_full_info for a ranked player."""
        with aioresponses() as mocked:
            # Mock account endpoint
            account_url = f"{RIOT_ACCOUNT_API}/riot/account/v1/accounts/by-riot-id/Dziad/J0KER"
            mocked.get(account_url, payload=SAMPLE_ACCOUNT_RESPONSE)

            # Mock ranked endpoint
            ranked_url = f"{RIOT_EUW_API}/lol/league/v4/entries/by-puuid/abc123-puuid-xyz"
            mocked.get(ranked_url, payload=SAMPLE_RANKED_RESPONSE)

            result = await api.get_player_full_info("Dziad", "J0KER")
            await api.close()

            assert result["puuid"] == "abc123-puuid-xyz"
            assert result["game_name"] == "Dziad"
            assert result["tag_line"] == "J0KER"
            assert result["ranked"] is not None
            assert result["ranked"]["tier"] == "DIAMOND"
            assert result["ranked"]["rank"] == "IV"
            assert result["ranked"]["lp"] == 45

    @pytest.mark.asyncio
    async def test_get_player_full_info_unranked(self, api):
        """Test get_player_full_info for an unranked player."""
        with aioresponses() as mocked:
            # Mock account endpoint
            account_url = f"{RIOT_ACCOUNT_API}/riot/account/v1/accounts/by-riot-id/NewPlayer/EUW"
            mocked.get(account_url, payload={
                "puuid": "newplayer-puuid",
                "gameName": "NewPlayer",
                "tagLine": "EUW"
            })

            # Mock ranked endpoint (empty = unranked)
            ranked_url = f"{RIOT_EUW_API}/lol/league/v4/entries/by-puuid/newplayer-puuid"
            mocked.get(ranked_url, payload=[])

            result = await api.get_player_full_info("NewPlayer", "EUW")
            await api.close()

            assert result["puuid"] == "newplayer-puuid"
            assert result["game_name"] == "NewPlayer"
            assert result["ranked"] is None

    @pytest.mark.asyncio
    async def test_get_player_full_info_not_found(self, api):
        """Test get_player_full_info raises error for unknown player."""
        with aioresponses() as mocked:
            account_url = f"{RIOT_ACCOUNT_API}/riot/account/v1/accounts/by-riot-id/FakePlayer/FAKE"
            mocked.get(account_url, status=404)

            with pytest.raises(RiotAPIError) as exc_info:
                await api.get_player_full_info("FakePlayer", "FAKE")
            await api.close()

            assert exc_info.value.status == 404


class TestRiotAPIHighRanks:
    """Test cases for high-rank players (Master+)."""

    @pytest.fixture
    def api(self):
        return RiotAPI(api_key="test-api-key")

    @pytest.mark.asyncio
    async def test_master_player(self, api):
        """Test Master rank player has no division."""
        master_response = [
            {
                "queueType": "RANKED_SOLO_5x5",
                "tier": "MASTER",
                "rank": "I",  # Master+ always shows I but it's not displayed
                "leaguePoints": 250,
                "wins": 200,
                "losses": 150
            }
        ]
        with aioresponses() as mocked:
            url = f"{RIOT_EUW_API}/lol/league/v4/entries/by-puuid/master-puuid"
            mocked.get(url, payload=master_response)

            result = await api.get_ranked_stats_by_puuid("master-puuid")
            await api.close()

            assert result["tier"] == "MASTER"
            assert result["lp"] == 250

    @pytest.mark.asyncio
    async def test_challenger_player(self, api):
        """Test Challenger rank player."""
        challenger_response = [
            {
                "queueType": "RANKED_SOLO_5x5",
                "tier": "CHALLENGER",
                "rank": "I",
                "leaguePoints": 1200,
                "wins": 500,
                "losses": 300
            }
        ]
        with aioresponses() as mocked:
            url = f"{RIOT_EUW_API}/lol/league/v4/entries/by-puuid/challenger-puuid"
            mocked.get(url, payload=challenger_response)

            result = await api.get_ranked_stats_by_puuid("challenger-puuid")
            await api.close()

            assert result["tier"] == "CHALLENGER"
            assert result["lp"] == 1200
