"""Tests for the match notification loop in bot.py."""
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

import bot as bot_module
import database as db

GUILD_ID = 123456789
CHANNEL_ID = 555
PUUID = "test-puuid"
DAY_MS = 24 * 60 * 60 * 1000


def make_match(match_id: str, start_ms: int, duration_s: int = 1800) -> dict:
    """Build a minimal match-v5 payload for PUUID."""
    return {
        "metadata": {"matchId": match_id},
        "info": {
            "gameStartTimestamp": start_ms,
            "gameEndTimestamp": start_ms + duration_s * 1000,
            "gameDuration": duration_s,
            "queueId": 420,
            "participants": [{
                "puuid": PUUID,
                "win": True,
                "championName": "Ahri",
                "championId": 103,
                "kills": 5,
                "deaths": 2,
                "assists": 7,
                "totalMinionsKilled": 200,
                "neutralMinionsKilled": 10,
            }],
        },
    }


class FakeRiotMatches:
    """Stands in for the Riot match-v5 endpoints, honoring startTime like the real API."""

    def __init__(self, matches: list[dict]):
        self.matches = {m["metadata"]["matchId"]: m for m in matches}

    async def get_match_ids_by_puuid(self, puuid, queue=420, count=5, start_time=None):
        matches = sorted(
            self.matches.values(),
            key=lambda m: m["info"]["gameStartTimestamp"],
            reverse=True
        )
        if start_time:
            matches = [m for m in matches if m["info"]["gameStartTimestamp"] >= start_time * 1000]
        return [m["metadata"]["matchId"] for m in matches[:count]]

    async def get_match_details(self, match_id):
        return self.matches[match_id]


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch, tmp_path):
    """Use a throwaway database for each test."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test_lp_tracker.db")


@pytest.fixture
async def leaderboard_id():
    """A leaderboard with one player and notifications enabled 30 days ago."""
    await db.init_db()
    lb_id = await db.create_leaderboard(GUILD_ID, "Team")
    await db.add_player(lb_id, "Player#EUW", PUUID)
    await db.set_leaderboard_notification_channel(GUILD_ID, "Team", CHANNEL_ID)
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute(
            "UPDATE leaderboards SET notifications_enabled_at = ? WHERE id = ?",
            (datetime.utcnow() - timedelta(days=30), lb_id)
        )
        await conn.commit()
    return lb_id


@pytest.fixture
def channel():
    return SimpleNamespace(send=AsyncMock())


@pytest.fixture
def lp_bot(monkeypatch, channel):
    """The bot with Discord and sleeps stubbed out."""
    monkeypatch.setattr(bot_module, "asyncio", SimpleNamespace(sleep=AsyncMock()))
    # Not in the roster sheet, so no comments on games (tests for those are in test_roasts.py)
    monkeypatch.setattr(bot_module.team_sheet, "read_roster_cached", AsyncMock(return_value=[]))
    lp_bot = bot_module.LPTrackerBot()
    monkeypatch.setattr(lp_bot, "get_channel", lambda channel_id: channel)
    return lp_bot


def use_riot_matches(monkeypatch, matches: list[dict]) -> None:
    fake = FakeRiotMatches(matches)
    monkeypatch.setattr(bot_module.riot_api, "get_match_ids_by_puuid", fake.get_match_ids_by_puuid)
    monkeypatch.setattr(bot_module.riot_api, "get_match_details", fake.get_match_details)


async def count_notified_rows() -> int:
    async with aiosqlite.connect(db.DB_PATH) as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM notified_matches")
        return (await cursor.fetchone())[0]


class TestMatchNotifications:
    """Test cases for check_match_notifications."""

    async def test_old_matches_not_reposted_after_cleanup(self, monkeypatch, leaderboard_id, lp_bot, channel):
        """Test that a player inactive for over a week doesn't get their last games posted again."""
        now_ms = int(time.time() * 1000)
        old_matches = [make_match(f"EUW1_{i}", now_ms - (8 + i) * DAY_MS) for i in range(5)]
        use_riot_matches(monkeypatch, old_matches)

        # The games were notified 8 days ago, so cleanup will drop their records
        for match in old_matches:
            await db.mark_match_notified(leaderboard_id, match["metadata"]["matchId"], PUUID)
        async with aiosqlite.connect(db.DB_PATH) as conn:
            await conn.execute("UPDATE notified_matches SET notified_at = datetime('now', '-8 days')")
            await conn.commit()

        await lp_bot.check_match_notifications()
        assert await count_notified_rows() == 0

        await lp_bot.check_match_notifications()

        channel.send.assert_not_awaited()

    async def test_new_match_notified_once(self, monkeypatch, leaderboard_id, lp_bot, channel):
        """Test that a fresh match is posted on the first check and not again on the next."""
        now_ms = int(time.time() * 1000)
        use_riot_matches(monkeypatch, [make_match("EUW1_new", now_ms - 40 * 60 * 1000)])

        await lp_bot.check_match_notifications()
        await lp_bot.check_match_notifications()

        channel.send.assert_awaited_once()

    def test_lookback_shorter_than_retention(self):
        """Test that matches stop being fetched before their notified records are cleaned up."""
        assert bot_module.MATCH_LOOKBACK_SECONDS < bot_module.NOTIFIED_MATCH_RETENTION_DAYS * 24 * 60 * 60
