"""Tests for full team-game data: raw storage, timelines and per-player stats."""
import json
from unittest.mock import AsyncMock

import pytest

import database as db
import game_stats
import team_games
from champions import champions
from game_stats import lane_at, compute_stats, infer_roles
from riot_api import RiotAPIError
from test_team_games import CHAMPIONS, OURS, ENEMIES, FakeSheet, client_game, five, match_row, ms

ROLES = ["Top", "Jungle", "Mid", "ADC", "Support"]
POSITIONS = {"Top": (1500, 9000), "Jungle": (7000, 4000), "Mid": (7500, 7400), "ADC": (9000, 1500), "Support": (9500, 1200)}


def frame(minute: int, roles: dict[int, str], gold: dict[int, int], events: list = None) -> dict:
    """A timeline frame; roles and gold-per-minute by participant ID."""
    frames = {}
    for pid, role in roles.items():
        x, y = POSITIONS[role]
        frames[str(pid)] = {
            "participantId": pid, "totalGold": gold[pid] * minute, "xp": 400 * minute,
            "minionsKilled": (0 if role in ("Jungle", "Support") else 8) * minute,
            "jungleMinionsKilled": (5 * minute if role == "Jungle" else 0), "position": {"x": x, "y": y},
        }
    return {"timestamp": minute * 60_000, "participantFrames": frames, "events": events or []}


def make_timeline(api: bool = False) -> dict:
    # Ours are participants 1-5 (in role order), enemies 6-10 (E1..E5 in role order)
    roles = {pid: ROLES[(pid - 1) % 5] for pid in range(1, 11)}
    gold = {pid: 400 for pid in roles}
    gold[1] = 460  # our top is 60 gold/min ahead
    kill = {"type": "CHAMPION_KILL", "timestamp": 6 * 60_000, "killerId": 6, "victimId": 1, "assistingParticipantIds": [7]}
    late_kill = {"type": "CHAMPION_KILL", "timestamp": 16 * 60_000, "killerId": 6, "victimId": 1}
    frames = [frame(m, roles, gold, [kill] if m == 7 else [late_kill] if m == 17 else []) for m in range(0, 21)]
    return {"info": {"frames": frames}} if api else {"frames": frames}


@pytest.fixture(autouse=True)
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test_lp_tracker.db")
    monkeypatch.setattr(champions, "by_key", dict(CHAMPIONS))
    monkeypatch.setattr(champions, "by_id", {})
    monkeypatch.setattr(champions, "refresh", AsyncMock())
    monkeypatch.setattr(game_stats, "UPLOADS_DIR", tmp_path / "uploads")


@pytest.fixture
def roster() -> team_games.TeamRoster:
    return team_games.TeamRoster([{"riot_id": r, "nick": n, "role": role, "puuid": None} for r, n, role in OURS])


def scrim(game_id: int = 1) -> dict:
    game = client_game(game_id, ms(29, 18, 5), five(), ENEMIES, our_team=100)
    for p, kills in zip(game["participants"], [3, 1, 4, 6, 0, 2, 2, 2, 2, 2]):
        p["stats"].update(kills=kills, goldEarned=10000 + 100 * p["participantId"], totalDamageDealtToChampions=1000 * kills + 500)
    return game


class TestLanes:
    """Test cases for lane_at and infer_roles."""

    def test_lane_at(self):
        assert lane_at(1500, 9000) == "Top"
        assert lane_at(5000, 13500) == "Top"
        assert lane_at(9000, 1500) == "Bot"
        assert lane_at(13500, 6000) == "Bot"
        assert lane_at(7500, 7400) == "Mid"
        assert lane_at(7000, 4000) is None

    def test_infer_roles_from_timeline(self):
        frames = make_timeline()["frames"]
        enemies = [{"participant_id": pid, "role": ""} for pid in range(6, 11)]
        infer_roles(enemies, frames)
        assert [p["role"] for p in enemies] == ROLES

    def test_known_roles_are_kept(self):
        frames = make_timeline()["frames"]
        team = [{"participant_id": 6, "role": "Mid"}] + [{"participant_id": pid, "role": ""} for pid in range(7, 11)]
        infer_roles(team, frames)
        # Participant 6 stood top but is known as Mid, so nobody else can be Mid and top stays free
        assert [p["role"] for p in team] == ["Mid", "Jungle", "", "ADC", "Support"]

    def test_last_missing_role_without_timeline(self):
        team = [{"participant_id": pid, "role": role} for pid, role in zip(range(6, 11), ["Jungle", "Mid", "", "ADC", "Support"])]
        infer_roles(team, [])
        assert team[2]["role"] == "Top"


