"""Tests for database operations."""
import pytest
import os
from pathlib import Path

# Set up test database before importing database module
TEST_DB_PATH = Path(__file__).parent / "test_lp_tracker.db"


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch):
    """Use a test database for all tests."""
    import database as db
    monkeypatch.setattr(db, "DB_PATH", TEST_DB_PATH)
    yield
    # Cleanup after tests
    if TEST_DB_PATH.exists():
        os.remove(TEST_DB_PATH)


class TestLeaderboards:
    """Test cases for leaderboard operations."""

    @pytest.mark.asyncio
    async def test_create_leaderboard(self):
        """Test creating a new leaderboard."""
        import database as db
        await db.init_db()

        lb_id = await db.create_leaderboard(
            guild_id=123456789,
            name="Test Leaderboard",
            channel_id=987654321
        )

        assert lb_id is not None
        assert lb_id > 0

    @pytest.mark.asyncio
    async def test_create_leaderboard_lowercase_name(self):
        """Test that leaderboard names are stored lowercase."""
        import database as db
        await db.init_db()

        await db.create_leaderboard(guild_id=123, name="MyBoard")
        lb = await db.get_leaderboard(123, "myboard")

        assert lb is not None
        assert lb["name"] == "myboard"

    @pytest.mark.asyncio
    async def test_get_leaderboard(self):
        """Test retrieving a leaderboard."""
        import database as db
        await db.init_db()

        await db.create_leaderboard(
            guild_id=111,
            name="ranked",
            channel_id=222
        )

        lb = await db.get_leaderboard(111, "ranked")

        assert lb is not None
        assert lb["guild_id"] == 111
        assert lb["name"] == "ranked"
        assert lb["channel_id"] == 222

    @pytest.mark.asyncio
    async def test_get_leaderboard_not_found(self):
        """Test retrieving non-existent leaderboard returns None."""
        import database as db
        await db.init_db()

        lb = await db.get_leaderboard(999, "nonexistent")

        assert lb is None

    @pytest.mark.asyncio
    async def test_get_guild_leaderboards(self):
        """Test retrieving all leaderboards for a guild."""
        import database as db
        await db.init_db()

        await db.create_leaderboard(guild_id=100, name="board1")
        await db.create_leaderboard(guild_id=100, name="board2")
        await db.create_leaderboard(guild_id=200, name="other")

        leaderboards = await db.get_guild_leaderboards(100)

        assert len(leaderboards) == 2
        names = [lb["name"] for lb in leaderboards]
        assert "board1" in names
        assert "board2" in names

    @pytest.mark.asyncio
    async def test_delete_leaderboard(self):
        """Test deleting a leaderboard."""
        import database as db
        await db.init_db()

        await db.create_leaderboard(guild_id=123, name="todelete")
        deleted = await db.delete_leaderboard(123, "todelete")

        assert deleted is True
        lb = await db.get_leaderboard(123, "todelete")
        assert lb is None

    @pytest.mark.asyncio
    async def test_delete_leaderboard_not_found(self):
        """Test deleting non-existent leaderboard returns False."""
        import database as db
        await db.init_db()

        deleted = await db.delete_leaderboard(999, "nonexistent")

        assert deleted is False

    @pytest.mark.asyncio
    async def test_set_leaderboard_channel(self):
        """Test updating leaderboard channel."""
        import database as db
        await db.init_db()

        await db.create_leaderboard(guild_id=123, name="test")
        updated = await db.set_leaderboard_channel(123, "test", 555)

        assert updated is True
        lb = await db.get_leaderboard(123, "test")
        assert lb["channel_id"] == 555


