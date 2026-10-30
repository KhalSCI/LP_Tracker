"""Tests for comments on SoloQ games."""
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot as bot_module
import database as db
import roasts
from config import TIMEZONE
from roasts import detect_trigger, template_text, build_prompt, streak

PUUID = "test-puuid"
HOUR_MS = 60 * 60 * 1000


def game(win: bool, champion: str = "Ashe", start: datetime = None, kills=5, deaths=3, assists=8, remake=False) -> dict:
    """A row of the matches table."""
    start = start or datetime(2026, 10, 30, 18, 0, tzinfo=TIMEZONE)
    return {"match_id": f"EUW1_{start.timestamp()}", "puuid": PUUID, "queue_id": 420, "team_id": 100,
            "champion_name": champion, "win": win, "remake": remake, "kills": kills, "deaths": deaths,
            "assists": assists, "game_start": int(start.timestamp() * 1000), "game_duration": 1800}


def history(*results: bool, champion: str = "Ashe", start: datetime = None) -> list[dict]:
    """Games newest first, an hour apart, with the given results (newest first)."""
    start = start or datetime(2026, 10, 30, 22, 0, tzinfo=TIMEZONE)
    return [game(win, champion, start - timedelta(hours=i)) for i, win in enumerate(results)]


def stats_for(newest: dict, **extra) -> dict:
    return {"match_id": newest["match_id"], "win": newest["win"], "champion_name": newest["champion_name"],
            "kills": newest["kills"], "deaths": newest["deaths"], "assists": newest["assists"],
            "remake": False, "cs_per_min": 7.1, "game_duration_minutes": 30, "queue_id": 420, **extra}


class TestDetectTrigger:
    """Test cases for detect_trigger."""

    def test_three_losses(self):
        games = history(False, False, False, True)
        assert detect_trigger(stats_for(games[0]), games)["kind"] == "lose_streak"

    def test_four_losses_is_not_a_milestone(self):
        games = history(False, False, False, False, True)
        assert detect_trigger(stats_for(games[0]), games) is None

    def test_five_wins(self):
        games = history(True, True, True, True, True, False)
        trigger = detect_trigger(stats_for(games[0]), games)
        assert trigger == {"kind": "win_streak", "reason": "5 wygranych z rzędu"}

    def test_streak_end(self):
        games = history(True, False, False, False, False)
        assert detect_trigger(stats_for(games[0]), games)["kind"] == "streak_end"

    def test_int(self):
        games = history(False, True)
        games[0].update(kills=1, deaths=11, assists=2)
        assert detect_trigger(stats_for(games[0]), games)["kind"] == "int"

    def test_pentakill_in_a_loss(self):
        games = history(False, True)
        assert detect_trigger(stats_for(games[0], penta_kills=1), games)["reason"] == "pentakill"

    def test_off_pool_loss(self):
        comfort = [game(i % 4 != 0, "Camille", datetime(2026, 10, 20, 18, 0, tzinfo=TIMEZONE) - timedelta(hours=i)) for i in range(10)]
        games = history(False, True, champion="Aatrox") + comfort
        trigger = detect_trigger(stats_for(games[0]), games)
        assert trigger["kind"] == "off_pool"
        assert "(Aatrox, wcześniej 1 gra), a Camille ma 10 gier i 70% WR" in trigger["reason"]

    def test_no_off_pool_without_a_comfort_pick(self):
        games = history(False, True, champion="Aatrox")
        assert detect_trigger(stats_for(games[0]), games) is None

    def test_third_night_loss(self):
        games = history(False, False, True, start=datetime(2026, 10, 31, 2, 30, tzinfo=TIMEZONE))
        games.insert(2, game(False, start=datetime(2026, 10, 31, 0, 45, tzinfo=TIMEZONE)))
        games[3]["win"] = True
        # Newest first: L 2:30, L 1:30, L 0:45, W 0:30 -> third loss of the night, but also a 3-game streak
        assert detect_trigger(stats_for(games[0]), games)["kind"] == "lose_streak"
        games[2]["win"] = True
        games.insert(3, game(False, start=datetime(2026, 10, 31, 0, 15, tzinfo=TIMEZONE)))
        # L 2:30, L 1:30, W 0:45, L 0:15 -> no streak, third loss tonight
        assert detect_trigger(stats_for(games[0]), games)["kind"] == "night"

    def test_remake_is_ignored(self):
        games = history(False, False, False)
        assert detect_trigger(stats_for(games[0], remake=True), games) is None

    def test_streak(self):
        assert streak(history(True, True, False)) == (True, 2)
        assert streak([]) == (False, 0)


class TestPlural:
    """Test cases for Polish plural forms."""

    def test_forms(self):
        assert [roasts.games_count(n) for n in (1, 2, 4, 5, 12, 22, 25)] == [
            "1 gra", "2 gry", "4 gry", "5 gier", "12 gier", "22 gry", "25 gier"]