class TestComputeStats:
    """Test cases for compute_stats."""

    def test_client_scrim_with_timeline(self, roster):
        raw = scrim()
        game = team_games.normalize_client_game(raw, roster)
        rows = {r["participant_id"]: r for r in compute_stats(game, "client", raw, make_timeline())}

        top = rows[1]
        assert top["ours"] and top["player"] == "drewin" and top["role"] == "Top"
        assert top["gold_diff_10"] == 600 and top["gold_diff_15"] == 900
        assert top["cs_diff_15"] == 0 and top["xp_diff_15"] == 0
        assert top["lane_opponent"] == CHAMPIONS[7]
        assert top["deaths_before_14"] == 1
        assert top["kill_participation"] == round((3 + 3) / 14, 3)
        assert rows[6]["role"] == "Top" and not rows[6]["ours"] and rows[6]["gold_diff_15"] == -900
        assert rows[9]["role"] == "ADC" and rows[10]["role"] == "Support"

    def test_without_timeline(self, roster):
        raw = scrim()
        game = team_games.normalize_client_game(raw, roster)
        rows = compute_stats(game, "client", raw, None)

        assert len(rows) == 10
        assert rows[0]["gold_diff_15"] is None and rows[0]["deaths_before_14"] is None
        assert rows[0]["damage_share"] == round(3500 / sum(1000 * k + 500 for k in [3, 1, 4, 6, 0]), 3)

    def test_api_game(self, roster):
        roles = {"Top": "TOP", "Jungle": "JUNGLE", "Mid": "MIDDLE", "ADC": "BOTTOM", "Support": "UTILITY"}
        participants = []
        for pid in range(1, 11):
            riot_id = (OURS[pid - 1][0] if pid <= 5 else ENEMIES[pid - 6])
            name, tag = riot_id.split("#")
            participants.append({
                "participantId": pid, "teamId": 100 if pid <= 5 else 200, "puuid": f"p{pid}",
                "riotIdGameName": name, "riotIdTagline": tag, "championId": pid, "championName": "",
                "teamPosition": roles[ROLES[(pid - 1) % 5]], "win": pid <= 5, "kills": 2, "deaths": 1, "assists": 3,
                "goldEarned": 9000, "totalDamageDealtToChampions": 12000, "summoner1Id": 11 if pid in (2, 7) else 4,
            })
        raw = {"metadata": {"matchId": "EUW1_99"}, "info": {
            "mapId": 11, "gameStartTimestamp": ms(29, 20), "gameDuration": 1500, "gameVersion": "16.21.712.4512",
            "tournamentCode": "EUW-CODE", "participants": participants, "teams": []}}
        game = team_games.normalize_api_game(raw, roster)
        rows = {r["participant_id"]: r for r in compute_stats(game, "api", raw, make_timeline(api=True))}

        assert rows[1]["patch"] == "16.21" and rows[1]["tournament"]
        assert rows[1]["gold_diff_15"] == 900
        assert rows[7]["role"] == "Jungle" and rows[7]["lane_opponent"] == CHAMPIONS[2]


class TestStorage:
    """Test cases for storing full data on upload and backfill."""

    @pytest.fixture
    def sheet(self, monkeypatch):
        sheet = FakeSheet(matches=[match_row(29, 18, "Scrim")])
        monkeypatch.setattr(team_games, "team_sheet", sheet)
        monkeypatch.setattr(team_games, "load_team_roster", AsyncMock(return_value=team_games.TeamRoster(
            [{"riot_id": r, "nick": n, "role": role, "puuid": None} for r, n, role in OURS])))
        monkeypatch.setattr(team_games.riot_api, "get_match_details", AsyncMock(side_effect=RiotAPIError(404, "")))
        return sheet

    async def test_upload_with_timeline(self, sheet):
        await db.init_db()
        payload = {"format": "lp-tracker-scrims", "version": 2, "games": [scrim()], "timelines": {"1": make_timeline()}}

        await team_games.ingest_client_upload(payload)

        stored = await db.get_team_game_raw("EUW1_1")
        assert stored["source"] == "client" and stored["game"]["gameId"] == 1 and stored["timeline"]
        stats = await db.get_team_game_stats("EUW1_1")
        assert len(stats) == 10 and stats[0]["gold_diff_15"] == 900

    async def test_resent_game_gets_its_timeline(self, sheet):
        await db.init_db()
        await team_games.ingest_client_upload({"format": "lp-tracker-scrims", "games": [scrim()]})
        assert (await db.get_team_game_stats("EUW1_1"))[0]["gold_diff_15"] is None

        summary = await team_games.ingest_client_upload(
            {"format": "lp-tracker-scrims", "version": 2, "games": [scrim()], "timelines": {"1": make_timeline()}})
        again = await team_games.ingest_client_upload(
            {"format": "lp-tracker-scrims", "version": 2, "games": [scrim()], "timelines": {"1": make_timeline()}})

        assert summary["timelines_added"] == 1 and again["timelines_added"] == 0
        assert (await db.get_team_game_stats("EUW1_1"))[0]["gold_diff_15"] == 900
        assert "Dopisany przebieg gry" in team_games.format_summary(summary)

    async def test_backfill_from_saved_uploads(self, sheet, tmp_path):
        await db.init_db()
        await team_games.ingest_games([team_games.normalize_client_game(scrim(), await team_games.load_team_roster())])
        assert await db.get_team_game_raw("EUW1_1") is None

        (tmp_path / "uploads").mkdir()
        (tmp_path / "uploads" / "upload.json").write_text(json.dumps({"format": "lp-tracker-scrims", "games": [scrim()]}))

        assert await game_stats.backfill_game_data() == 1
        assert await game_stats.backfill_game_data() == 0
        assert len(await db.get_team_game_stats("EUW1_1")) == 10
