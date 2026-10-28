"""Tests for leaderboard embed rendering."""
import pytest

from cogs.tracker import create_leaderboard_embed

LEADERBOARD = {"name": "pyry main"}


class TestLeaderboardEmbed:
    """Test cases for create_leaderboard_embed."""

    @pytest.mark.asyncio
    async def test_shows_discord_nick_before_account(self):
        """Test that the Discord nick is shown first, then the account and LP."""
        players = [{
            "riot_id": "nigelf42#yolo",
            "discord_name": "drewin",
            "tier": "MASTER",
            "rank": "I",
            "lp": 555,
            "prev_lp": 0,
            "wins": 206,
            "losses": 112,
        }]

        embed = await create_leaderboard_embed(LEADERBOARD, players)

        assert embed.description.startswith("**1.** **drewin** — nigelf42#yolo\n")
        assert "Master (555 LP)" in embed.description

    @pytest.mark.asyncio
    async def test_without_discord_nick_shows_account_only(self):
        """Test that players without a nick still render by account."""
        players = [{"riot_id": "NoNick#EUW", "discord_name": None, "tier": None, "lp": 0}]

        embed = await create_leaderboard_embed(LEADERBOARD, players)

        assert embed.description.startswith("**1.** NoNick#EUW\n")

    @pytest.mark.asyncio
    async def test_escapes_markdown_in_nick(self):
        """Test that underscores in nicks don't break formatting."""
        players = [{"riot_id": "Rychu Peja#SLUG", "discord_name": "drukarz_lp", "tier": None, "lp": 0}]

        embed = await create_leaderboard_embed(LEADERBOARD, players)

        assert "**drukarz\\_lp**" in embed.description
