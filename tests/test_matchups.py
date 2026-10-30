"""Tests for matchup data: OP.GG's matchup tables and lane opponents of our SoloQ games."""
import json
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aioresponses import aioresponses

import database as db
import opgg
from cogs import sheet
from riot_api import RiotAPI


def guide_response(counters: list[tuple[str, int, int]]) -> str:
    payload = {"data": {"counters": [{"champion_name": n, "play": p, "win": w} for n, p, w in counters]}}
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps(payload)}]}})


class TestOpgg:
    """Test cases for the OP.GG matchup client."""

    def test_names(self):
        assert [opgg.opgg_name(n) for n in ("Kai'Sa", "Lee Sin", "Nunu & Willump", "Dr. Mundo", "K'Sante", "Wukong")] == [
            "KAISA", "LEE_SIN", "NUNU_WILLUMP", "DR_MUNDO", "KSANTE", "WUKONG"]

    def test_parse(self):
        assert opgg.parse_matchups(guide_response([("Mel", 2556, 1278), ("Vel'Koz", 824, 399)])) == {
            "Mel": (2556, 1278), "Vel'Koz": (824, 399)}
        error = '{"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "Invalid position or champion"}}'
        assert opgg.parse_matchups(error) is None

    def test_win_rate_from_either_side(self):
        tables = {("Viktor", "Mid"): {"Syndra": (1000, 480)}, ("Ahri", "Mid"): {"Mel": (200, 120)}}
        assert opgg.win_rate(tables, "Syndra", "Viktor", "Mid") == (0.52, 1000)
        assert opgg.win_rate(tables, "Ahri", "Mel", "Mid") == (0.6, 200)
        assert opgg.win_rate(tables, "Ahri", "Zed", "Mid") is None

    async def test_fetch_and_cache(self, monkeypatch):
        monkeypatch.setattr(opgg, "_cache", {})
        monkeypatch.setattr(opgg, "REQUEST_DELAY", 0)
        with aioresponses() as mocked:
            mocked.post(opgg.API_URL, body=guide_response([("Mel", 100, 50)]))
            mocked.post(opgg.API_URL, body='{"jsonrpc": "2.0", "id": 1, "error": {"code": -32600}}')

            tables = await opgg.fetch_many({("Ahri", "Mid"), ("Nope", "Top")})
            again = await opgg.fetch_many({("Ahri", "Mid"), ("Nope", "Top")})

            sent = [call.kwargs["json"]["params"]["arguments"] for calls in mocked.requests.values() for call in calls]
        assert tables == again == {("Ahri", "Mid"): {"Mel": (100, 50)}}
        assert {"position": "mid", "my_champion": "AHRI", "opponent_champion": "LUX"} in sent
        assert len(sent) == 2  # the second round came from the cache, failures included


def match(match_id: str, players: list[tuple[str, int, str, str]]) -> dict:
    """match-v5 payload; players as (puuid, team, position, champion)."""
    return {"metadata": {"matchId": match_id}, "info": {"queueId": 420, "gameDuration": 1800, "participants": [
        {"puuid": puuid, "teamId": team, "teamPosition": position, "championName": champion, "win": team == 100}
        for puuid, team, position, champion in players]}}


class TestLaneOpponents:
    """Test cases for the lane opponent of our SoloQ games."""

    def test_extracted_with_the_game(self):
        data = match("EUW1_1", [("me", 100, "MIDDLE", "Ahri"), ("x", 200, "TOP", "Gnar"), ("y", 200, "MIDDLE", "Mel")])
        stats = RiotAPI("key").extract_player_match_stats(data, "me")
        assert (stats["position"], stats["lane_opponent"]) == ("MIDDLE", "Mel")
        assert sheet.match_rows("EUW1_1", data, {"me"})[0]["lane_opponent"] == "Mel"

    async def test_backfill(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
        monkeypatch.setattr(sheet.asyncio, "sleep", AsyncMock())
        await db.init_db()
        row = {"match_id": "EUW1_1", "puuid": "me", "queue_id": 420, "team_id": 100, "champion_name": "Ahri", "win": True,
               "remake": False, "kills": 1, "deaths": 1, "assists": 1, "game_start": 5000, "game_duration": 1800}
        await db.save_matches([row, dict(row, match_id="EUW1_2"), dict(row, match_id="EUW1_3", lane_opponent="Zed")])
        payloads = {
            "EUW1_1": match("EUW1_1", [("me", 100, "MIDDLE", "Ahri"), ("y", 200, "MIDDLE", "Mel")]),
            "EUW1_2": match("EUW1_2", [("me", 100, "", "Ahri")]),  # remake: no positions
        }
        details = AsyncMock(side_effect=lambda match_id: payloads[match_id])
        monkeypatch.setattr(sheet.riot_api, "get_match_details", details)

        assert await sheet.backfill_lane_opponents({"me"}, 0, 10) == 2
        assert await sheet.backfill_lane_opponents({"me"}, 0, 10) == 0

        stored = {m["match_id"]: m["lane_opponent"] for m in await db.get_matches_since(["me"], 0)}
        assert stored == {"EUW1_1": "Mel", "EUW1_2": "", "EUW1_3": "Zed"}
        assert details.await_count == 2
