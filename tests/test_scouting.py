"""Tests for opponent scouting."""
from collections import Counter
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot as bot_module
import database as db
import scouting
from champions import champions
from config import TIMEZONE
from scouting import (
    parse_players, season_of, parse_pro_meta, champion_key, summarize, ban_suggestions,
    build_sheet_rows, scout_rows, SHEET_COLS,
)

YEAR = 2026
GOLGG_HTML = """
<table class="region_filter"><tr><td>Season:</td></tr></table>
<table class='table_list playerslist tablesaw trhover'>
<thead><tr><th>Champion</th><th>Picks</th><th>Bans</th><th>PrioScore</th><th>Wins</th><th>Losses</th><th>Winrate</th></tr></thead>
<tbody>
<tr><td><a href='./champion-stats/1/'>Orianna</a></td><td>1504</td><td>4508</td><td>68%</td><td>803</td><td>701</td><td>53%</td></tr>
<tr><td><a href='./champion-stats/2/'>Nunu</a></td><td>10</td><td>17</td><td>0%</td><td>6</td><td>4</td><td>60%</td></tr>
<tr><td><a href='./champion-stats/3/'>Kaisa</a></td><td>848</td><td>603</td><td>14%</td><td>438</td><td>410</td><td>52%</td></tr>
</tbody></table>
"""


def game(champion: str, win=True, season=YEAR, queue=420, role="Top", kills=5, deaths=2, assists=5, puuid="p1", n=0) -> dict:
    return {"puuid": puuid, "match_id": f"EUW1_{champion}_{season}_{queue}_{n}", "queue_id": queue,
            "game_start": int(datetime(season or YEAR, 6, 1, tzinfo=TIMEZONE).timestamp() * 1000), "season": season,
            "champion": champion, "role": role, "win": win, "kills": kills, "deaths": deaths, "assists": assists}


def games(champion: str, wins: int, losses: int, **kwargs) -> list[dict]:
    return [game(champion, True, n=i, **kwargs) for i in range(wins)] + [game(champion, False, n=100 + i, **kwargs) for i in range(losses)]


class TestParsePlayers:
    """Test cases for parse_players."""

    def test_multi_opgg_link(self):
        link = "https://op.gg/lol/multisearch/euw?summoners=iym%230509%2CRychu+Peja%23SLUG%2CKiziu%23Gwali"
        assert parse_players(link) == ["iym#0509", "Rychu Peja#SLUG", "Kiziu#Gwali"]

    def test_euw_and_eune_links(self):
        text = ("https://op.gg/lol/multisearch/euw?summoners=Top%23EUW%2CMid%23EUW "
                "https://op.gg/lol/multisearch/eune?summoners=TopSmurf%23EUNE%2CMid%23EUW")
        assert parse_players(text) == ["Top#EUW", "Mid#EUW", "TopSmurf#EUNE"]

    def test_profile_links_and_list(self):
        text = ("https://op.gg/lol/summoners/eune/Some%20Name-EUNE\n"
                "Rychu Peja#SLUG, alive n HAPPY33#frk; rychu peja#slug")
        assert parse_players(text) == ["Some Name#EUNE", "Rychu Peja#SLUG", "alive n HAPPY33#frk"]

    def test_nothing_useful(self):
        assert parse_players("ekipa z AGH") == []


class TestProMeta:
    """Test cases for the gol.gg parser."""

    def test_parse(self):
        meta = parse_pro_meta(GOLGG_HTML)

        assert meta["orianna"] == {"champion": "Orianna", "picks": 1504, "bans": 4508, "prio": 0.68, "wr": 0.53}
        assert meta[champion_key("Nunu & Willump")]["picks"] == 10
        assert meta[champion_key("Kai'Sa")]["prio"] == 0.14

    def test_unexpected_page(self):
        assert parse_pro_meta("<html>Cloudflare</html>") == {}

    def test_season(self):
        assert season_of("16.21.712.4512") == 2026
        assert season_of("15.24.1.1") == 2025
        assert season_of("") is None