class TestTexts:
    """Test cases for the prompt and the template fallback."""

    def test_prompt_has_the_facts(self):
        games = history(False, False, False, True)
        player = {"tier": "EMERALD", "rank": "II", "lp": 45}
        prompt = build_prompt("khal", player, stats_for(games[0], position="BOTTOM"),
                              {"kind": "lose_streak", "reason": "3 porażki z rzędu"}, games, "Kalista")

        assert "Gracz: khal (Emerald II 45 LP)" in prompt
        assert "3 porażki z rzędu" in prompt
        assert "przeciwnik na linii: Kalista" in prompt
        assert "L Ashe 5/3/8, L Ashe 5/3/8, L Ashe 5/3/8, W Ashe 5/3/8" in prompt

    def test_prompt_has_the_team_description(self):
        import player_profiles
        player_profiles.PROFILES_PATH.write_text("## khal\nADC i kapitan.\n", encoding="utf-8")
        games = history(False, False, False, True)
        prompt = build_prompt("khal", {"tier": None}, stats_for(games[0]),
                              {"kind": "lose_streak", "reason": "3 porażki z rzędu"}, games, None)

        assert prompt.startswith("Gracz: khal (bez rangi)\nOpis od drużyny: ADC i kapitan.\nPowód komentarza")

    def test_template_suggests_the_comfort_pick(self):
        comfort = [game(i % 4 != 0, "Camille", datetime(2026, 10, 20, tzinfo=TIMEZONE) - timedelta(hours=i)) for i in range(10)]
        games = history(False, False, False, champion="Aatrox") + comfort
        text = template_text({"kind": "lose_streak", "reason": "3 porażki z rzędu"}, stats_for(games[0]), games)

        assert text.startswith("3 porażki z rzędu.")
        assert "Camille (70% WR)" in text


def match_payload(match_id: str, start_ms: int, win: bool) -> dict:
    return {
        "metadata": {"matchId": match_id},
        "info": {
            "gameStartTimestamp": start_ms, "gameEndTimestamp": start_ms + 1800_000, "gameDuration": 1800,
            "queueId": 420,
            "participants": [
                {"puuid": PUUID, "teamId": 100, "teamPosition": "BOTTOM", "win": win, "championName": "Ashe",
                 "championId": 22, "kills": 2, "deaths": 6, "assists": 3, "totalMinionsKilled": 200,
                 "neutralMinionsKilled": 0, "pentaKills": 0, "quadraKills": 0},
                {"puuid": "enemy", "teamId": 200, "teamPosition": "BOTTOM", "win": not win, "championName": "Kalista",
                 "championId": 429, "kills": 6, "deaths": 2, "assists": 2, "totalMinionsKilled": 210,
                 "neutralMinionsKilled": 0},
            ],
        },
    }


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test_lp_tracker.db")


@pytest.fixture
async def lp_bot(monkeypatch):
    await db.init_db()
    lp_bot = bot_module.LPTrackerBot()
    monkeypatch.setattr(bot_module.team_sheet, "read_roster_cached",
                        AsyncMock(return_value=[{"nick": "khal", "riot_id": "OpenYourMind#MAGIK", "role": "ADC", "status": "Main"}]))
    monkeypatch.setattr(bot_module, "find_member_id", AsyncMock(return_value=42))
    monkeypatch.setattr(bot_module.champions, "refresh", AsyncMock())
    monkeypatch.setattr(roasts.llm, "complete", AsyncMock(return_value="Mój stary by lepiej zagrał tą Ashe."))
    return lp_bot


PLAYER = {"puuid": PUUID, "riot_id": "OpenYourMind#MAGIK", "discord_name": "khal", "tier": "EMERALD", "rank": "II", "lp": 45}


async def play(lp_bot, results: list[bool]) -> list:
    """Notify games with the given results (oldest first); returns the reply mocks."""
    now_ms = int(time.time() * 1000) - 10 * HOUR_MS
    messages = []
    for i, win in enumerate(results):
        data = match_payload(f"EUW1_{i}", now_ms + i * HOUR_MS, win)
        stats = bot_module.riot_api.extract_player_match_stats(data, PUUID)
        message = SimpleNamespace(id=1000 + i, reply=AsyncMock(), guild=SimpleNamespace(name="PYRY"))
        await lp_bot.comment_on_game(message, PLAYER, data, stats, {PUUID})
        messages.append(message)
    return messages


class TestCommentOnGame:
    """Test cases for comment_on_game in bot.py."""

    async def test_third_loss_gets_a_comment_with_a_ping(self, lp_bot):
        messages = await play(lp_bot, [False, False, False])

        messages[0].reply.assert_not_awaited()
        messages[1].reply.assert_not_awaited()
        messages[2].reply.assert_awaited_once()
        assert messages[2].reply.await_args.args[0] == "<@42> Mój stary by lepiej zagrał tą Ashe."
        assert [u.id for u in messages[2].reply.await_args.kwargs["allowed_mentions"].users] == [42]

    async def test_lane_opponent_reaches_the_model(self, lp_bot):
        await play(lp_bot, [False, False, False])

        assert "przeciwnik na linii: Kalista" in roasts.llm.complete.await_args.args[1]

    async def test_daily_limit(self, monkeypatch, lp_bot):
        monkeypatch.setattr(bot_module, "ROASTS_PER_DAY", 1)
        messages = await play(lp_bot, [False, False, False, False, False])

        assert sum(m.reply.await_count for m in messages) == 1

    async def test_opted_out_player_gets_nothing(self, lp_bot):
        await db.set_roast_optout(42, True)
        messages = await play(lp_bot, [False, False, False])

        messages[2].reply.assert_not_awaited()

    async def test_player_not_in_roster_gets_nothing(self, monkeypatch, lp_bot):
        monkeypatch.setattr(bot_module.team_sheet, "read_roster_cached", AsyncMock(return_value=[]))
        messages = await play(lp_bot, [False, False, False])

        messages[2].reply.assert_not_awaited()

    async def test_template_without_the_model(self, monkeypatch, lp_bot):
        monkeypatch.setattr(roasts.llm, "complete", AsyncMock(return_value=None))
        messages = await play(lp_bot, [False, False, False])

        assert messages[2].reply.await_args.args[0].startswith("<@42> 3 porażki z rzędu.")