class TestPlayers:
    """Test cases for player operations."""

    @pytest.mark.asyncio
    async def test_add_player(self):
        """Test adding a player to a leaderboard."""
        import database as db
        await db.init_db()

        lb_id = await db.create_leaderboard(guild_id=123, name="test")
        player_id = await db.add_player(
            leaderboard_id=lb_id,
            riot_id="Dziad#J0KER",
            puuid="abc123-puuid"
        )

        assert player_id is not None
        assert player_id > 0

    @pytest.mark.asyncio
    async def test_get_player(self):
        """Test retrieving a player."""
        import database as db
        await db.init_db()

        lb_id = await db.create_leaderboard(guild_id=123, name="test")
        await db.add_player(lb_id, "TestPlayer#EUW", "test-puuid")

        player = await db.get_player(lb_id, "TestPlayer#EUW")

        assert player is not None
        assert player["riot_id"] == "TestPlayer#EUW"
        assert player["puuid"] == "test-puuid"

    @pytest.mark.asyncio
    async def test_get_player_case_insensitive(self):
        """Test player lookup is case-insensitive."""
        import database as db
        await db.init_db()

        lb_id = await db.create_leaderboard(guild_id=123, name="test")
        await db.add_player(lb_id, "TestPlayer#EUW", "test-puuid")

        # Search with different case
        player = await db.get_player(lb_id, "testplayer#euw")

        assert player is not None
        assert player["riot_id"] == "TestPlayer#EUW"

    @pytest.mark.asyncio
    async def test_get_player_not_found(self):
        """Test retrieving non-existent player returns None."""
        import database as db
        await db.init_db()

        lb_id = await db.create_leaderboard(guild_id=123, name="test")
        player = await db.get_player(lb_id, "Unknown#PLAYER")

        assert player is None

    @pytest.mark.asyncio
    async def test_remove_player(self):
        """Test removing a player."""
        import database as db
        await db.init_db()

        lb_id = await db.create_leaderboard(guild_id=123, name="test")
        await db.add_player(lb_id, "ToRemove#TAG", "remove-puuid")

        removed = await db.remove_player(lb_id, "ToRemove#TAG")

        assert removed is True
        player = await db.get_player(lb_id, "ToRemove#TAG")
        assert player is None

    @pytest.mark.asyncio
    async def test_remove_player_not_found(self):
        """Test removing non-existent player returns False."""
        import database as db
        await db.init_db()

        lb_id = await db.create_leaderboard(guild_id=123, name="test")
        removed = await db.remove_player(lb_id, "NonExistent#TAG")

        assert removed is False

    @pytest.mark.asyncio
    async def test_get_leaderboard_players(self):
        """Test retrieving all players for a leaderboard."""
        import database as db
        await db.init_db()

        lb_id = await db.create_leaderboard(guild_id=123, name="test")
        await db.add_player(lb_id, "Player1#TAG", "puuid1")
        await db.add_player(lb_id, "Player2#TAG", "puuid2")
        await db.add_player(lb_id, "Player3#TAG", "puuid3")

        players = await db.get_leaderboard_players(lb_id)

        assert len(players) == 3
        riot_ids = [p["riot_id"] for p in players]
        assert "Player1#TAG" in riot_ids
        assert "Player2#TAG" in riot_ids
        assert "Player3#TAG" in riot_ids

    @pytest.mark.asyncio
    async def test_update_player_rank(self):
        """Test updating a player's rank."""
        import database as db
        await db.init_db()

        lb_id = await db.create_leaderboard(guild_id=123, name="test")
        player_id = await db.add_player(lb_id, "Player#TAG", "puuid")

        await db.update_player_rank(
            player_id=player_id,
            tier="DIAMOND",
            rank="IV",
            lp=45,
            wins=100,
            losses=80
        )

        player = await db.get_player(lb_id, "Player#TAG")

        assert player["tier"] == "DIAMOND"
        assert player["rank"] == "IV"
        assert player["lp"] == 45
        assert player["wins"] == 100
        assert player["losses"] == 80

    @pytest.mark.asyncio
    async def test_update_player_rank_with_prev_lp(self):
        """Test updating rank preserves previous LP."""
        import database as db
        await db.init_db()

        lb_id = await db.create_leaderboard(guild_id=123, name="test")
        player_id = await db.add_player(lb_id, "Player#TAG", "puuid")

        # First update
        await db.update_player_rank(player_id, "GOLD", "I", 50, 50, 50)

        # Second update with prev_lp
        await db.update_player_rank(
            player_id=player_id,
            tier="GOLD",
            rank="I",
            lp=75,
            wins=51,
            losses=50,
            prev_lp=50
        )

        player = await db.get_player(lb_id, "Player#TAG")

        assert player["lp"] == 75
        assert player["prev_lp"] == 50

    @pytest.mark.asyncio
    async def test_delete_leaderboard_deletes_players(self):
        """Test deleting leaderboard also deletes its players."""
        import database as db
        await db.init_db()

        lb_id = await db.create_leaderboard(guild_id=123, name="test")
        await db.add_player(lb_id, "Player1#TAG", "puuid1")
        await db.add_player(lb_id, "Player2#TAG", "puuid2")

        await db.delete_leaderboard(123, "test")

        # Players should be gone
        players = await db.get_leaderboard_players(lb_id)
        assert len(players) == 0


class TestLeaderboardsWithChannels:
    """Test cases for leaderboards with auto-update channels."""

    @pytest.mark.asyncio
    async def test_get_all_leaderboards_with_channels(self):
        """Test retrieving only leaderboards with channels set."""
        import database as db
        await db.init_db()

        # Create leaderboards - some with channels, some without
        await db.create_leaderboard(guild_id=100, name="with_channel", channel_id=111)
        await db.create_leaderboard(guild_id=100, name="no_channel")
        await db.create_leaderboard(guild_id=200, name="also_with", channel_id=222)

        leaderboards = await db.get_all_leaderboards_with_channels()

        assert len(leaderboards) == 2
        names = [lb["name"] for lb in leaderboards]
        assert "with_channel" in names
        assert "also_with" in names
        assert "no_channel" not in names