class TestSummarize:
    """Test cases for summarize and ban_suggestions."""

    def test_seasons_and_tournaments(self):
        history = (games("Camille", 8, 2) + games("Jax", 1, 3)
                   + games("Aatrox", 3, 3, season=YEAR - 1)
                   + games("Gnar", 2, 0, queue=0)
                   + games("Fiora", 5, 0, season=YEAR - 2))
        history.append(game("Camille", win=None, n=999))  # remake
        history.append(game("Teemo", season=None, n=998))  # not on Summoner's Rift

        s = summarize(history, YEAR, {"camille": {"prio": 0.2, "wr": 0.5}})

        assert [(c["champion"], c["games"], c["wins"]) for c in s["current"]] == [("Camille", 10, 8), ("Jax", 4, 1)]
        assert s["current"][0]["pro_prio"] == 0.2 and s["current"][0]["kda"] == 5.0
        assert [c["champion"] for c in s["previous"]] == ["Aatrox"]
        assert [c["champion"] for c in s["tournament"]] == ["Gnar"]
        assert s["current_games"] == 14 and s["role"] == "Top"
        assert s["one_trick"] is None

    def test_one_trick(self):
        s = summarize(games("Camille", 10, 5) + games("Jax", 2, 2), YEAR, {})
        assert s["one_trick"]["champion"] == "Camille"

    def test_ban_suggestions(self):
        top = {"riot_id": "Top#EUW", "summary": summarize(games("Camille", 8, 2) + games("Jax", 1, 3), YEAR, {})}
        mid = {"riot_id": "Mid#EUW", "summary": summarize(games("Ahri", 5, 5, role="Mid") + games("Azir", 2, 1, queue=0, role="Mid"), YEAR, {})}
        error = {"riot_id": "X#1", "error": "Nie znaleziono konta"}

        bans = ban_suggestions([top, mid, error])

        assert [b["champion"] for b in bans] == ["Camille", "Azir", "Ahri"]
        assert bans[0]["reason"] == "10 gier, 80% WR w tym sezonie" and bans[0]["player"] == "Top"
        assert bans[1]["reason"] == "3 w turniejach/Clashu (67% WR)"


class TestSheetRows:
    """Test cases for build_sheet_rows."""

    def test_layout(self):
        player = {"riot_id": "Top#EUW", "platform": "eun1", "ranks": {},
                  "summary": summarize(games("Camille", 8, 2) + games("Gnar", 1, 0, queue=0), YEAR, {})}
        report = {"team": "AGH", "year": YEAR, "created": datetime(2026, 10, 30, 16, 0, tzinfo=TIMEZONE),
                  "players": [player, {"riot_id": "X#1", "error": "Nie znaleziono konta"}],
                  "bans": ban_suggestions([player]), "summary": "• banujcie Camille", "pro_meta": {},
                  "history": {"games": 0, "wins": 0, "players": Counter(), "picks": Counter(), "bans": Counter()}}

        rows = build_sheet_rows(report)

        assert all(len(row) == SHEET_COLS for row in rows)
        assert rows[0][0] == "Scouting: AGH"
        assert ["Camille", "Top", "Top", "10 gier, 80% WR w tym sezonie", "", ""] == rows[5][:6]
        header = next(i for i, row in enumerate(rows) if row[0] == "Champion" and row[9] == "Champion")
        assert rows[header + 1][:6] == ["Camille", 10, 8, 2, "80%", 5.0]
        assert rows[header + 1][18:20] == ["Gnar", 1]
        assert ["X#1", "⚠ Nie znaleziono konta"] == rows[-1][:2]


class TestEmbed:
    """Test cases for build_embed."""

    def report(self, accounts: int, summary: str = None) -> dict:
        champs = ["Camille", "Jax", "Aatrox", "Gnar", "Fiora", "Renekton", "Ksante", "Rumble"]
        history = [g for i, c in enumerate(champs) for g in games(c, 5 + i, 3)]
        history += [g for c in champs for g in games(c, 2, 2, season=YEAR - 1)]
        history += [g for c in champs for g in games(c, 1, 1, queue=3130)]
        players = [{"riot_id": f"Very Long Player Name {i}#EUNE", "platform": "eun1", "ranks": {},
                    "summary": summarize(history, YEAR, {})} for i in range(accounts)]
        return {"team": "AGH", "year": YEAR, "created": datetime(2026, 10, 30, 16, 0, tzinfo=TIMEZONE),
                "players": players, "bans": ban_suggestions(players), "summary": summary, "pro_meta": {},
                "history": {"games": 0, "wins": 0, "players": Counter(), "picks": Counter(), "bans": Counter()}}

    def test_five_accounts_get_four_champions(self):
        embed = scouting.build_embed(self.report(5))
        assert embed.fields[0].value.count("(") == 4 * 3

    def test_ten_accounts_fit(self):
        embed = scouting.build_embed(self.report(10, summary="• " + "banujcie Camille " * 200))
        assert len(embed) <= scouting.EMBED_MAX_LENGTH
        assert len(embed.fields) == 11


class FakeRiot:
    """Match IDs per (puuid, type, window) and match payloads."""

    def __init__(self, matches: dict[str, dict], ids: dict[tuple, list[str]]):
        self.matches, self.ids, self.details_calls = matches, ids, []

    async def get_match_ids(self, puuid, match_type=None, start_time=None, end_time=None, start=0, count=100, queue=None):
        return self.ids.get((puuid, match_type or queue, end_time is None), [])[start:start + count]

    async def get_match_details(self, match_id):
        self.details_calls.append(match_id)
        return self.matches[match_id]


def payload(match_id: str, players: dict[str, str], version="16.21.1.1", queue=420) -> dict:
    return {"metadata": {"matchId": match_id}, "info": {
        "mapId": 11, "queueId": queue, "gameVersion": version, "gameStartTimestamp": 1_790_000_000_000,
        "participants": [{"puuid": puuid, "championId": 0, "championName": champ, "teamPosition": "TOP",
                          "win": True, "kills": 1, "deaths": 1, "assists": 1} for puuid, champ in players.items()]}}


@pytest.fixture(autouse=True)
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test_lp_tracker.db")
    monkeypatch.setattr(champions, "by_key", {})
    monkeypatch.setattr(champions, "by_id", {})
    monkeypatch.setattr(champions, "refresh", AsyncMock())


class TestSync:
    """Test cases for fetching and storing scouted games."""

    async def test_shared_games_fetched_once(self, monkeypatch):
        await db.init_db()
        matches = {
            "EUW1_1": payload("EUW1_1", {"a": "Camille", "b": "Ahri"}),
            "EUW1_2": payload("EUW1_2", {"a": "Jax"}, version="15.20.1.1"),
            "EUW1_3": payload("EUW1_3", {"b": "Azir"}, queue=3130),
            "EUN1_4": payload("EUN1_4", {"b": "Orianna"}, queue=700),
        }
        fake = FakeRiot(matches, {("a", "ranked", True): ["EUW1_1"], ("a", "ranked", False): ["EUW1_2"],
                                  ("b", "ranked", True): ["EUW1_1"], ("b", "tourney", True): ["EUW1_3"],
                                  ("b", 700, True): ["EUN1_4"]})
        monkeypatch.setattr(scouting.riot_api, "get_match_ids", fake.get_match_ids)
        monkeypatch.setattr(scouting.riot_api, "get_match_details", fake.get_match_details)

        assert await scouting.sync_scout_games("a", {"a", "b"}, YEAR) == 2
        assert await scouting.sync_scout_games("b", {"a", "b"}, YEAR) == 2
        assert await scouting.sync_scout_games("a", {"a", "b"}, YEAR) == 0

        assert fake.details_calls == ["EUW1_1", "EUW1_2", "EUW1_3", "EUN1_4"]
        b_games = await db.get_scout_games("b")
        assert {(g["champion"], g["season"], g["queue_id"]) for g in b_games} == {
            ("Ahri", 2026, 420), ("Azir", 2026, 3130), ("Orianna", 2026, 700)}
        summary = summarize(b_games, YEAR, {})
        assert [c["champion"] for c in summary["tournament"]] == ["Azir", "Orianna"]

    def test_remake_row(self):
        data = payload("EUW1_9", {"a": "Camille"})
        data["info"]["participants"][0]["gameEndedInEarlySurrender"] = True
        assert scout_rows("EUW1_9", data, {"a"})[0]["win"] is None


class TestKnownPlayers:
    """Test cases for finding an opponent's players."""

    async def test_saved_players_first(self):
        await db.init_db()
        await db.save_opponent("AGH Esports", ["A#1", "B#2"])
        assert await scouting.known_players("agh esports") == ["A#1", "B#2"]

    async def test_from_our_games_against_them(self, monkeypatch):
        await db.init_db()
        header = ["Data", "Godz.", "Typ", "Przeciwnik", "Mecz (do Map)"]
        monkeypatch.setattr(scouting, "GOOGLE_SHEET_ID", "sheet")
        monkeypatch.setattr(scouting.team_sheet, "read_matches", AsyncMock(return_value=[
            header, [1, 0.5, "Liga", "AGH", "01.10 18:00 · Liga"], [2, 0.5, "Scrim", "PWr", "02.10 18:00 · Scrim"]]))
        for match_id, label, enemies in (("EUW1_1", "01.10 18:00 · Liga", ["E1#X", "E2#X"]),
                                         ("EUW1_2", "01.10 18:00 · Liga", ["E1#X", "E3#X"]),
                                         ("EUW1_3", "02.10 18:00 · Scrim", ["P1#Y"])):
            await db.save_team_game({"match_id": match_id, "source": "api", "start_ms": 0, "win": True,
                                     "enemy": [{"riot_id": r, "champion": "Ahri"} for r in enemies],
                                     "enemy_bans": ["Camille"]}, label)

        players = await scouting.known_players("agh")
        history = await scouting.history_against_us("AGH")

        assert players[0] == "E1#X" and set(players) == {"E1#X", "E2#X", "E3#X"}
        assert history["games"] == 2 and history["bans"]["Camille"] == 2


class TestAutoScouting:
    """Test cases for scouting after the reminder the day before an official."""

    @pytest.fixture
    def lp_bot(self, monkeypatch):
        lp_bot = bot_module.LPTrackerBot()
        channel = SimpleNamespace(send=AsyncMock())
        monkeypatch.setattr(lp_bot, "get_reminder_channel", AsyncMock(return_value=channel))
        monkeypatch.setattr(bot_module.scouting, "known_players", AsyncMock(return_value=["A#1"]))
        monkeypatch.setattr(bot_module.scouting, "start_scouting", lambda *args: lp_bot.started.append(args))
        lp_bot.started = []
        return lp_bot

    async def reminders(self, monkeypatch, lp_bot, type_: str, hours_before: int):
        await db.init_db()
        start = datetime(2026, 10, 31, 17, 0, tzinfo=TIMEZONE)
        match = {"start": start, "has_time": True, "type": type_, "event": "Liga", "opponent": "AGH", "notes": ""}
        monkeypatch.setattr(bot_module, "team_sheet", SimpleNamespace(read_matches=AsyncMock(return_value=[])))
        monkeypatch.setattr(bot_module, "parse_planned_matches", lambda rows: [match])
        now = start.replace(hour=17 - hours_before) if hours_before < 17 else datetime(2026, 10, 30, 18, 0, tzinfo=TIMEZONE)
        monkeypatch.setattr(bot_module, "datetime", SimpleNamespace(now=lambda tz=None: now))
        await lp_bot.check_match_reminders()

    async def test_official_is_scouted_the_day_before(self, monkeypatch, lp_bot):
        await self.reminders(monkeypatch, lp_bot, "Liga", 23)
        assert [args[1:] for args in lp_bot.started] == [("AGH", ["A#1"])]

    async def test_not_on_the_1h_reminder(self, monkeypatch, lp_bot):
        await self.reminders(monkeypatch, lp_bot, "Liga", 1)
        assert lp_bot.started == []

    async def test_not_for_scrims(self, monkeypatch, lp_bot):
        await self.reminders(monkeypatch, lp_bot, "Scrim", 23)
        assert lp_bot.started == []
